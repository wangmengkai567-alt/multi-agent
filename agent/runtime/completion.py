"""Completion reflects actual task outcomes and the exact delivered artifact."""

from copy import deepcopy
from hashlib import sha256


# 核对最终答复、交付摘要与 answer.md 字节完全一致。
def validate_delivery(context, answer):
    context.check_active()
    draft = context.delivery_draft or {}
    artifact = draft.get("artifact")
    digest = sha256(answer.encode("utf-8")).hexdigest()
    if not artifact or draft.get("delivered_content_digest") != digest:
        raise RuntimeError("Final answer does not match the delivered content")
    path = context.work_dir / "answer.md"
    if (
        artifact.get("path") != "answer.md"
        or artifact["status"] != "completed"
        or artifact.get("content_digest") != digest
        or artifact["file_digest"] != digest
        or not path.is_file()
        or path.read_bytes() != answer.encode("utf-8")
    ):
        raise RuntimeError("Delivered Markdown is missing or differs from the final answer")


# 汇总执行状态、覆盖情况与文件交付状态；只有成功终态才发布交付文件信息。
def completion_snapshot(context, execution_status, *, terminal_reason=None):
    from agent.runtime.evidence_protocol import fingerprint
    from agent.runtime.required_work import required_work_summary

    execution_status = getattr(context, "stop_status", None) or execution_status
    draft = getattr(context, "delivery_draft", None) or {}
    # Prepared files become delivery artifacts only for the committed success path.
    artifact = deepcopy(draft.get("artifact")) if execution_status == "completed" else None
    delivery_status = "not_delivered" if draft.get("artifact") else "not_requested"
    if artifact:
        delivery_status = artifact["status"]
    coverage = getattr(context, "coverage", "unknown")
    items = getattr(context, "required_work", None)
    if coverage == "complete" and (
        not isinstance(items, dict)
        or draft.get("coverage") != coverage
        or draft.get("required_work_digest") != fingerprint(items)
        or any(item.get("status") != "completed" for item in items.values())
    ):
        coverage = context.coverage = "unknown"
    status = execution_status
    if execution_status == "completed":
        status = coverage if artifact and draft.get("delivered_content_digest") else "unknown"
    return dict(
        status=status,
        execution_status=execution_status,
        coverage=coverage,
        delivery_status=delivery_status,
        artifact=artifact,
        required_work=required_work_summary(
            context, execution_status=execution_status, terminal_reason=terminal_reason
        ),
        error=None,
    )
