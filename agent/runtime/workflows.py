"""Server-owned prerequisites for explicitly requested workflows and report inputs."""

import json
from hashlib import sha256
from pathlib import Path

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent.runtime.context import get_run_context
from agent.runtime.events import EventPersistenceError, make_event
from utils.secure_paths import confined_path, resolve_run_path, validate_filename

SOURCES = ("db", "search")
OUTPUT_TOOLS = ("generate_markdown", "convert_md_to_pdf", "write_file", "edit_file")
DEGRADED_NOTICE = (
    "【降级分析】数据库依赖不可用，本轮没有可用内部经营指标。以下仅为外部检索材料，不构成内部数据与市场的有效对照。"
)


def guarded(context):
    return context is not None and bool(
        context.business_mode == "internal_external" or context.required_sources or context.required_uploads
    )


def initialize_workflow(context=None):
    context = context or get_run_context()
    if context is None:
        return
    if context.business_mode not in ("ordinary", "internal_external"):
        raise ValueError("Unknown business mode")
    if guarded(context) and context.event_sink is None:
        raise refusal("storage_required", "当前流程需要可追溯的本轮结果存储。")
    if context.business_mode == "internal_external" and "ecommerce-analysis" not in context.skill_ids:
        raise ValueError("internal_external requires explicit ecommerce-analysis selection")
    if set(context.required_sources) - set(SOURCES) or len(set(context.required_sources)) != len(
        context.required_sources
    ):
        raise ValueError("Invalid required sources")
    for name in context.required_uploads:
        validate_filename(name)
    with context.budget_lock:
        if context.workflow is None and guarded(context):
            context.workflow = dict(mode=context.business_mode, db="pending", search="pending", reports={})
            emit(
                context,
                "initialized",
                required_sources=required_sources(context),
                required_uploads=list(context.required_uploads),
            )


def before_run_workflow(call):
    initialize_workflow()


def required_sources(context):
    return sorted(
        set(context.required_sources) | ({"db", "search"} if context.business_mode == "internal_external" else set())
    )


def emit(context, status, call=None, **data):
    if context.event_sink is not None:
        context.event_sink.submit(
            make_event(
                context.thread_id,
                context.run_id,
                "workflow_state",
                "流程状态：" + status,
                dict(business_mode=context.business_mode, **data),
                actor=call.actor if call else "main",
                status=status,
                call_id=call.call_id if call else context.run_id,
                parent_call_id=call.parent_call_id if call else None,
            )
        )


def refusal(code, next_step, **details):
    from agent.runtime.hooks import HookRejected

    return HookRejected(
        json.dumps(dict(error="workflow_precondition", code=code, next_step=next_step, **details), ensure_ascii=False)
    )


def upload_requirements(context):
    required = set(context.required_uploads)
    directory = confined_path("uploads", context.work_dir)
    if context.business_mode == "internal_external" and directory.exists():
        required.update(file.name for file in directory.iterdir() if file.is_file())
    unread = []
    for name in sorted(required):
        file = confined_path("uploads/" + name, context.work_dir)
        if not file.is_file() or context.upload_reads.get(str(file)) != sha256(file.read_bytes()).hexdigest():
            unread.append(name)
    return unread


async def snapshot(context):
    """Rebuild facts from this run's durable results; never trust model descriptions or state flags."""
    from agent.results import ResultService

    sink = context.event_sink
    if sink is None:
        raise EventPersistenceError("Workflow prerequisites require durable run results")
    await sink.flush(context.run_id)
    run = await sink.store.get(context.run_id)
    if run is None or run["thread_id"] != context.thread_id or run["status"] not in ("queued", "running"):
        raise refusal("run_ended", "本轮已结束，不再启动新动作。")
    usable = await sink.store.list_results(context.run_id, thread_id=context.thread_id, usable_only=True)
    for row in usable:
        await ResultService().read_raw(context, row["result_id"])
    all_rows = await sink.store.list_results(context.run_id, thread_id=context.thread_id)
    tasks = await sink.store.list_subtasks(context.run_id)
    active_db = any(t["target"] == "db" and t["status"] in ("queued", "running") for t in tasks)
    ended = {t["call_id"] for t in tasks if t["status"] in ("completed", "failed")}
    source_ids = {source: [r["result_id"] for r in usable if r["source"] == source] for source in SOURCES}
    failures = [
        r["result_id"]
        for r in all_rows
        if r["source"] == "db" and r["check_status"] == "dependency_unavailable" and r["call_id"] in ended
    ]
    degraded = (
        context.business_mode == "internal_external" and not source_ids["db"] and bool(failures) and not active_db
    )
    events = await sink.store.list_events(context.run_id)
    linked = set()
    for event in events:
        if event["event"] != "result_handoff":
            continue
        data = event["data"]
        if (degraded and data.get("degraded") and set(data.get("db_failure_ids", [])) & set(failures)) or (
            source_ids["db"] and set(data.get("result_ids", [])) & set(source_ids["db"])
        ):
            linked.add(event["call_id"])
    searches = [r for r in usable if r["source"] == "search" and r["call_id"] in linked]
    missing = [s for s in required_sources(context) if not source_ids[s] and not (s == "db" and degraded)]
    if context.business_mode == "internal_external" and not searches and "search" not in missing:
        missing.append("search")
    if context.workflow is not None and context.business_mode == "internal_external":
        with context.budget_lock:
            context.workflow.update(
                db="passed" if source_ids["db"] else "degraded" if degraded else "pending",
                search="passed" if searches else "pending",
            )
    return dict(
        db_result_ids=source_ids["db"],
        db_failure_ids=failures if degraded else [],
        degraded=degraded,
        source_ids=source_ids,
        linked_search_ids=[r["result_id"] for r in searches],
        missing_sources=missing,
        unread_uploads=upload_requirements(context),
        external_summaries=[r["summary"] for r in searches],
    )


async def prepare_delegate(context, call):
    initialize_workflow(context)
    if context.business_mode == "internal_external" and call.target == "search":
        call.workflow_data = await snapshot(context)


def before_delegate_workflow(call):
    context = get_run_context()
    if context is None or context.business_mode != "internal_external" or call.target != "search":
        return
    data = call.workflow_data
    if not data or not (data.get("db_result_ids") or data.get("degraded")):
        emit(context, "blocked", call, code="database_required")
        raise refusal(
            "database_required",
            "先调用 task(db) 并等待本轮数据库结果检查通过，再调用 search；不得用口述或旧轮次 ID 代替。",
        )
    with context.budget_lock:
        context.workflow.update(db="degraded" if data["degraded"] else "passed", search="running")
    emit(
        context,
        "degraded" if data["degraded"] else "search_allowed",
        call,
        db_result_ids=data["db_result_ids"],
        db_failure_ids=data["db_failure_ids"],
    )


def output_call(call):
    return call.actor.rsplit(":", 1)[-1] in OUTPUT_TOOLS


async def prepare_tool(call):
    context = get_run_context()
    if guarded(context) and output_call(call):
        initialize_workflow(context)
        call.workflow_data = await snapshot(context)


def prepare_tool_sync(call):
    from agent.results import sync_result

    context = get_run_context()
    if guarded(context) and output_call(call):
        if context.event_sink is None:
            raise refusal("storage_required", "当前流程需要可追溯的本轮结果存储。")
        sync_result(prepare_tool(call), context.event_sink)


def report_check(context, data, call=None):
    if not data or data["missing_sources"] or data["unread_uploads"]:
        missing = data.get("missing_sources", required_sources(context)) if data else required_sources(context)
        unread = data.get("unread_uploads", list(context.required_uploads)) if data else list(context.required_uploads)
        emit(context, "blocked", call, code="report_not_ready", missing_sources=missing, unread_uploads=unread)
        raise refusal(
            "report_not_ready",
            "先完成所需查询并通过检查，读取本轮要求的上传文件后再生成报告或结束任务。",
            missing_sources=missing,
            unread_uploads=unread,
        )


def degraded_content(data):
    parts = [DEGRADED_NOTICE, "外部检索助手返回的材料（未完成内部数据对照）："]
    for text in data["external_summaries"]:
        parts.append("\n".join("> " + line for line in text.splitlines()))
    parts.append("数据库失败记录：" + ", ".join(data["db_failure_ids"]))
    parts.append("外部结果引用：" + ", ".join(data["linked_search_ids"]))
    return "\n\n".join(parts)


def before_tool_workflow(call):
    context = get_run_context()
    if not guarded(context) or not output_call(call):
        return
    data = call.workflow_data
    report_check(context, data, call)
    name = call.actor.rsplit(":", 1)[-1]
    if data["degraded"]:
        if name == "edit_file":
            raise refusal("degraded_rewrite", "降级报告必须重新生成，不能编辑可能含内部指标的旧正文。")
        if name == "convert_md_to_pdf":
            source = resolve_run_path(str(Path(call.args["md_filename"]).with_suffix(".md")))
            if (
                not source.is_file()
                or context.workflow["reports"].get(str(source)) != sha256(source.read_bytes()).hexdigest()
            ):
                raise refusal("degraded_source", "先生成注明数据库不可用的降级 Markdown，再转换 PDF。")
        else:
            call.args["content"] = degraded_content(data)
    emit(context, "report_allowed", call, source_ids=data["source_ids"], degraded=data["degraded"])


def after_tool_workflow(call):
    context = get_run_context()
    if not guarded(context) or not call.executed or call.status != "completed":
        return
    messages = [call.result] if isinstance(call.result, ToolMessage) else []
    if isinstance(call.result, Command) and isinstance(call.result.update, dict):
        messages = call.result.update.get("messages", [])
    if isinstance(messages, ToolMessage):
        messages = [messages]
    if not any(isinstance(m, ToolMessage) and m.content and not str(m.content).startswith("Error") for m in messages):
        return
    name = call.actor.rsplit(":", 1)[-1]
    if name in ("read_file", "read_file_content"):
        value = call.args.get("file_path" if name == "read_file" else "filename")
        file = resolve_run_path(value, virtual=name == "read_file")
        if not file.is_relative_to((context.work_dir / "uploads").resolve()) or not file.is_file():
            return
        raw = file.read_bytes()
        digest = sha256(raw).hexdigest()
        if name == "read_file":
            try:
                total = len(raw.decode("utf-8").splitlines())
            except UnicodeError:
                return
            offset, limit = call.args.get("offset", 0), call.args.get("limit", 2000)
            with context.budget_lock:
                coverage = context.upload_coverage.setdefault((str(file), digest), set())
                coverage.update(range(max(0, offset), min(total, offset + limit)))
                if len(coverage) != total or not total:
                    return
        with context.budget_lock:
            context.upload_reads[str(file)] = digest
        emit(context, "upload_read", call, file_sha256=digest)
    elif output_call(call) and call.workflow_data.get("degraded") and name in ("write_file", "generate_markdown"):
        if name == "write_file":
            file = resolve_run_path(call.args["file_path"], virtual=True)
        else:
            filename = call.args["filename"]
            file = resolve_run_path(
                str(Path(call.args.get("path") or "") / (filename if filename.endswith(".md") else filename + ".md"))
            )
        if file.is_file():
            with context.budget_lock:
                context.workflow["reports"][str(file)] = sha256(file.read_bytes()).hexdigest()


async def finalize_answer(context, answer):
    if not guarded(context):
        return answer
    initialize_workflow(context)
    data = await snapshot(context)
    report_check(context, data)
    emit(
        context,
        "completed_degraded" if data["degraded"] else "completed",
        source_ids=data["source_ids"],
        db_failure_ids=data["db_failure_ids"],
    )
    return degraded_content(data) if data["degraded"] else answer


def instruction(context):
    if not guarded(context):
        return ""
    return "\n【服务端业务要求】" + json.dumps(
        dict(
            business_mode=context.business_mode,
            required_sources=required_sources(context),
            required_uploads=list(context.required_uploads),
            rule=(
                "本轮内外对照先取得合格数据库 result_id 再搜索；依赖不可用时仅做明确降级。"
                if context.business_mode == "internal_external"
                else "本轮普通任务自由选择助手。"
            )
            + "报告或结束任务前完成所需查询与上传文件读取。",
        ),
        ensure_ascii=False,
    )
