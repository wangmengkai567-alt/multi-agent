"""Atomic, bounded-run snapshots, including tools completed before a worker is cancelled."""

import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4


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
        if context.job_checkpoint_closed:
            return
        tasks = {}
        for key, task in context.search_tasks.items():
            item = asdict(task)
            item["calls"], item["page_calls"] = sorted(task.calls), sorted(task.page_calls)
            tasks[key] = item
        write_json(
            context.job_checkpoint_path,
            dict(
                call_counts=context.call_counts,
                answer_repair_attempted=context.answer_repair_attempted,
                answer_citation_repair_attempted=context.answer_citation_repair_attempted,
                delivery_status=context.delivery_status,
                collecting=context.collecting,
                tool_turns=[[owner, call, turn] for (owner, call), turn in context.tool_turns.items()],
                turn_counts=context.turn_counts,
                original_call_ids=context.original_call_ids,
                search_calls=sorted(context.search_calls),
                tasks=tasks,
                task_scopes=context.task_scopes,
                accepted_plan=context.accepted_plan,
                approved_internal=context.approved_internal,
            ),
        )


def restore_job_checkpoint(context):
    from agent.runtime.search import SearchTaskBudget

    path = context.job_checkpoint_path
    if not path or not Path(path).exists():
        return
    data = json.loads(Path(path).read_text("utf-8"))
    context.call_counts.update(data["call_counts"])
    context.answer_repair_attempted = data.get("answer_repair_attempted", False)
    context.answer_citation_repair_attempted = data.get("answer_citation_repair_attempted", False)
    context.delivery_status = data.get("delivery_status")
    context.collecting = data.get("collecting", True)
    context.tool_turns = {(owner, call): turn for owner, call, turn in data.get("tool_turns", [])}
    context.turn_counts = data.get("turn_counts", {})
    context.original_call_ids = data.get("original_call_ids", {})
    context.task_scopes = data.get("task_scopes", {})
    context.accepted_plan = data.get("accepted_plan", False)
    context.approved_internal = data.get("approved_internal", {})
    context.search_calls = {tuple(x) for x in data["search_calls"]}
    for key, item in data["tasks"].items():
        item["calls"], item["page_calls"] = set(item["calls"]), set(item["page_calls"])
        item["active"] = False
        context.search_tasks[key] = SearchTaskBudget(**item)
