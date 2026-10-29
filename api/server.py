import asyncio
import json
import logging
import sys
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Literal

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles as FastAPIStaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.insert(0, str(project_root))

# Import agent runner and monitor
from agent.main_agent import initialize_agent, run_deep_agent, shutdown_agent  # noqa: E402
from agent.runtime.context import RunContext, RunStopped, reset_run_context, set_run_context  # noqa: E402
from agent.runtime.events import EventSink  # noqa: E402
from agent.runtime.hooks import HookCall, finish_run, pipeline  # noqa: E402
from agent.runtime.policies import load_runtime_policy  # noqa: E402
from agent.runtime.skills import SKILL_IDS, skill_catalog, validate_skill_ids  # noqa: E402
from agent.runtime.workflows import finalize_answer  # noqa: E402
from api.monitor import manager  # noqa: E402
from api.run_store import RunConflict, RunStore  # noqa: E402
from api.security import COOKIE, AuthService, DataLease, SecurityMiddleware, SecuritySettings  # noqa: E402
from utils.path_utils import PathDenied, confined_path, validate_filename, validate_identifier  # noqa: E402
from utils.secure_paths import iter_confined_files, resource_path  # noqa: E402

logger = logging.getLogger(__name__)
run_db_path = project_root / "data" / "runs.db"


def _task_finished(task, run_id):
    app.state.agent_tasks.discard(task)
    context = app.state.run_contexts.get(run_id)
    if context and context.scheduler_ticket:
        context.scheduler.gate("runs").cancel(context.scheduler_ticket)
    if app.state.run_tasks.get(run_id) is task:
        del app.state.run_tasks[run_id]
        app.state.run_contexts.pop(run_id, None)
    if not task.cancelled():
        error = task.exception()
        if error is not None:
            logger.error(
                "Agent execution/recording raised for run %s",
                run_id,
                exc_info=(type(error), error, error.__traceback__),
            )


def _execution_finished(task, executions):
    executions.discard(task)
    if not task.cancelled():
        task.exception()  # Detached late errors cannot change the saved outcome.


async def _run_body(record, context):
    token = set_run_context(context)
    try:
        context.check_active()
        pipeline.emit("before_run", HookCall("main", context.run_id))
        answer = await run_deep_agent(record["question"], context.thread_id)
        context.check_active()
        answer = await finalize_answer(context, answer)
        context.check_active()
        return answer
    finally:
        reset_run_context(token)


async def _execute_run(record, context):
    if context.deadline is None:
        duration = context.policy.timeouts.total_seconds
        context.deadline = datetime.now(UTC) + timedelta(seconds=duration)
        context.deadline_clock = monotonic() + duration
    try:
        async with context.scheduler.run_slot(context):
            await _execute_run_inner(record, context)
    except BaseException as error:
        # Queue cancellation/expiry must durably settle a run that never started.
        current = await context.event_sink.store.get(context.run_id)
        if current and current["status"] == "queued":
            status = context.stop_status or ("cancelled" if isinstance(error, asyncio.CancelledError) else "failed")
            context.stop(status)
            await context.event_sink.finish(context, status, error=str(error) or status)
        raise


async def _execute_run_inner(record, context):
    token = set_run_context(context)
    outcome = HookCall("main", context.run_id)
    answer = None
    error = None
    worker = None
    try:
        if context.remaining() is None:
            context.policy = context.policy or load_runtime_policy()
            duration = context.policy.timeouts.total_seconds
            context.deadline = datetime.now(UTC) + timedelta(seconds=duration)
            context.deadline_clock = monotonic() + duration
        if not await context.event_sink.start(context):
            return
        worker = asyncio.create_task(_run_body(record, context), name=f"execution:{context.run_id}")
        executions = getattr(app.state, "execution_tasks", set())
        executions.add(worker)
        worker.add_done_callback(lambda task: _execution_finished(task, executions))
        done, _ = await asyncio.wait({worker}, timeout=max(0, context.remaining()))
        if not done:
            context.stop("timed_out")
            raise RunStopped("Total run deadline exceeded")
        answer = worker.result()
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("执行器未返回有效最终答复，不能标记完成")
        context.check_active()
        outcome.status, outcome.result = "completed", answer
    except asyncio.CancelledError:
        outcome.status = context.stop_status or context.stop("cancelled")
        error = "Service execution cancelled"
        pipeline.emit("on_error", HookCall("main", context.run_id, status="cancelled", error=asyncio.CancelledError()))
        raise
    except Exception as exc:
        outcome.status, outcome.error = context.stop_status or "failed", exc
        error = str(exc) or type(exc).__name__
        pipeline.emit("on_error", outcome)
        raise
    finally:
        try:
            if outcome.status != "running":
                if outcome.status != "completed":
                    context.stop(outcome.status)
                if worker is not None and not worker.done():
                    worker.cancel()
                await finish_run(context, outcome, answer=answer, error=error)
                context.stop(outcome.status)
        finally:
            reset_run_context(token)


@asynccontextmanager
async def lifespan(app):
    app.state.security_settings = SecuritySettings.load()
    lease = DataLease(run_db_path.parent)
    try:
        async with runtime_lifespan(app):
            yield
    finally:
        lease.close()


@asynccontextmanager
async def runtime_lifespan(app):
    app.state.agent_tasks = set()
    app.state.run_tasks = {}
    app.state.run_contexts = {}
    app.state.execution_tasks = set()
    app.state.submission_lock = asyncio.Lock()
    app.state.stopping = False
    store = None
    sink = None
    try:
        app.state.runtime_policy = load_runtime_policy()
        from agent.runtime.scheduler import ServiceScheduler

        scheduler = getattr(app.state, "scheduler", None)
        limits = app.state.runtime_policy.scheduler
        if scheduler is not None and scheduler.limits != limits:
            if any(row["active"] or row["queued"] for row in scheduler.snapshot().values()):
                raise RuntimeError("Cannot change scheduling limits while previous calls are still in flight")
            scheduler = None
        app.state.scheduler = scheduler if scheduler is not None else ServiceScheduler(limits)
        store = await RunStore.open(run_db_path)
        app.state.auth = await AuthService.open(store, app.state.security_settings)
        app.state.run_store = store
        sink = EventSink(store, manager.schedule_delivery)
        app.state.event_sink = sink
        output_dir.mkdir(exist_ok=True)
        await initialize_agent()
        await sink.interrupt_active("Service restarted; previous execution was interrupted")
        updated_dir.mkdir(exist_ok=True)
        manager.set_loop(asyncio.get_running_loop())
        manager.store = store
        yield
    finally:
        app.state.stopping = True
        # Wait for an admission already in progress before snapshotting tasks.
        async with app.state.submission_lock:
            tasks = list(app.state.agent_tasks)
        for context in list(app.state.run_contexts.values()):
            context.stop("interrupted")
            await sink.finish(context, "interrupted", error="Service shut down before execution completed")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        executions = set(app.state.execution_tasks)
        for task in executions:
            task.cancel()
        if executions:
            await asyncio.wait(executions, timeout=app.state.runtime_policy.timeouts.shutdown_seconds)
        try:
            if sink is not None:
                try:
                    await sink.interrupt_active("Service shut down before execution completed")
                finally:
                    await sink.close()
        finally:
            try:
                await manager.shutdown()
            finally:
                try:
                    if store is not None:
                        await store.close()
                finally:
                    await shutdown_agent()


app = FastAPI(title="DeepAgents API", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SecurityMiddleware)


from agent.runtime.scheduler import QueueFull  # noqa: E402


@app.exception_handler(QueueFull)
async def scheduling_backpressure(request, error):
    return JSONResponse(status_code=503, content={"status": "busy", "detail": str(error)})


# Artifact roots are private; downloads always resolve through an owned run.
output_dir = project_root / "output"


# 定义上传目录 updated
updated_dir = project_root / "updated"


# 挂载静态文件目录 (前端调试页面)
static_dir = current_dir / "static"

app.mount("/static", FastAPIStaticFiles(directory=str(static_dir)), name="static")


# 首页
@app.get("/", response_class=HTMLResponse)
async def root():
    index_path = static_dir / "index.html"
    if index_path.exists():
        return index_path.read_text(encoding="utf-8")
    raise HTTPException(status_code=503, detail="Login page unavailable")


def auth_view(session):
    return {"csrf_token": session["csrf"], "expires_at": session["expires_at"]}


@app.post("/api/auth/login")
async def login(request: Request):
    # Keep password validation outside Pydantic errors, which echo invalid inputs.
    if request.url.query:
        raise HTTPException(status_code=400, detail="Login does not accept URL parameters")
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise HTTPException(status_code=400, detail="JSON login required")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 8192:
            raise HTTPException(status_code=413, detail="Login request too large")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        raise HTTPException(status_code=400, detail="Invalid login request") from None
    if (
        not isinstance(body, dict)
        or set(body) != {"password"}
        or not isinstance(body["password"], str)
        or not 1 <= len(body["password"]) <= 1024
    ):
        raise HTTPException(status_code=400, detail="Invalid login request")
    try:
        body["password"].encode("utf-8")
    except UnicodeError:
        raise HTTPException(status_code=400, detail="Invalid login request") from None
    result, status = await app.state.auth.login(body["password"])
    if result is None:
        return JSONResponse(
            {"detail": "登录失败，请检查口令" if status == 401 else "尝试过多，请一分钟后再试"},
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )
    token, session = result
    # Replace a prior cookie without changing the persistent resource owner.
    from api.security import session_cookie

    previous = session_cookie(request.headers)
    if previous:
        await app.state.auth.revoke(app.state.auth.digest(previous))
    response = JSONResponse(auth_view(session), headers={"Cache-Control": "no-store"})
    response.set_cookie(
        COOKIE,
        token,
        max_age=app.state.security_settings.session_seconds,
        secure=app.state.security_settings.secure,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return response


@app.get("/api/auth/session")
async def get_auth_session(request: Request):
    return auth_view(request.state.auth_session)


@app.post("/api/auth/logout")
async def logout(request: Request):
    await app.state.auth.revoke(request.state.auth_session["token_hash"])
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie(
        COOKIE, path="/", secure=app.state.security_settings.secure, httponly=True, samesite="strict"
    )
    return response


@app.post("/api/threads")
async def create_thread():
    return {"thread_id": await app.state.run_store.create_thread()}


@app.get("/api/skills")
async def get_skills():
    return skill_catalog()


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: StrictStr = Field(min_length=1)
    input_text: StrictStr | None = Field(default=None, min_length=1, max_length=100000)
    network_policy: Literal["allowed", "forbidden"] = "allowed"
    skill_ids: list[StrictStr] | None = Field(default=None, max_length=len(SKILL_IDS))
    business_mode: Literal["ordinary", "internal_external"] = "ordinary"
    required_sources: list[Literal["db", "search"]] = Field(default_factory=list, max_length=2)
    required_uploads: list[StrictStr] = Field(default_factory=list, max_length=20)
    thread_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")

    @field_validator("query", "input_text")
    @classmethod
    def nonblank_text(cls, value):
        if value is not None and not value.strip():
            raise ValueError("任务指令和原文不能仅包含空白")
        return value

    @field_validator("skill_ids")
    @classmethod
    def known_skills(cls, value):
        validate_skill_ids(value)
        return value

    @field_validator("required_sources", "required_uploads")
    @classmethod
    def unique_requirements(cls, value, info):
        if len(set(value)) != len(value):
            raise ValueError("Duplicate report requirement")
        if info.field_name == "required_uploads":
            for name in value:
                try:
                    validate_filename(name)
                except PathDenied as exc:
                    raise ValueError(str(exc)) from exc
        return value

    @model_validator(mode="after")
    def explicit_workflow(self):
        if self.network_policy == "forbidden" and (
            "search" in self.required_sources or self.business_mode == "internal_external"
        ):
            raise ValueError("禁止联网与公开研究要求冲突；请调整显式执行约束")
        return self


@app.post("/api/task")
async def run_task(request: TaskRequest):
    if request.thread_id is not None:
        if not await app.state.run_store.owns_thread(request.thread_id):
            raise HTTPException(status_code=404, detail="Thread not found")
        thread_id = request.thread_id
    else:
        thread_id = await app.state.run_store.create_thread()
    async with app.state.submission_lock:
        if app.state.stopping:
            raise HTTPException(status_code=503, detail="Service is shutting down")
        run_id = str(uuid.uuid4())
        try:
            record = await app.state.event_sink.create(thread_id, request.query, run_id)
        except asyncio.CancelledError:
            # A cancelled await can race a committed insert in the DB worker.
            # The server-generated ID lets us settle precisely that reservation.
            await app.state.event_sink.finish(
                RunContext(run_id, thread_id, output_dir, owner_id=app.state.run_store.owner_id),
                "cancelled",
                error="Task submission cancelled",
            )
            raise
        except RunConflict as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "status": "busy",
                    "thread_id": thread_id,
                    "run_id": exc.run_id,
                },
            )
        run_id = record["run_id"]
        context = RunContext(
            run_id,
            thread_id,
            output_dir / f"session_{thread_id}" / f"run_{run_id}",
            owner_id=app.state.run_store.owner_id,
            deadline=datetime.now(UTC) + timedelta(seconds=app.state.runtime_policy.timeouts.total_seconds),
            deadline_clock=monotonic() + app.state.runtime_policy.timeouts.total_seconds,
            event_sink=app.state.event_sink,
            policy=app.state.runtime_policy,
            scheduler=app.state.scheduler,
            user_query=request.query,
            input_text=request.input_text,
            network_policy=request.network_policy,
            skill_ids=tuple(request.skill_ids or ()),
            business_mode=request.business_mode,
            required_sources=tuple(request.required_sources),
            required_uploads=tuple(request.required_uploads),
        )
        app.state.run_contexts[run_id] = context
        record = dict(record, question=context.user_query)
        from agent.runtime.scheduler import QueueFull

        try:
            context.scheduler_ticket = app.state.scheduler.gate("runs").enqueue()
        except QueueFull:
            await context.event_sink.finish(context, "failed", error="Service execution queue is full")
            app.state.run_contexts.pop(run_id, None)
            return JSONResponse(status_code=503, content={"status": "busy", "run_id": run_id})
        from agent.runtime.scheduler import detached_context

        task = asyncio.create_task(_execute_run(record, context), name=f"run:{run_id}", context=detached_context())
        app.state.agent_tasks.add(task)
        app.state.run_tasks[run_id] = task
        task.add_done_callback(lambda completed: _task_finished(completed, run_id))
    return {"status": "started", "thread_id": thread_id, "run_id": run_id}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    record = await app.state.run_store.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return record


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    async with app.state.submission_lock:
        record = await app.state.run_store.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Run not found")
        context = app.state.run_contexts.get(run_id)
        if record["status"] in ("queued", "running"):
            context = context or RunContext(
                run_id, record["thread_id"], output_dir, owner_id=app.state.run_store.owner_id
            )
            status = context.stop("cancelled")
            if status in ("cancelled", "timed_out", "interrupted"):
                await app.state.event_sink.finish(context, status, error="Cancelled by user")
            task = app.state.run_tasks.get(run_id)
            if status in ("cancelled", "timed_out", "interrupted") and task is not None and not task.done():
                task.cancel()
        return await app.state.run_store.get(run_id)


@app.get("/api/runs/{run_id}/events")
async def get_run_events(
    run_id: str, after_seq: int = Query(0, ge=0, le=9007199254740991), limit: int = Query(200, ge=1, le=1000)
):
    record = await app.state.run_store.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found")
    events = await app.state.run_store.list_events(run_id, after_seq=after_seq, limit=limit + 1)
    return {
        "run_id": run_id,
        "thread_id": record["thread_id"],
        "events": events[:limit],
        "next_seq": events[min(len(events), limit) - 1]["event_seq"] if events else after_seq,
        "has_more": len(events) > limit,
    }


@app.get("/api/runs/{run_id}/history")
async def get_run_history(run_id: str):
    history = await app.state.run_store.history(run_id)
    if history is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return history


@app.post("/api/upload")
async def upload_files(files: list[UploadFile] = File(...), thread_id: str = Form(...)):
    if not await app.state.run_store.owns_thread(thread_id):
        raise HTTPException(status_code=404, detail="Thread not found")
    try:
        validate_identifier(thread_id)
        names = [validate_filename(file.filename) for file in files]
        if len({name.casefold() for name in names}) != len(names):
            raise PathDenied("Duplicate upload filenames")
        target_dir = confined_path(f"session_{thread_id}", updated_dir)
        targets = [confined_path(name, target_dir) for name in names]
    except PathDenied as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    limit = app.state.runtime_policy.files.max_upload_bytes
    if any(file.size is not None and file.size > limit for file in files):
        raise HTTPException(status_code=413, detail="Upload exceeds the configured size limit")
    target_dir.mkdir(parents=True, exist_ok=True)
    # Stage and validate the whole batch before replacing any existing upload.
    with tempfile.TemporaryDirectory(dir=target_dir, prefix="upload-") as staging:
        for file, name in zip(files, names, strict=True):
            size = 0
            staged = Path(staging) / name
            with staged.open("wb") as stream:
                while chunk := await file.read(65536):
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(status_code=413, detail="Upload exceeds the configured size limit")
                    stream.write(chunk)
        for name, target in zip(names, targets, strict=True):
            confined_path(str(target), updated_dir)
            (Path(staging) / name).replace(target)
    return {"status": "uploaded", "files": names}


async def run_workspace(run_id):
    record = await app.state.run_store.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found")
    try:
        return record, resource_path(record["workspace"], output_dir)
    except PathDenied as exc:
        raise HTTPException(status_code=403, detail="Run workspace rejected") from exc


def _published_run_file(record, base, file):
    """The durable terminal winner owns final-answer publication."""
    if file == base / ".answer.tmp":
        return False
    return file != base / "answer.md" or record["status"] == "completed"


@app.get("/api/runs/{run_id}/files/download")
async def download_file(run_id: str, path: str):
    record, base = await run_workspace(run_id)
    try:
        file = resource_path(path, base)
    except PathDenied as exc:
        raise HTTPException(status_code=403, detail="Relative file path rejected") from exc
    if not _published_run_file(record, base, file) or not file.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file, filename=file.name, media_type="application/octet-stream")


@app.get("/api/runs/{run_id}/files")
async def list_files(run_id: str):
    from utils.artifact_links import artifact_url

    record, base = await run_workspace(run_id)
    if not base.is_dir():
        return {"files": []}
    files = []
    for file in iter_confined_files(base):
        if not _published_run_file(record, base, file):
            continue
        info = file.stat()
        relative = file.relative_to(base).as_posix()
        files.append(
            {
                "name": file.name,
                "relative_path": relative,
                "url": artifact_url(run_id, relative),
                "size": info.st_size,
                "mtime": info.st_mtime,
            }
        )
    files.sort(key=lambda item: item["mtime"], reverse=True)
    return {"files": files}


@app.websocket("/ws/{thread_id}")
async def websocket_endpoint(
    websocket: WebSocket, thread_id: str, run_id: str | None = None, after_seq: int = Query(0, ge=0)
):
    try:
        validate_identifier(thread_id)
    except PathDenied:
        await websocket.close(code=1008)
        return
    if not await app.state.run_store.owns_thread(thread_id) or run_id is None:
        await websocket.close(code=1008)
        return
    if run_id is not None:
        record = await app.state.run_store.get(run_id)
        if record is None or record["thread_id"] != thread_id:
            await websocket.close(code=1008)
            return
    try:
        await manager.connect(websocket, thread_id, run_id=run_id, after_seq=after_seq)
        while True:
            data = await websocket.receive_text()
            await manager.pong(websocket, {"type": "pong", "message": f"服务端已收到: {data}"})
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket, thread_id)


if __name__ == "__main__":
    settings = SecuritySettings.load()
    uvicorn.run(app, host=settings.host, port=settings.port, reload=False, proxy_headers=False, access_log=False)
