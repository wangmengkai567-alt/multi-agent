"""Program-owned actions after scope locking or successful content review."""

import json
from contextvars import ContextVar
from hashlib import sha256
from urllib.parse import quote
from uuid import uuid4

from langchain.agents.middleware import ModelResponse
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage

from agent.runtime.context import get_run_context
from agent.runtime.contracts import assert_contract
from agent.runtime.hooks import HookToolMiddleware

authorized_action = ContextVar("authorized_delivery_action", default=None)


def _request(tool, args):
    context = get_run_context()
    call_id = str(uuid4())
    with context.budget_lock:
        context.tool_turns[(context.run_id, call_id)] = call_id
    return ToolCallRequest(
        tool_call=dict(name=tool.name, args=args, id=call_id, type="tool_call"), tool=tool, state={}, runtime=None
    )


def _result(message):
    if not isinstance(message, ToolMessage) or message.status != "success":
        raise RuntimeError("Program-owned delivery action did not complete successfully")
    return message.content


def invoke_action(tool, args):
    request = _request(tool, args)
    token = authorized_action.set((tool.name, request.tool_call["id"]))
    try:
        return _result(HookToolMiddleware("delivery").wrap_tool_call(request, lambda r: r.tool.invoke(r.tool_call)))
    finally:
        authorized_action.reset(token)


async def ainvoke_action(tool, args):
    request = _request(tool, args)
    token = authorized_action.set((tool.name, request.tool_call["id"]))
    try:
        return _result(
            await HookToolMiddleware("delivery").awrap_tool_call(request, lambda r: r.tool.ainvoke(r.tool_call))
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

    context.delivery_draft["artifact"] = dict(content_digest=sha256(text.encode()).hexdigest(), **data)
    write_json(context.work_dir / "delivery-artifact.json", context.delivery_draft["artifact"])
    save_job_checkpoint(context)


def _verify_markdown(context, text):
    expected = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n").encode("utf-8")
    if (context.work_dir / "answer.md").read_bytes() != expected:
        raise RuntimeError("Written Markdown differs from the content that passed review")


def _render_artifact(context, contract, text, conversion=None):
    if contract.file_output == "markdown":
        path = (context.work_dir / "answer.md").resolve()
        _save_artifact(context, text, dict(kind="markdown", path="answer.md", status="completed"))
        return text + "\n\n[下载 Markdown](/api/download?path=" + quote(str(path), safe="") + ")"
    if context.job_service is not None:
        job = json.loads(conversion)
        _save_artifact(context, text, dict(kind="pdf", status=job["status"], task_id=job["task_id"]))
        return text + "\n\nPDF 转换任务已提交，页面将更新转换结果。任务：" + job["task_id"]
    path = context.work_dir / "answer.pdf"
    if not path.is_file() or not path.stat().st_size:
        raise RuntimeError("PDF conversion did not produce the requested artifact")
    _save_artifact(context, text, dict(kind="pdf", path="answer.pdf", status="completed"))
    return text + "\n\n[下载 PDF](/api/download?path=" + quote(str(path.resolve()), safe="") + ")"


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
