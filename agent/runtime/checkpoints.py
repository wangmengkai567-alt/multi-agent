"""Atomic, bounded-run snapshots, including tools completed before a worker is cancelled."""

import json
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from agent.runtime.contracts import CHECKPOINT_VERSION


def write_json(path, value):
    from agent.results import clean

    path = Path(path).resolve()
    import os

    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        path = Path("\\\\?\\" + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".write-" + uuid4().hex[:12] + ".tmp")
    raw = json.dumps(clean(value), ensure_ascii=False, indent=2).replace("\n", "\r\n") + "\r\n"
    try:
        with temporary.open("xb") as stream:
            stream.write(raw.encode("utf-8"))
            stream.flush()
            import os

            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_job_checkpoint(context):
    if not context.job_checkpoint_path or context.job_checkpoint_closed:
        return
    with context.budget_lock:
        tasks = {}
        for key, task in context.search_tasks.items():
            item = asdict(task)
            item["state"] = task.state.model_dump()
            item["calls"], item["page_calls"] = sorted(task.calls), sorted(task.page_calls)
            tasks[key] = item
        fields = (
            "call_counts",
            "collection_states",
            "answer_repair_attempted",
            "answer_citation_repair_attempted",
            "coverage",
            "query_version",
            "objects",
            "requirement_links",
            "requirements",
            "coverage_rows",
            "gaps",
            "research_cache",
            "evidence_snapshots",
            "quote_registry",
            "request_ledger",
            "attempt_ledger",
            "outbound",
            "review_cache",
            "revision_reservations",
            "delivery_draft",
            "collecting",
            "turn_counts",
            "original_call_ids",
            "task_scopes",
            "expert_contract",
            "accepted_plan",
            "plan_version",
            "plan_expansions",
            "plan_closed",
            "approved_internal",
        )
        data = {key: getattr(context, key) for key in fields}
        data.update(
            execution_run_id=context.run_id,
            terminal_failure=type(context.terminal_error).__name__ if context.terminal_error is not None else None,
            owner_id=context.owner_id,
            thread_id=context.thread_id,
            research_scope_id=context.research_scope_id,
            user_query=context.user_query,
            input_text=context.input_text,
            network_policy=context.network_policy,
            task_contract=context.task_contract.model_dump(mode="json") if context.task_contract else None,
            checkpoint_schema_version=CHECKPOINT_VERSION,
            source_policy_version=sha256(context.policy.sources.model_dump_json().encode()).hexdigest(),
            tool_turns=[[owner, call, turn] for (owner, call), turn in context.tool_turns.items()],
            search_calls=sorted(context.search_calls),
            tasks=tasks,
        )
        write_json(context.job_checkpoint_path, data)


def restore_job_checkpoint(context):
    from threading import Event

    from agent.runtime.context import RunStopped
    from agent.runtime.review_state import ReviewState
    from agent.runtime.search import SearchTaskBudget

    path = context.job_checkpoint_path
    if not path or not Path(path).exists():
        return
    data = json.loads(Path(path).read_text("utf-8"))
    if data.get("checkpoint_schema_version") != CHECKPOINT_VERSION:
        context.stop("interrupted")
        raise RunStopped("旧 checkpoint 与当前执行控制及任务契约不一致，保留原文件并中断；不按新协议自动续跑")
    if (
        not context.owner_id
        or data["owner_id"] != context.owner_id
        or data["thread_id"] != context.thread_id
        or data["research_scope_id"] != context.research_scope_id
        or data["user_query"] != context.user_query
        or data["network_policy"] != context.network_policy
        or data["input_text"] != context.input_text
    ):
        context.stop("interrupted")
        raise RunStopped("Checkpoint logical task/query mismatch")
    if data["source_policy_version"] != sha256(context.policy.sources.model_dump_json().encode()).hexdigest():
        context.stop("interrupted")
        raise RunStopped("Source policy changed; retain the checkpoint and require a new reviewed run")
    if data["terminal_failure"] is not None:
        context.stop("failed")
        raise RunStopped(
            "Saved execution terminated: " + data["terminal_failure"] + "; retain checkpoint without resampling"
        )
    from agent.runtime.contracts import TaskContract, assert_contract

    saved_contract = data["task_contract"]
    restored_contract = TaskContract.model_validate_json(json.dumps(saved_contract)) if saved_contract else None
    if context.task_contract is not None and context.task_contract != restored_contract:
        context.stop("interrupted")
        raise RunStopped("Checkpoint changed the originally authorized task contract")
    context.task_contract = restored_contract
    if context.task_contract is not None:
        for material in context.task_contract.materials:
            if material.kind == "previous":
                context.previous_run_id, context.previous_answer = material.reference, material.text
        assert_contract(context)
    context.resume_source_run_id = data["execution_run_id"]
    special = {
        "checkpoint_schema_version",
        "terminal_failure",
        "owner_id",
        "thread_id",
        "tool_turns",
        "search_calls",
        "tasks",
        "execution_run_id",
        "research_scope_id",
        "user_query",
        "task_contract",
        "source_policy_version",
    }
    for key, value in data.items():
        if key not in special:
            setattr(context, key, value)
    from agent.runtime.evidence_protocol import validate_record
    from agent.runtime.quotes import validate_registry

    try:
        for record in [*data["tasks"].values(), *context.approved_internal.values()]:
            for item in record["evidence"]:
                validate_record(item, context)
        validate_registry(context)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        context.stop("interrupted")
        raise RunStopped("Saved evidence/quote registry failed integrity checks") from exc
    from agent.runtime.measurement import restore_measurements

    restore_measurements(context)
    context.tool_turns = {(owner, call): turn for owner, call, turn in data["tool_turns"]}
    context.search_calls = {tuple(row) for row in data["search_calls"]}
    for key, item in data["tasks"].items():
        item["calls"], item["page_calls"] = set(item["calls"]), set(item["page_calls"])
        item["state"] = ReviewState.model_validate(item["state"])
        item["active"] = False
        context.search_tasks[key] = SearchTaskBudget(**item)
    for key, entry in context.research_cache.items():
        if entry["status"] == "in_flight":
            entry["status"] = "failed"
        context.research_waiters[key] = Event()
        context.research_waiters[key].set()
    journal = context.work_dir / "transport.jsonl"
    observed = (
        [json.loads(line) for line in journal.read_text("utf-8").splitlines() if line.strip()]
        if journal.exists()
        else []
    )
    if observed[: len(context.outbound)] != context.outbound:
        context.stop("interrupted")
        raise RunStopped("Checkpoint transport journal mismatch")
    for entry in observed:
        key = f"{entry['kind']}:{entry['call_id']}:{entry['attempt']}"
        row = context.attempt_ledger.get(key)
        if row is None or row["run_id"] != entry["run_id"] or row["request_key"] != entry["request_key"]:
            context.stop("interrupted")
            raise RunStopped("Unowned transport observation in checkpoint")
        row["status"] = "spent"
    context.outbound = observed
    for row in context.attempt_ledger.values():
        if row["status"] == "reserved":
            # Preserve uncertain charges, but never invent a transport send.
            row["status"] = "uncertain"
            row["outcome"] = "uncertain"
    from agent.runtime.measurement import persist

    persist(context)
