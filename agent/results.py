"""Redaction and execution result metadata shared by storage and source adapters."""

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
