"""Capture actual tool results in the current delegation; no generated fact protocol."""

import re
from contextvars import ContextVar

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent.runtime.context import get_run_context

tool_evidence = ContextVar("delegated_tool_evidence", default=None)


# 将当前工具调用的消息和 artifact 归入所属子任务，并保存逐次调用的证据记录。
def capture_tool(call):
    collector = tool_evidence.get()
    if collector is None:
        return
    from agent.runtime.evidence_protocol import adapter_by_id, make_record

    if call.evidence_adapter is not None and not adapter_by_id(call.evidence_adapter).capture:
        return
    messages = [call.result] if isinstance(call.result, ToolMessage) else []
    if isinstance(call.result, Command) and isinstance(call.result.update, dict):
        value = call.result.update.get("messages", [])
        messages = [value] if isinstance(value, ToolMessage) else value if isinstance(value, list) else []
    matches = [
        message for message in messages if isinstance(message, ToolMessage) and message.tool_call_id == call.call_id
    ]
    if call.status == "completed" and len(matches) != 1:
        from agent.runtime.evidence_protocol import EvidenceError

        raise EvidenceError("Successful tool must return exactly one owned ToolMessage")
    message = matches[0] if matches else None
    item = make_record(call, message.content if message else None, message.artifact if message else None)
    item["error_category"] = item["failure"]["code"] if item["failure"] else None
    collector.append(item)
    context = get_run_context()
    from agent.runtime.checkpoints import write_json

    if context.work_dir.is_dir():
        key = re.sub(r"[^A-Za-z0-9_-]", "_", call.call_id)
        write_json(context.work_dir / "results" / "tools" / f"{key}-{call.attempt}.json", item)


# 从有效证据中提取去重后的网页 URL。
def source_urls(evidence):
    from agent.runtime.evidence_protocol import source_rows

    return list(dict.fromkeys(row["url"] for _, row, _ in source_rows(evidence) if row["kind"] == "web"))
