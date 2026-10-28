"""Redaction and program-owned execution results shared by storage and delegates."""

import asyncio
import json
import re

from sqlglot import exp, parse_one

from agent.runtime.events import EventPersistenceError, redact_text


def clean(value):
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                return json.dumps(clean(json.loads(value)), ensure_ascii=False)
            except ValueError:
                pass
        return redact_text(value)
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]"
            if re.search(r"(?i)(password|secret|token|api[_-]?key|authorization|cookie)", str(k))
            else clean(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [clean(v) for v in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    return str(value)


def task_evidence(evidence, task_id):
    """Accept only this task's observations and retain unresolved execution limits."""
    from agent.runtime.context import get_run_context
    from agent.runtime.evidence_protocol import EvidenceError, source_rows, validate_record
    from agent.runtime.workflows import _range_covered

    for item in evidence:
        if item["task_id"] != task_id:
            raise EvidenceError("Tool evidence belongs to another delegated task")
        validate_record(item, get_run_context())
    sources = [row for _, row, _ in source_rows(evidence)]
    unfinished, limitations = [], []
    documents = {}
    # A successful runtime retry resolves earlier attempts of that same call.
    latest = {item["call_id"]: item for item in evidence}
    for item in latest.values():
        reason = item["error"] if item["status"] != "completed" else ""
        if item["status"] == "completed" and item["adapter"] == "document.v1":
            coverage = json.loads(item["evidence_note"])
            limitations.extend(
                dict(call_id=item["call_id"], tool=item["name"], reason=note) for note in coverage["limitations"]
            )
            document = item["artifact"]["document"]
            identity = (document["path"], document["sha256"], document["parser"])
            state = documents.setdefault(
                identity, dict(extents=coverage["extents"], ranges={}, missing=set(), complete=False, item=item)
            )
            if state["extents"] != coverage["extents"]:
                raise EvidenceError("Document coverage changed within the delegated task")
            state["complete"] |= coverage["complete"]
            state["missing"].update(coverage["missing"])
            for target, bounds in coverage["read_ranges"].items():
                state["ranges"].setdefault(target, []).append(bounds)
            reason = ""
        elif item["status"] == "completed" and item["adapter"] not in ("control.v1", "network-control.v1"):
            reason = item["evidence_note"]
        if reason:
            unfinished.append(dict(call_id=item["call_id"], tool=item["name"], status=item["status"], reason=reason))
    for (path, _, _), state in documents.items():
        covered = (
            state["complete"]
            or bool(state["ranges"])
            and all(
                _range_covered(extent, state["ranges"].get(target, [])) for target, extent in state["extents"].items()
            )
        )
        if state["missing"] or not covered:
            item = state["item"]
            reason = path + "：" + "；".join(sorted(state["missing"]) or ["材料尚未完整读取"])
            unfinished.append(dict(call_id=item["call_id"], tool=item["name"], status=item["status"], reason=reason))
    return sources, unfinished, limitations


def delegation_payload(row):
    """One result shape for success, failure, timeout and cancellation snapshots."""
    return dict(
        task_id=row["call_id"],
        expert=row["target"],
        status=row["status"],
        result=row["result"],
        sources=[
            {
                key: source[key]
                for key in ("key", "kind", "title", "url", "locator", "inputs", "retrieval", "provenance")
            }
            for source in row["sources"]
        ],
        unfinished=row["unfinished"],
        limitations=row["limitations"],
        error=row["error"],
        error_category=row["error_category"],
    )


def metadata(text, evidence):
    """Keep declared scope separate from actual SQL; never infer missing dates."""

    def declared(label):
        match = re.search(r"(?m)^\s*" + label + r"[：:]\s*([^\r\n]+)", text)
        return redact_text(match[1].strip()) if match else "未提供"

    tables, queries = set(), []
    for item in evidence:
        if not item.get("executed") or item.get("status") != "completed":
            continue
        args = item.get("query_reference", {})
        if args.get("table_name"):
            tables.add(args["table_name"])
        query = args.get("query")
        if query:
            queries.append(redact_text(query))
            try:
                tables.update(t.name for t in parse_one(query, read="mysql").find_all(exp.Table))
            except Exception:
                pass  # The policy already validated production SQL; do not invent metadata.
    return dict(
        query_object=", ".join(sorted(tables)) or declared("查询对象"),
        time_range=declared("时间范围"),
        statistical_scope=dict(reported=declared("统计口径"), executed_sql=queries),
    )


def sync_result(coro, sink):
    """Sync framework executors run in workers; DB work stays on its owner loop."""
    try:
        same_loop = asyncio.get_running_loop() is sink.loop
    except RuntimeError:
        same_loop = False
    if same_loop:
        coro.close()
        raise EventPersistenceError("Use ainvoke on the event loop for durable result handoff")
    return asyncio.run_coroutine_threadsafe(coro, sink.loop).result()
