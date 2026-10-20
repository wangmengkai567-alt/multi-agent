import asyncio
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
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles as FastAPIStaticFiles
from pydantic import BaseModel, Field, StrictStr, field_validator, model_validator

# Add project root to sys.path
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
sys.path.insert(0, str(project_root))

# Import agent runner and monitor
from agent.main_agent import initialize_agent, run_deep_agent, shutdown_agent  # noqa: E402
from agent.runtime.checkpoints import restore_job_checkpoint  # noqa: E402
from agent.runtime.context import RunContext, RunStopped, reset_run_context, set_run_context  # noqa: E402
from agent.runtime.events import EventSink  # noqa: E402
from agent.runtime.hooks import HookCall, finish_run, pipeline  # noqa: E402
from agent.runtime.jobs import JobService  # noqa: E402
from agent.runtime.policies import load_runtime_policy  # noqa: E402
from agent.runtime.skills import validate_skill_ids  # noqa: E402
from agent.runtime.workflows import finalize_answer  # noqa: E402
from api.monitor import manager  # noqa: E402
from api.run_store import RunConflict, RunStore  # noqa: E402
from utils.path_utils import PathDenied, confined_path, validate_filename, validate_identifier  # noqa: E402
from utils.secure_paths import iter_confined_files  # noqa: E402

logger = logging.getLogger(__name__)
run_db_path = project_root / "data" / "runs.db"


def _task_finished(task, run_id):
    app.state.agent_tasks.discard(task)
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
        if context.resume_graph:
            answer = await run_deep_agent(record["question"], context.thread_id, resume=True)
        else:
            answer = await run_deep_agent(record["question"], context.thread_id)
        context.check_active()
        answer = await finalize_answer(context, answer)
        context.check_active()
        return answer
    finally:
        reset_run_context(token)


async def _execute_run(record, context):
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
        if context.search_tasks or context.approved_internal:
            from agent.runtime.finalization import partial_search_answer

            answer = partial_search_answer(context, "达到执行边界，部分必要资料未核实")
            context.partial_answer = True
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
    app.state.agent_tasks = set()
    app.state.run_tasks = {}
    app.state.run_contexts = {}
    app.state.execution_tasks = set()
    app.state.submission_lock = asyncio.Lock()
    app.state.stopping = False
    app.state.job_service = None
    store = None
    sink = None
    try:
        app.state.runtime_policy = load_runtime_policy()
        await initialize_agent()
        store = await RunStore.open(run_db_path)
        app.state.run_store = store
        sink = EventSink(store, manager.schedule_delivery)
        app.state.event_sink = sink
        await sink.interrupt_active("Service restarted; previous execution was interrupted")
        output_dir.mkdir(exist_ok=True)
        app.state.job_service = await JobService.open(
            run_db_path.with_name("jobs.db"),
            {"research": _research_job, "pdf_conversion": _conversion_job},
            output_dir / "background-jobs",
        )
        await app.state.job_service.recover()
        updated_dir.mkdir(exist_ok=True)
        manager.set_loop(asyncio.get_running_loop())
        manager.store = store
        yield
    finally:
        app.state.stopping = True
        if getattr(app.state, "job_service", None):
            await app.state.job_service.close()
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


app = FastAPI(title="DeepAgents API", lifespan=lifespan)

# 挂载输出目录，以便前端访问生成的静态文件
# 假设输出目录位于项目根目录下的 output
output_dir = project_root / "output"


# 定义上传目录 updated
updated_dir = project_root / "updated"


# 挂载静态文件目录 (前端调试页面)
static_dir = current_dir / "static"

app.mount("/static", FastAPIStaticFiles(directory=str(static_dir)), name="static")

# 挂载输出目录 (前端可直接访问生成的文件)
# Output files are served through the guarded route below.


# 首页
@app.get("/", response_class=HTMLResponse)
async def root():
    index_path = static_dir / "index.html"
    if index_path.exists():
        return index_path.read_text(encoding="utf-8")
    return "<h1>MultiAgent-Search API</h1><p>访问 <a href='/docs'>/docs</a> 查看 API 文档</p>"


# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class TaskRequest(BaseModel):
    query: str = Field(min_length=1)
    skill_ids: list[StrictStr] | None = Field(default=None, max_length=2)
    business_mode: Literal["ordinary", "internal_external"] = "ordinary"
    execution_mode: Literal["auto", "foreground", "background"] = "auto"
    required_sources: list[Literal["db", "search"]] = Field(default_factory=list, max_length=2)
    required_uploads: list[StrictStr] = Field(default_factory=list, max_length=20)
    thread_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")

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
        if self.execution_mode == "background" and (self.business_mode != "ordinary" or self.required_uploads):
            raise ValueError("后台公开调研不接收内部业务流程或上传文件；请使用前台执行")
        if self.business_mode == "internal_external" and "ecommerce-analysis" not in (self.skill_ids or []):
            raise ValueError("internal_external requires explicit ecommerce-analysis selection")
        return self


@app.post("/api/task")
async def run_task(request: TaskRequest):
    thread_id = request.thread_id or str(uuid.uuid4())
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
                RunContext(run_id, thread_id, output_dir), "cancelled", error="Task submission cancelled"
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
            deadline=datetime.now(UTC) + timedelta(seconds=app.state.runtime_policy.timeouts.total_seconds),
            deadline_clock=monotonic() + app.state.runtime_policy.timeouts.total_seconds,
            event_sink=app.state.event_sink,
            policy=app.state.runtime_policy,
            user_query=await resolve_user_goal(thread_id, request.query),
            latest_message=request.query,
            job_service=getattr(app.state, "job_service", None),
            enforce_plan=True,
            execution_mode=request.execution_mode,
            skill_ids=tuple(request.skill_ids or ()),
            business_mode=request.business_mode,
            required_sources=tuple(request.required_sources),
            required_uploads=tuple(request.required_uploads),
        )
        previous_jobs = (
            await app.state.job_service.list_thread(thread_id) if context.user_query != request.query else []
        )
        matching = [job for job in previous_jobs if job["payload"].get("user_query") == context.user_query]
        if matching:
            from tools.job_tools import public_job

            job = matching[0]
            await app.state.job_service.subscribe(job["task_id"], run_id)
            context.pending_jobs[job["task_id"]] = public_job(job)
            await context.event_sink.start(context)
            answer = (job.get("result") or {}).get("answer") or (
                "后台研究仍在进行中。" if job["status"] == "working" else "后台任务状态：" + job["status"]
            )
            await context.event_sink.finish(context, "completed", answer=answer)
            return {
                "status": job["status"],
                "thread_id": thread_id,
                "run_id": run_id,
                "task_id": job["task_id"],
                "poll_after_seconds": job["poll_after_seconds"],
            }
        if request.execution_mode == "background":
            from tools.job_tools import public_job

            await context.event_sink.start(context)
            job = await app.state.job_service.submit(
                thread_id,
                run_id,
                "research",
                {"description": context.user_query, "user_query": context.user_query},
                run_id + ":research",
                context.policy.timeouts.job_seconds,
            )
            context.pending_jobs[job["task_id"]] = public_job(job)
            await context.event_sink.finish(
                context, "completed", answer="研究任务已受理，正在后台执行。本页会自动更新状态和最终结果。"
            )
            return {
                "status": "working",
                "thread_id": thread_id,
                "run_id": run_id,
                "task_id": job["task_id"],
                "poll_after_seconds": 3,
            }
        app.state.run_contexts[run_id] = context
        record = dict(record, question=context.user_query)
        task = asyncio.create_task(_execute_run(record, context), name=f"run:{run_id}")
        app.state.agent_tasks.add(task)
        app.state.run_tasks[run_id] = task
        task.add_done_callback(lambda completed: _task_finished(completed, run_id))
    return {"status": "started", "thread_id": thread_id, "run_id": run_id}


async def resolve_user_goal(thread_id, message):
    # Status follow-ups inherit the actual request, which remains in durable history.
    import re

    if not re.fullmatch(r"\s*(完成了吗|好了吗|做完了吗|继续|继续执行|进度如何|现在怎么样了)[？?！!。\s]*", message):
        return message
    for run_id in reversed(await app.state.run_store.thread_runs(thread_id)):
        previous = await app.state.run_store.get(run_id)
        if (
            previous
            and previous["question"] != message
            and not re.fullmatch(
                r"\s*(完成了吗|好了吗|做完了吗|继续|继续执行|进度如何|现在怎么样了)[？?！!。\s]*", previous["question"]
            )
        ):
            return previous["question"]
    return message


async def _conversion_job(job):
    from agent.runtime.artifact_jobs import convert_document

    return await convert_document(job, app.state.job_service, output_dir)


async def _research_job(job):
    from agent.runtime.checkpoints import save_job_checkpoint

    run_id = str(uuid.uuid4())
    record = await app.state.event_sink.create(job["task_id"], job["payload"]["user_query"], run_id)
    async with app.state.job_service.lock:
        await app.state.job_service.connection.execute(
            "UPDATE jobs SET execution_run_id=? WHERE task_id=?", (run_id, job["task_id"])
        )
        await app.state.job_service.connection.commit()
    policy = app.state.runtime_policy
    seconds = max(0.001, (datetime.fromisoformat(job["expires_at"]) - datetime.now(UTC)).total_seconds())
    policy = policy.model_copy(
        update={
            "timeouts": policy.timeouts.model_copy(update={"total_seconds": seconds}),
            "budgets": policy.job_budgets,
        }
    )
    directory = app.state.job_service.directory / job["task_id"]
    context = RunContext(
        run_id,
        job["task_id"],
        directory,
        deadline=datetime.now(UTC) + timedelta(seconds=seconds),
        deadline_clock=monotonic() + seconds,
        event_sink=app.state.event_sink,
        policy=policy,
        user_query=job["payload"]["user_query"],
        background_job=True,
        enforce_plan=True,
        resume_graph=job["attempts"] > 0,
        job_checkpoint_path=directory / "job-checkpoint.json",
    )
    app.state.job_service.contexts[job["task_id"]] = context
    restore_job_checkpoint(context)
    # Shutdown interrupted execution may safely repeat read-only queries, with original usage retained.
    for task in context.search_tasks.values():
        if task.check_status == "not_checked":
            task.closed_reason = None
    try:
        await _execute_run(dict(record, question=context.user_query), context)
    except Exception as error:
        from agent.runtime.jobs import JobFailed

        saved = await app.state.run_store.get(run_id)
        raise JobFailed(
            str(error),
            {"answer": saved["answer"], "execution_run_id": run_id, "partial": True}
            if saved and saved["answer"]
            else None,
        ) from error
    finally:
        with context.budget_lock:
            save_job_checkpoint(context)
            context.job_checkpoint_closed = True
        app.state.job_service.contexts.pop(job["task_id"], None)
    result = await app.state.run_store.get(run_id)
    if result["status"] != "completed":
        raise RuntimeError(result["error"] or "后台任务未完成")
    return {
        "answer": result["answer"],
        "execution_run_id": run_id,
        "partial": context.delivery_status != "complete"
        or any(t.check_status != "passed" for t in context.search_tasks.values()),
    }


@app.get("/api/runs/{run_id}/jobs")
async def list_run_jobs(run_id: str):
    if await app.state.run_store.get(run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    from tools.job_tools import public_job

    return {"jobs": [public_job(job) for job in await app.state.job_service.list_run(run_id)]}


@app.get("/api/jobs/{task_id}")
async def get_job(task_id: str, thread_id: str):
    from tools.job_tools import public_job

    job = await app.state.job_service.get(task_id, thread_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return public_job(job)


@app.post("/api/jobs/{task_id}/cancel")
async def cancel_job(task_id: str, thread_id: str):
    from tools.job_tools import public_job

    job = await app.state.job_service.cancel(task_id, thread_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return public_job(job)


class JobResume(BaseModel):
    thread_id: str
    user_input: str = Field(min_length=1, max_length=8000)


@app.post("/api/jobs/{task_id}/resume")
async def resume_job(task_id: str, request: JobResume):
    from tools.job_tools import public_job

    try:
        return public_job(await app.state.job_service.resume(task_id, request.thread_id, request.user_input))
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


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
            context = context or RunContext(run_id, record["thread_id"], output_dir)
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


def output_file(value, *, virtual=False):
    try:
        path = confined_path(value, output_dir, virtual=virtual)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        return path
    except PathDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/api/download")
async def download_file(path: str):
    file = output_file(path)
    return FileResponse(file, filename=file.name)


@app.get("/output/{path:path}")
async def static_output(path: str):
    return FileResponse(output_file(path, virtual=True))


@app.get("/api/files")
async def list_files(path: str | None = None):
    try:
        base = confined_path(path or ".", output_dir)
    except PathDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not base.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")
    files = []
    for file in iter_confined_files(base):
        info = file.stat()
        files.append(
            {"name": file.name, "type": "file", "path": str(file), "size": info.st_size, "mtime": info.st_mtime}
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
    if after_seq and run_id is None:
        await websocket.close(code=1008)
        return
    if run_id is not None:
        record = await app.state.run_store.get(run_id)
        if record is None or record["thread_id"] != thread_id:
            await websocket.close(code=1008)
            return
    await manager.connect(websocket, thread_id, run_id=run_id, after_seq=after_seq)
    try:
        while True:
            data = await websocket.receive_text()
            await manager.pong(websocket, {"type": "pong", "message": f"服务端已收到: {data}"})
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket, thread_id)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
