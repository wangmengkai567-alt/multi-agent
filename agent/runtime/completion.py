"""Completion reflects actual task outcomes and the exact delivered artifact."""

from copy import deepcopy
from hashlib import sha256


def validate_delivery(context, answer):
    context.check_active()
    draft = context.delivery_draft or {}
    artifact = draft.get("artifact")
    digest = sha256(answer.encode("utf-8")).hexdigest()
    if not artifact or draft.get("delivered_content_digest") != digest:
        raise RuntimeError("Final answer does not match the delivered content")
    path = context.work_dir / "answer.md"
    if (
        artifact["status"] != "completed"
        or artifact["file_digest"] != digest
        or not path.is_file()
        or path.read_bytes() != answer.encode("utf-8")
    ):
        raise RuntimeError("Delivered Markdown is missing or differs from the final answer")


def completion_snapshot(context, execution_status):
    draft = getattr(context, "delivery_draft", None) or {}
    artifact = deepcopy(draft.get("artifact"))
    coverage = getattr(context, "coverage", "unknown")
    status = execution_status
    if execution_status == "completed":
        status = coverage if artifact and draft.get("delivered_content_digest") else "unknown"
    return dict(
        status=status,
        execution_status=execution_status,
        review_status="not_requested",
        coverage=coverage,
        delivery_status=artifact["status"] if artifact else "not_requested",
        artifact=artifact,
        error=None,
    )
