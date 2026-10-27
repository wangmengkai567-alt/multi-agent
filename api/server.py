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
from agent.runtime.checkpoints import restore_job_checkpoint  # noqa: E402
from agent.runtime.context import RunContext, RunStopped, reset_run_context, set_run_context  # noqa: E402
from agent.runtime.events import EventSink  # noqa: E402
from agent.runtime.hooks import HookCall, finish_run, pipeline  # noqa: E402
from agent.runtime.jobs import JobService  # noqa: E402
from agent.runtime.policies import load_runtime_policy  # noqa: E402
from agent.runtime.skills import SKILL_IDS, required_skill_ids, skill_catalog, validate_skill_ids  # noqa: E402
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
    app.state.job_service = None
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
        app.state.job_service = await JobService.open(
            run_db_path.with_name("jobs.db"),
            {"research": _research_job},
            output_dir / "background-jobs",
            scheduler=app.state.scheduler,
            run_store=store,
        )
        await initialize_agent()
        await sink.interrupt_active("Service restarted; previous execution was interrupted")
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
    execution_mode: Literal["auto", "foreground", "background"] = "auto"
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
            self.execution_mode == "background"
            or "search" in self.required_sources
            or self.business_mode == "internal_external"
        ):
            raise ValueError("禁止联网与公开研究要求冲突；请调整显式执行约束")
        if self.execution_mode == "background" and (self.business_mode != "ordinary" or self.required_uploads):
            raise ValueError("后台公开调研不接收内部业务流程或上传文件；请使用前台执行")
        missing = set(required_skill_ids(self.business_mode)) - set(self.skill_ids or ())
        if missing:
            raise ValueError(f"{self.business_mode} requires explicit skill selection: {', '.join(sorted(missing))}")
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
            user_query=request.query
            if request.input_text is not None
            else await resolve_user_goal(thread_id, request.query),
            input_text=request.input_text,
            network_policy=request.network_policy,
            latest_message=request.query,
            job_service=getattr(app.state, "job_service", None),
            enforce_plan=True,
            execution_mode=request.execution_mode,
            skill_ids=tuple(request.skill_ids or ()),
            business_mode=request.business_mode,
            required_sources=tuple(request.required_sources),
            required_uploads=tuple(request.required_uploads),
        )
        for previous_id in reversed(await app.state.run_store.thread_runs(thread_id)):
            previous = await app.state.run_store.get(previous_id)
            if previous_id == run_id or previous["status"] != "completed" or not previous.get("answer"):
                continue
            events = await app.state.run_store.list_events(previous_id)
            if any(e["event"] == "answer_validation" and e["status"] == "passed" for e in events):
                context.previous_run_id, context.previous_answer = previous_id, previous["answer"]
                break
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


async def _research_job(job):
    from agent.runtime.checkpoints import save_job_checkpoint
    from agent.runtime.contracts import CONTRACT_VERSION, TaskContract, background_eligible
    from agent.runtime.jobs import JobFailed

    if job["payload"].get("contract_version") != CONTRACT_VERSION:
        raise JobFailed("旧后台任务缺少当前执行契约，保留记录并停止；请新建任务")
    saved_contract = TaskContract.model_validate_json(json.dumps(job["payload"]["task_contract"]))
    if not background_eligible(saved_contract):
        raise JobFailed("后台研究契约不允许执行")
    run_id = str(uuid.uuid4())
    if job["resource_owner_id"] != app.state.run_store.owner_id:
        raise JobFailed("Background task resource owner mismatch")
    await app.state.run_store.create_thread(job["task_id"])
    record = await app.state.event_sink.create(
        job["task_id"], job["payload"]["user_query"], run_id, job_id=job["task_id"]
    )
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
        owner_id=job["resource_owner_id"],
        deadline=datetime.now(UTC) + timedelta(seconds=seconds),
        deadline_clock=monotonic() + seconds,
        event_sink=app.state.event_sink,
        policy=policy,
        user_query=job["payload"]["user_query"],
        input_text=job["payload"]["input_text"],
        network_policy=job["payload"]["network_policy"],
        required_sources=tuple(job["payload"]["required_sources"]),
        task_contract=saved_contract,
        background_job=True,
        scheduler=app.state.scheduler,
        research_scope_id=job["task_id"],
        enforce_plan=True,
        job_checkpoint_path=directory / "job-checkpoint.json",
    )
    app.state.job_service.contexts[job["task_id"]] = context
    checkpoint_loaded = False
    try:
        if job["attempts"] > 0 and not context.job_checkpoint_path.is_file():
            raise RunStopped("当前任务检查点缺失，无法证明剩余预算；拒绝自动重新开始")
        restore_job_checkpoint(context)
        if not context.recovering:
            from agent.runtime.capacity import initialize

            initialize(context, saved_contract)
        if context.resume_source_run_id:
            previous = await app.state.run_store.get(context.resume_source_run_id)
            if not previous or previous["thread_id"] != context.thread_id:
                raise RunStopped("Checkpoint source run does not belong to the owned execution thread")
        from agent.runtime.recovery_state import reconcile_result_history
        from agent.runtime.review_state import ReviewState

        await reconcile_result_history(context)
        from agent.runtime.recovery_state import result_review_state

        for task_id, task in context.search_tasks.items():
            if task.current_result_id:
                persisted = await app.state.run_store.get_current_result(
                    task.current_result_id, task.result_run_id, context.thread_id
                )
                if (
                    persisted is None
                    or result_review_state(context, persisted, task_id) != task.state
                    or persisted["review"].get("accepted_report") != task.report
                ):
                    context.stop("interrupted")
                    raise RunStopped("恢复结果与数据库权威快照不一致")
                context.recovered_results[persisted["result_id"]] = persisted
        for item in context.approved_internal.values():
            import copy

            persisted = await app.state.run_store.get_current_result(
                item["result_id"], item["result_run_id"], context.thread_id
            )
            if persisted is None or result_review_state(
                context, persisted, item["task_id"]
            ) != ReviewState.model_validate(item["state"]):
                context.stop("interrupted")
                raise RunStopped("恢复内部结果与数据库当前快照不一致")
            report = copy.deepcopy(persisted["review"].get("accepted_report"))
            if report is None:
                raise RunStopped("恢复内部结果缺少已审核报告")
            mapping = {f["id"]: f"{item['task_id']}:{item['report_attempt']}:{f['id']}" for f in report["facts"]}
            for fact in report["facts"]:
                fact["id"] = mapping[fact["id"]]
            for analysis in report["analysis"]:
                analysis["fact_ids"] = [mapping[k] for k in analysis["fact_ids"]]
            if report != item["report"]:
                context.stop("interrupted")
                raise RunStopped("恢复内部报告内容与数据库不一致")
            context.recovered_results[persisted["result_id"]] = persisted
        from agent.results import ResultService

        for result_id in context.recovered_results:
            await ResultService().read_raw(context, result_id)
        from agent.runtime.measurement import persist

        checkpoint_loaded = True
        persist(context)
        await _execute_run(dict(record, question=context.user_query), context)
    except Exception as error:
        from agent.runtime.jobs import JobFailed

        saved = await app.state.run_store.get(run_id)
        if saved and saved["status"] in ("queued", "running"):
            await context.event_sink.finish(context, "interrupted", error=str(error))
        raise JobFailed(
            str(error),
            {"answer": saved["answer"], "execution_run_id": run_id, "partial": True}
            if saved and saved["answer"]
            else None,
        ) from error
    finally:
        with context.budget_lock:
            if checkpoint_loaded:
                save_job_checkpoint(context)
            context.job_checkpoint_closed = True
        app.state.job_service.contexts.pop(job["task_id"], None)
    result = await app.state.run_store.get(run_id)
    if result["status"] != "completed":
        raise RuntimeError(result["error"] or "后台任务未完成")
    return {
        "answer": result["answer"],
        "execution_run_id": run_id,
        "partial": context.coverage != "complete",
        "coverage": context.coverage,
        "completion": result["completion"],
    }


async def _with_completion(record, jobs=None):
    from agent.runtime.completion import resolve_completion

    if record is None:
        return None
    if jobs is None:
        jobs = await app.state.job_service.list_run(record["run_id"])
    return dict(record, completion=resolve_completion(record["completion"], jobs))


@app.get("/api/runs/{run_id}/jobs")
async def list_run_jobs(run_id: str):
    record = await app.state.run_store.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found")
    from tools.job_tools import public_job

    jobs = await app.state.job_service.list_run(run_id)
    record = await _with_completion(record, jobs)
    return {
        "jobs": [public_job(job) for job in jobs],
        "completion": record["completion"],
        "status_seq": record["terminal_event_seq"],
    }


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
    model_config = ConfigDict(extra="forbid")
    thread_id: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    user_input: StrictStr = Field(min_length=1, max_length=8000)


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
    return await _with_completion(record)


def _requirement_context(run_id):
    context = app.state.run_contexts.get(run_id)
    if context is None:
        service = getattr(app.state, "job_service", None)
        context = (
            next((value for value in list(service.contexts.values()) if value.run_id == run_id), None)
            if service is not None
            else None
        )
    return context


@app.get("/api/runs/{run_id}/requirements")
async def get_requirements(run_id: str):
    from agent.runtime.requirement_changes import requirement_snapshot

    record = await app.state.run_store.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found")
    context = _requirement_context(run_id)
    if context is not None:
        with context.budget_lock:
            return requirement_snapshot(context)
    path = resource_path("requirement-state.json", resource_path(record["workspace"], output_dir))
    if not path.is_file():
        raise HTTPException(status_code=409, detail="No persisted requirement snapshot is available")
    return json.loads(path.read_text(encoding="utf-8"))


from agent.runtime.requirement_changes import RequirementChange  # noqa: E402


@app.post("/api/runs/{run_id}/requirements")
async def change_requirements(run_id: str, change: RequirementChange):
    from agent.runtime.requirement_changes import stage_change

    async with app.state.submission_lock:
        record = await app.state.run_store.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Run not found")
        context = _requirement_context(run_id)
        if record["status"] != "running" or context is None:
            raise HTTPException(status_code=409, detail="Requirement changes need the live running context")
        try:
            return stage_change(context, change)
        except (ValueError, RunStopped) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error


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
        return await _with_completion(await app.state.run_store.get(run_id))


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
    history["run"] = await _with_completion(history["run"])
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
        return resource_path(record["workspace"], output_dir)
    except PathDenied as exc:
        raise HTTPException(status_code=403, detail="Run workspace rejected") from exc


@app.get("/api/runs/{run_id}/files/download")
async def download_file(run_id: str, path: str):
    base = await run_workspace(run_id)
    try:
        file = resource_path(path, base)
    except PathDenied as exc:
        raise HTTPException(status_code=403, detail="Relative file path rejected") from exc
    if not file.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file, filename=file.name, media_type="application/octet-stream")


@app.get("/api/runs/{run_id}/files")
async def list_files(run_id: str):
    from utils.artifact_links import artifact_url

    base = await run_workspace(run_id)
    if not base.is_dir():
        return {"files": []}
    files = []
    for file in iter_confined_files(base):
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
