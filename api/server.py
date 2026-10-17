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
from agent.runtime.context import RunContext, RunStopped, reset_run_context, set_run_context  # noqa: E402
from agent.runtime.events import EventSink  # noqa: E402
from agent.runtime.hooks import HookCall, finish_run, pipeline  # noqa: E402
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
        await initialize_agent()
        store = await RunStore.open(run_db_path)
        app.state.run_store = store
        sink = EventSink(store, manager.schedule_delivery)
        app.state.event_sink = sink
        await sink.interrupt_active("Service restarted; previous execution was interrupted")
        output_dir.mkdir(exist_ok=True)
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
    required_sources: list[Literal["db", "search", "knowledge"]] = Field(default_factory=list, max_length=3)
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
            skill_ids=tuple(request.skill_ids or ()),
            business_mode=request.business_mode,
            required_sources=tuple(request.required_sources),
            required_uploads=tuple(request.required_uploads),
        )
        app.state.run_contexts[run_id] = context
        record = dict(record, question=request.query)
        task = asyncio.create_task(_execute_run(record, context), name=f"run:{run_id}")
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
