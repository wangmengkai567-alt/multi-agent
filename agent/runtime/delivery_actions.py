"""Program-owned actions after scope locking or successful content review."""

import json
from contextvars import ContextVar
from hashlib import sha256
from uuid import uuid4

from langchain.agents.middleware import ModelResponse
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage

from agent.runtime.context import get_run_context
from agent.runtime.contracts import assert_contract
from agent.runtime.hooks import HookToolMiddleware, error_details, error_message
from utils.artifact_links import artifact_url

authorized_action = ContextVar("authorized_delivery_action", default=None)


class DeliveryActionError(RuntimeError):
    def __init__(self, details):
        self.details = details
        super().__init__(
            f"Delivery {details['tool_name']} failed ({details['error_category']}, "
            f"stage={details['stage']}, call_id={details['call_id']}): {details['message']}"
        )


def _signature(args):
    return sha256(json.dumps(args, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _saved_action(tool, args):
    context = get_run_context()
    context.check_active()
    draft = context.delivery_draft
    row = draft.get("actions", {}).get(tool.name) if draft else None
    if row is None:
        return None
    if row["args_digest"] != _signature(args):
        raise RuntimeError("Delivery action input changed after its first admission")
    if row["result_status"] != "accepted":
        raise DeliveryActionError(
            row.get("error")
            or dict(
                error_category="outcome_unknown",
                stage="execution",
                tool_name=tool.name,
                call_id=row["call_id"],
                message="Previous delivery action has no accepted result; automatic replay is forbidden",
            )
        )
    if row.get("file_digest"):
        path = context.work_dir / ("answer.md" if tool.name == "generate_markdown" else "answer.pdf")
        if not path.is_file() or sha256(path.read_bytes()).hexdigest() != row["file_digest"]:
            raise _artifact_error(context, tool.name, "Previously completed delivery file is missing or changed")
    return row


def _artifact_error(
    context, tool_name, message, *, category="artifact_integrity", stage="artifact_validation", task_id=None
):
    row = context.delivery_draft["actions"][tool_name]
    details = dict(
        error_category=category,
        stage=stage,
        tool_name=tool_name,
        call_id=row["call_id"],
        message=message,
    )
    if task_id is not None:
        details["task_id"] = task_id
    context.delivery_draft["delivery_error"] = details
    return DeliveryActionError(details)


def record_action(call):
    context = get_run_context()
    if not context or not context.delivery_draft or not action_authorized(call):
        return
    row = context.delivery_draft.get("actions", {}).get(call.actor.rsplit(":", 1)[-1])
    if row is None or row["call_id"] != call.call_id:
        return
    row.update(execution_status=call.execution_status, result_status=call.result_status, stage=call.stage)
    if call.error is not None:
        row["error"] = dict(error_details(call), message=error_message(call.error))
        context.delivery_draft["delivery_error"] = row["error"]
    if call.result_status == "accepted" and isinstance(call.result, ToolMessage):
        row["result"] = call.result.content
        filename = {"generate_markdown": "answer.md", "convert_md_to_pdf": "answer.pdf"}.get(
            call.actor.rsplit(":", 1)[-1]
        )
        if filename and (context.work_dir / filename).is_file():
            row["file_digest"] = sha256((context.work_dir / filename).read_bytes()).hexdigest()
    from agent.runtime.checkpoints import save_job_checkpoint

    save_job_checkpoint(context)


def _request(tool, args):
    context = get_run_context()
    call_id = str(uuid4())
    with context.budget_lock:
        context.check_active()
        if context.delivery_draft is not None:
            actions = context.delivery_draft.setdefault("actions", {})
            if tool.name in actions:
                raise RuntimeError("Delivery action was already admitted; concurrent replay is forbidden")
            actions[tool.name] = dict(
                call_id=call_id,
                args_digest=_signature(args),
                execution_status="not_started",
                result_status="unavailable",
                stage="admission",
            )
            from agent.runtime.checkpoints import save_job_checkpoint

            save_job_checkpoint(context)
        context.tool_turns[(context.run_id, call_id)] = call_id
    return ToolCallRequest(
        tool_call=dict(name=tool.name, args=args, id=call_id, type="tool_call"), tool=tool, state={}, runtime=None
    )


def _result(message, request):
    if (
        not isinstance(message, ToolMessage)
        or message.status != "success"
        or message.tool_call_id != request.tool_call["id"]
    ):
        context = get_run_context()
        draft = context.delivery_draft or {}
        row = draft.get("actions", {}).get(request.tool.name, {})
        details = row.get("error")
        if details is None and isinstance(message, ToolMessage) and message.tool_call_id == request.tool_call["id"]:
            details = message.additional_kwargs.get("runtime_error")
        raise DeliveryActionError(
            details
            or dict(
                error_category="tool_response_error",
                stage="result_validation",
                tool_name=request.tool.name,
                call_id=request.tool_call["id"],
                message="Program-owned tool did not return a successful result",
            )
        )
    return message.content


def invoke_action(tool, args):
    saved = _saved_action(tool, args)
    if saved is not None:
        return saved["result"]
    request = _request(tool, args)
    token = authorized_action.set((tool.name, request.tool_call["id"]))
    try:
        return _result(
            HookToolMiddleware("delivery").wrap_tool_call(request, lambda r: r.tool.invoke(r.tool_call)), request
        )
    finally:
        authorized_action.reset(token)


async def ainvoke_action(tool, args):
    saved = _saved_action(tool, args)
    if saved is not None:
        return saved["result"]
    request = _request(tool, args)
    token = authorized_action.set((tool.name, request.tool_call["id"]))
    try:
        return _result(
            await HookToolMiddleware("delivery").awrap_tool_call(request, lambda r: r.tool.ainvoke(r.tool_call)),
            request,
        )
    finally:
        authorized_action.reset(token)


def action_authorized(call):
    return authorized_action.get() == (call.actor.rsplit(":", 1)[-1], call.call_id)


def _artifact_input(text):
    context = get_run_context()
    contract = assert_contract(context)
    if contract.file_output == "none":
        return None
    if (
        context.background_job
        or context.delivery_draft is None
        or context.delivery_draft.get("reviewed_content_digest") != sha256(text.encode()).hexdigest()
    ):
        raise RuntimeError("Delivery file requires exactly the content that passed review")
    return context, contract


def _save_artifact(context, text, data):
    from agent.runtime.checkpoints import save_job_checkpoint, write_json

    context.check_active()
    if data["status"] == "completed":
        data["file_digest"] = sha256((context.work_dir / data["path"]).read_bytes()).hexdigest()
    tool_name = "generate_markdown" if data["kind"] == "markdown" else "convert_md_to_pdf"
    data["call_id"] = context.delivery_draft["actions"][tool_name]["call_id"]
    context.delivery_draft["artifact"] = dict(content_digest=sha256(text.encode()).hexdigest(), **data)
    write_json(context.work_dir / "delivery-artifact.json", context.delivery_draft["artifact"])
    save_job_checkpoint(context)


def _verify_markdown(context, text):
    expected = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n").encode("utf-8")
    path = context.work_dir / "answer.md"
    if not path.is_file() or path.read_bytes() != expected:
        raise _artifact_error(
            context, "generate_markdown", "Written Markdown differs from the content that passed review"
        )
    context.delivery_draft["reviewed_markdown_digest"] = sha256(expected).hexdigest()


def _render_artifact(context, contract, text, conversion=None):
    if contract.file_output == "markdown":
        _save_artifact(context, text, dict(kind="markdown", path="answer.md", status="completed"))
        return text + "\n\n[下载 Markdown](" + artifact_url(context.run_id, "answer.md") + ")"
    if context.job_service is not None:
        job = json.loads(conversion)
        if job["task_id"] not in context.pending_jobs or job["kind"] != "pdf_conversion":
            raise _artifact_error(context, "convert_md_to_pdf", "PDF response is not an owned conversion task")
        if job["status"] == "completed":
            path = context.work_dir / "answer.pdf"
            if not path.is_file() or not path.stat().st_size:
                raise _artifact_error(context, "convert_md_to_pdf", "Completed conversion has no published PDF")
            _save_artifact(context, text, dict(kind="pdf", path="answer.pdf", status="completed"))
            return text + "\n\n[下载 PDF](" + artifact_url(context.run_id, "answer.pdf") + ")"
        if job["status"] != "working":
            raise _artifact_error(
                context,
                "convert_md_to_pdf",
                "PDF conversion did not complete: " + str(job.get("error") or job["status"]),
                category="conversion_failed",
                stage="background_conversion",
                task_id=job["task_id"],
            )
        _save_artifact(context, text, dict(kind="pdf", status=job["status"], task_id=job["task_id"]))
        return text + "\n\nPDF 转换任务已提交，页面将更新转换结果。任务：" + job["task_id"]
    path = context.work_dir / "answer.pdf"
    if not path.is_file() or not path.stat().st_size:
        raise _artifact_error(context, "convert_md_to_pdf", "PDF conversion did not produce the requested artifact")
    _save_artifact(context, text, dict(kind="pdf", path="answer.pdf", status="completed"))
    return text + "\n\n[下载 PDF](" + artifact_url(context.run_id, "answer.pdf") + ")"


def deliver_file(text):
    value = _artifact_input(text)
    if value is None:
        return text
    context, contract = value
    from tools.markdown_tools import generate_markdown
    from tools.pdf_tools import convert_md_to_pdf

    invoke_action(generate_markdown, dict(content=text, filename="answer.md"))
    _verify_markdown(context, text)
    conversion = (
        invoke_action(convert_md_to_pdf, dict(md_filename="answer.md", pdf_filename="answer.pdf"))
        if contract.file_output == "pdf"
        else None
    )
    return _render_artifact(context, contract, text, conversion)


async def adeliver_file(text):
    value = _artifact_input(text)
    if value is None:
        return text
    context, contract = value
    from tools.markdown_tools import generate_markdown
    from tools.pdf_tools import convert_md_to_pdf

    await ainvoke_action(generate_markdown, dict(content=text, filename="answer.md"))
    _verify_markdown(context, text)
    conversion = (
        await ainvoke_action(convert_md_to_pdf, dict(md_filename="answer.md", pdf_filename="answer.pdf"))
        if contract.file_output == "pdf"
        else None
    )
    return _render_artifact(context, contract, text, conversion)


async def submit_background():
    from tools.job_tools import submit_research_job

    context = get_run_context()
    contract = assert_contract(context)
    await ainvoke_action(submit_research_job, dict(description=contract.operation))
    return ModelResponse(
        result=[
            AIMessage(
                content="研究任务已受理，正在后台执行。本页将更新状态和最终结果。",
                additional_kwargs={"system_notice": True},
            )
        ]
    )


def submit_background_sync():
    from agent.results import sync_result

    return sync_result(submit_background(), get_run_context().event_sink)
