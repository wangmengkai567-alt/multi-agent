"""Reconstruct continuation from canonical state and owned, validated result records."""

import json
from collections import Counter
from hashlib import sha256

from agent.runtime.context import RunStopped
from agent.runtime.review_state import ReviewState, deliverable


def validate_accounting(context):
    counts = context.call_counts
    categories = {"main_model", "sub_model", "review", "tool", "retry"}
    if set(counts) != categories or any(type(value) is not int or value < 0 for value in counts.values()):
        raise RunStopped("Invalid saved call budget counters")
    charged = Counter()
    for row in context.request_ledger.values():
        if row["category"] not in categories - {"tool", "retry"}:
            raise RunStopped("Unknown saved model request category")
        if row["transport"] is not None:
            charged[row["category"]] += 1
    for row in context.attempt_ledger.values():
        if row["status"] not in ("spent", "uncertain", "released"):
            raise RunStopped("Invalid saved provider attempt state")
        if not row["categories"] or set(row["categories"]) - {"tool", "retry"}:
            raise RunStopped("Invalid provider attempt budget categories")
        if row["status"] != "released":
            charged.update(row["categories"])
    if (
        any(charged[kind] > counts[kind] or counts[kind] > getattr(context.policy.budgets, kind) for kind in categories)
        or sum(counts.values()) > context.policy.budgets.total
    ):
        raise RunStopped("Saved charges disagree with the available call budget")


def recovery_summary(context):
    from agent.runtime.delivery import fact_pool
    from agent.runtime.task_plan import plan_snapshot

    return dict(
        goal=context.task_contract.operation,
        reference_time=context.reference_time.isoformat(),
        plan=plan_snapshot(context),
        verified=fact_pool(context),
        requirements=list(context.requirements.values()),
        gaps=list(context.gaps.values()),
        call_counts=dict(context.call_counts),
        remaining_calls={
            kind: getattr(context.policy.budgets, kind) - value for kind, value in context.call_counts.items()
        },
        uncertain_attempts=[
            dict(key=key, status=row["status"], outcome=row.get("outcome"))
            for key, row in context.attempt_ledger.items()
            if row["status"] == "uncertain" or row.get("outcome") == "uncertain"
        ],
        instructions="沿用稳定 task_id；完成的分支不重采样，未确认发送保留扣账；不得重放交付动作。",
    )


async def owned_results(context, *, purpose=None):
    rows = await context.event_sink.store.list_results(context.run_id, thread_id=context.thread_id, purpose=purpose)
    selected = {row["result_id"]: row for row in rows}
    for key, row in context.recovered_results.items():
        if purpose is None or deliverable(ReviewState.from_record(row)):
            selected.setdefault(key, row)
    return list(selected.values())


def task_identity(context, task_id):
    scope = context.task_scopes.get(task_id)
    return sha256(json.dumps(scope, ensure_ascii=False, sort_keys=True).encode()).hexdigest() if scope else None


def result_review_state(context, record, task_id):
    state = ReviewState.from_record(record)
    ids = set(record["review"].get("coverage", {}))
    if not ids:
        return state
    rows = [context.requirements.get(rid) for rid in ids]
    if any(row is None or row["task_id"] != task_id for row in rows):
        raise RunStopped("Persisted result has foreign requirements")
    versions = {row["query_version"] for row in rows}
    if len(versions) != 1:
        raise RunStopped("Persisted result mixes requirement versions")
    version = next(iter(versions))
    expected = {
        r["rid"] for r in context.requirements.values() if r["task_id"] == task_id and r["query_version"] == version
    }
    if ids != expected or version > context.query_version:
        raise RunStopped("Persisted result has incomplete requirement coverage")
    return (
        ReviewState.decided(state.facts_status, "unknown")
        if state.judged and version < context.query_version
        else state
    )


async def reconcile_result_history(context):
    """Apply authoritative result observations newer than the canonical execution checkpoint."""
    if not context.resume_source_run_id:
        return
    from agent.reflection import source_urls
    from agent.results import ResultService
    from agent.runtime.evidence_protocol import validate_record
    from agent.runtime.facts import render_report
    from agent.runtime.requirements import apply_coverage, task_requirements
    from agent.runtime.reservations import release

    store = context.event_sink.store
    events = await store.list_events(context.resume_source_run_id)
    selected = {}
    for event in events:
        if event["event"] != "result_saved":
            continue
        metadata = event["data"]
        source = metadata["source"]
        execution = context.expert_contract[source]["execution"]
        if execution["input_kind"] != "planned_research":
            continue
        task_id = metadata["task_id"]
        version = metadata["scope_version"]
        scopes = (
            context.task_scopes
            if version == context.query_version
            else next((entry["task_scopes"] for entry in context.query_history if entry["version"] == version), {})
        )
        import json
        from hashlib import sha256

        identity = (
            sha256(json.dumps(scopes[task_id], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if task_id in scopes
            else None
        )
        if task_id not in context.search_tasks or metadata["task_identity"] != identity:
            raise RunStopped("Persisted result does not match its canonical planned task")
        record = await store.get_current_result(metadata["result_id"], context.resume_source_run_id, context.thread_id)
        if record is None:
            continue  # Superseded immutable attempts are history, not the current result.
        if record["source"] != source or record["call_id"] != event["call_id"]:
            raise RunStopped("Persisted result event ownership mismatch")
        if task_id in selected and selected[task_id]["result_id"] != record["result_id"]:
            raise RunStopped("Multiple current result owners for one planned task")
        selected[task_id] = (record, version)

    for task_id, (record, version) in selected.items():
        context.recovered_results[record["result_id"]] = record
        raw = await ResultService().read_raw(context, record["result_id"])
        for item in raw["tools"]:
            validate_record(item, context)
            if item["task_id"] != task_id:
                raise RunStopped("Persisted result contains evidence from another task")
        task = context.search_tasks[task_id]
        state = ReviewState.from_record(record)
        report = record["review"].get("accepted_report") if deliverable(state) else None
        if deliverable(state) and not isinstance(report, dict):
            raise RunStopped("Verified result has no canonical fact report")
        task.state, task.report = state, report
        task.current_result_id, task.result_run_id = record["result_id"], record["run_id"]
        task.report_attempt = record["attempt"]
        task.evidence, task.sources = raw["tools"], source_urls(raw["tools"])
        task.result = render_report(report) if report else ""
        task.active = False
        task.closed_reason = task.closed_reason or "已恢复持久化审核结果；不重新采集"
        task.failure_reason = record["review"].get("error") or ""
        rows = (record["review"].get("coverage") if version == context.query_version else None) or {
            requirement["rid"]: dict(status="unknown", fact_ids=[], claim_ids=[])
            for requirement in task_requirements(context, task_id)
        }
        if version != context.query_version and task.state.judged:
            task.state = ReviewState.decided(task.state.facts_status, "unknown")
        apply_coverage(context, task_id, rows)
        reservation = context.capacity_reservations.get("plan:" + task_id)
        if reservation is not None:
            reservation["stages"] = {}
        for owner, row in list(context.revision_reservations.items()):
            if row["task_id"] == task_id:
                release(context, owner)
                context.restored_reservation_owners.discard(owner)
