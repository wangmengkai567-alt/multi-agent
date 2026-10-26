"""Project business completion from reviewed content, obligations and owned artifacts."""

from copy import deepcopy
from hashlib import sha256


def validate_delivery(context, answer):
    context.check_active()
    contract, draft = context.task_contract, context.delivery_draft
    if not draft or not draft.get("reviewed_content_digest"):
        raise RuntimeError("Final delivery requires content review")
    if draft.get("delivered_content_digest") != sha256(answer.encode()).hexdigest():
        raise RuntimeError("Final answer differs from the accepted delivery")
    if contract.file_output == "none":
        return
    artifact = draft.get("artifact")
    if not artifact or artifact["kind"] != contract.file_output:
        raise RuntimeError("Requested artifact has not been delivered")
    if artifact["content_digest"] != draft["reviewed_content_digest"]:
        raise RuntimeError("Artifact does not belong to reviewed content")
    if artifact["status"] == "working":
        job = context.pending_jobs.get(artifact.get("task_id"))
        if contract.file_output == "pdf" and job and job["kind"] == "pdf_conversion" and job["status"] == "working":
            return
        raise RuntimeError("Artifact is not linked to an owned pending conversion")
    if artifact["status"] != "completed":
        raise RuntimeError("Requested artifact did not complete")
    path = context.work_dir / ("answer.md" if contract.file_output == "markdown" else "answer.pdf")
    if (
        not path.is_file()
        or not path.stat().st_size
        or sha256(path.read_bytes()).hexdigest() != artifact["file_digest"]
    ):
        raise RuntimeError("Delivered artifact is missing or changed")


def completion_snapshot(context, execution_status):
    contract = getattr(context, "task_contract", None)
    draft = getattr(context, "delivery_draft", None) or {}
    artifact = deepcopy(draft.get("artifact"))
    review = "passed" if draft.get("reviewed_content_digest") else "not_passed"
    delivery = "not_requested" if contract and contract.file_output == "none" else "unknown"
    if artifact:
        delivery = "pending" if artifact["status"] == "working" else artifact["status"]
    coverage = getattr(context, "coverage", "unknown")
    if execution_status != "completed":
        status = execution_status
    elif contract and contract.delivery_kind == "clarification":
        status = "input_required"
    elif any(job["kind"] == "research" for job in getattr(context, "pending_jobs", {}).values()):
        status = "pending"
    elif delivery == "pending":
        status = "pending"
    elif (
        review != "passed"
        or not draft.get("delivered_content_digest")
        or delivery not in ("completed", "not_requested")
    ):
        status = "unknown"
    else:
        status = {"complete": "complete", "partial": "partial", "unknown": "unknown"}[coverage]
    return dict(
        status=status,
        execution_status=execution_status,
        review_status=review,
        coverage=coverage,
        delivery_status=delivery,
        artifact=artifact,
        error=deepcopy(draft.get("delivery_error")),
    )


def resolve_completion(completion, jobs):
    """Durable jobs update business completion without rewriting the parent run lifecycle."""
    value = deepcopy(completion)
    if value is None or value["execution_status"] != "completed":
        return value
    for job in jobs:
        if job["status"] in ("failed", "cancelled", "timed_out", "interrupted"):
            value.update(status=job["status"], error=job.get("error"))
            if job["kind"] == "pdf_conversion":
                value["delivery_status"] = job["status"]
                value["error"] = dict(
                    error_category="conversion_failed",
                    stage="background_conversion",
                    tool_name="convert_md_to_pdf",
                    call_id=(value.get("artifact") or {}).get("call_id"),
                    task_id=job["task_id"],
                    message=job.get("error") or job["status"],
                )
            return value
    if any(job["status"] == "input_required" for job in jobs):
        value["status"] = "input_required"
        return value
    if any(job["status"] == "working" for job in jobs):
        value["status"] = "pending"
        return value
    research = next((job for job in jobs if job["kind"] == "research"), None)
    if research is not None:
        result = research.get("result") or {}
        child = result.get("completion")
        if research["status"] != "completed" or not child:
            value["status"] = "unknown"
            return value
        value.update(deepcopy(child))
        value["execution_status"] = "completed"
    artifact = value.get("artifact")
    if artifact and artifact.get("task_id"):
        conversion = next((job for job in jobs if job["task_id"] == artifact["task_id"]), None)
        if conversion is None:
            value["status"] = "unknown"
            return value
        artifacts = (conversion.get("result") or {}).get("artifacts", [])
        if conversion["status"] == "completed" and any(item.get("relative_path") == "answer.pdf" for item in artifacts):
            value["artifact"].update(status="completed", path="answer.pdf")
            value["delivery_status"] = "completed"
        else:
            value["status"] = "unknown"
            return value
    if value["review_status"] == "passed" and value["delivery_status"] in ("completed", "not_requested"):
        value["status"] = {"complete": "complete", "partial": "partial", "unknown": "unknown"}[value["coverage"]]
    return value
