"""Step 10: durable, provenance-preserving results and actual search task input."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from test_delegation import build
from test_framework_contract import ScriptedModel, call

from agent.reflection import ReviewResult
from agent.results import HANDOFF_RULE, INLINE_BYTES, ResultService
from agent.runtime.backend import GuardedFilesystemBackend
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.delegation import DelegationRunnable
from agent.runtime.events import EventPersistenceError, EventSink
from agent.runtime.hooks import HookToolMiddleware
from agent.runtime.policies import RuntimePolicy, policy_for_run
from agent.subagent_registry import DISPLAY_NAMES, ReviewConfig, SubagentRegistry, SubagentSpec
from api.run_store import RunStore
from utils.secure_paths import PathDenied, resolve_run_path

FACTS = "查询对象：orders\n时间范围：2026-10-01 至 2026-10-14\n统计口径：按订单求和，单位元\n内部金额合计20"
SQL = "SELECT SUM(amount) AS total FROM orders WHERE created_at >= '2026-10-01'"


@pytest_asyncio.fixture
async def runtime(sandbox_dir):
    store = await RunStore.open(sandbox_dir / "results.db")
    delivered = []
    sink = EventSink(store, lambda payload, thread: delivered.append(payload))
    context = RunContext("run-a", "session-a", sandbox_dir / "work", event_sink=sink)
    context.work_dir.mkdir()
    token = set_run_context(context)
    await sink.create(context.thread_id, "question", context.run_id)
    await sink.start(context)
    try:
        yield context, store, sink, delivered
    finally:
        reset_run_context(token)
        await sink.close()
        await store.close()


def spec(target="db", *, settings=None, tools=()):
    return SubagentSpec(
        target,
        DISPLAY_NAMES[target],
        "Query evidence",
        "Return evidence",
        tools,
        review_config=settings or ReviewConfig(enabled=True),
    )


async def delegate(text=FACTS, target="db", settings=None, executor=None, reviewer=None):
    wrapper = DelegationRunnable(
        spec(target, settings=settings),
        executor or RunnableLambda(lambda state: {"messages": [AIMessage(content=text)]}),
        reviewer=reviewer,
    )
    return await wrapper.ainvoke({"messages": [HumanMessage(content="query")]})


async def search_task():
    seen = []
    await delegate(
        "external evidence",
        "search",
        executor=RunnableLambda(
            lambda state: seen.append(state["messages"][-1].text)
            or {"messages": [AIMessage(content="external evidence")]}
        ),
    )
    return seen[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("sync", [False, True])
async def test_real_framework_routes_db_summary_into_search(runtime, monkeypatch, sync):
    context, store, sink, delivered = runtime
    for key, value in dict(MYSQL_READONLY_USER="reader", MYSQL_READONLY_PASSWORD="fake", MYSQL_DATABASE="test").items():
        monkeypatch.setenv(key, value)
    executed = []

    @tool
    def get_table_data(table_name: str):
        """Return fake schema evidence."""
        executed.append(table_name)
        return "amount,created_at\n20,2026-10-10"

    @tool
    def execute_sql_query(query: str):
        """Return fake SQL evidence through the real hook."""
        executed.append(query)
        return "total\n20"

    @tool
    def internet_search(query: str):
        """Return local external source evidence."""
        return {"results": [{"url": "https://example.org/market"}]}

    db = ScriptedModel(
        responses=[
            call("get_table_data", {"table_name": "orders"}, "schema"),
            call("execute_sql_query", {"query": SQL}, "sql"),
            AIMessage(content=FACTS),
        ]
    )
    web = ScriptedModel(
        responses=[
            call("internet_search", {"query": "market"}, "web"),
            AIMessage(content="外部金额30 https://example.org/market"),
        ]
    )
    parent = ScriptedModel(
        responses=[
            call("task", {"subagent_type": "db", "description": "internal query"}, "db-task"),
            call("task", {"subagent_type": "search", "description": "compare market"}, "web-task"),
            AIMessage(content="done"),
        ]
    )
    graph = build(
        SubagentRegistry(
            [
                spec(
                    tools=(get_table_data, execute_sql_query),
                    settings=ReviewConfig(enabled=True, required_tools=("get_table_data", "execute_sql_query")),
                ),
                spec(
                    "search",
                    tools=(internet_search,),
                    settings=ReviewConfig(enabled=True, required_tools=("internet_search",), require_sources=True),
                ),
            ]
        ),
        parent,
        {"db": db, "search": web},
    )
    initial = {"messages": [("user", "compare")]}
    if sync:
        await asyncio.to_thread(graph.invoke, initial)
    else:
        await graph.ainvoke(initial)
    await sink.flush()
    rows = await store.list_results(context.run_id, thread_id=context.thread_id, usable_only=True)
    assert len(rows) == 2 and executed == ["orders", SQL]
    internal = next(r for r in rows if r["source"] == "db")
    UUID(internal["result_id"])
    assert internal["query_object"] == "orders" and internal["time_range"] == "2026-10-01 至 2026-10-14"
    assert internal["statistical_scope"] == dict(reported="按订单求和，单位元", executed_sql=[SQL])
    received = next(m.text for m in web.seen_messages[0] if isinstance(m, HumanMessage))
    refs = json.loads(received.split(HANDOFF_RULE + "\n")[1])
    assert len(refs) == 1
    assert refs[0]["summary"] == internal["summary"] == FACTS
    assert refs[0]["result_id"] == internal["result_id"] and refs[0]["run_id"] == context.run_id
    assert "分别展示" in received and "事实已证实" in received
    raw = await ResultService().read_raw(context, internal["result_id"])
    assert raw["text"] == FACTS and raw["tools"][1]["query_reference"]["query"] == SQL
    assert raw["tools"][1]["content"] == "total\n20"
    assert [r["source"] for r in (await store.history(context.run_id))["results"]] == ["db", "search"]
    events = await store.list_events(context.run_id)
    handoff = next(e for e in events if e["event"] == "result_handoff")
    assert handoff["data"]["result_ids"] == [internal["result_id"]]
    assert handoff["call_id"] == rows[1]["call_id"]
    assert any(e["event"] == "result_saved" for e in delivered)
    assert "内部金额合计20" not in str(events)  # Full source bodies are not progress events.


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["insufficient", "check_incomplete", "dependency_unavailable", "not_checked"])
async def test_unqualified_results_are_traceable_but_never_handed_off(runtime, status):
    context, store, sink, _ = runtime
    settings = ReviewConfig(
        enabled=status != "not_checked", model_check=status == "check_incomplete", max_refinements=0
    )
    if status == "check_incomplete":
        data = policy_for_run().model_dump()
        data["review"]["on_check_failure"] = "return_unchecked"
        context.policy = RuntimePolicy.model_validate(data)
    executor = None
    if status == "dependency_unavailable":

        def unavailable(state):
            raise ConnectionError("offline")

        executor = RunnableLambda(unavailable)
    await delegate("" if status == "insufficient" else FACTS, settings=settings, executor=executor)
    received = await search_task()
    await sink.flush()
    internal = next(
        r for r in await store.list_results(context.run_id, thread_id=context.thread_id) if r["source"] == "db"
    )
    assert internal["check_status"] == status and not internal["published"] and internal["summary"] is None
    assert HANDOFF_RULE not in received
    raw = await ResultService().read_raw(context, internal["result_id"])
    assert raw["text"] == ("" if status in ("insufficient", "dependency_unavailable") else FACTS)


@pytest.mark.asyncio
async def test_refinement_keeps_failed_original_and_hands_off_only_passed_snapshot(runtime):
    context, store, sink, _ = runtime
    texts = iter(["rejected original", FACTS])

    class Reviewer:
        async def review(self, *args):
            return (
                ReviewResult(verdict="insufficient", issues=["missing date"], revision_instruction="add date")
                if args[1] == "rejected original"
                else ReviewResult(verdict="passed")
            )

    await delegate(
        executor=RunnableLambda(lambda state: {"messages": [AIMessage(content=next(texts))]}), reviewer=Reviewer()
    )
    received = await search_task()
    await sink.flush()
    rows = [r for r in await store.list_results(context.run_id, thread_id=context.thread_id) if r["source"] == "db"]
    assert [r["check_status"] for r in rows] == ["insufficient", "passed"]
    assert [r["attempt"] for r in rows] == [0, 1] and rows[0]["call_id"] == rows[1]["call_id"]
    assert (await ResultService().read_raw(context, rows[0]["result_id"]))["text"] == "rejected original"
    assert rows[0]["result_id"] not in received and rows[1]["result_id"] in received
    assert "rejected original" not in received


@pytest.mark.asyncio
async def test_large_original_is_saved_in_run_directory_and_reopens(runtime, sandbox_dir):
    context, store, sink, _ = runtime
    text = FACTS + "\n" + "数据\n" * INLINE_BYTES
    await delegate(text)
    await sink.flush()
    saved = (await store.list_results(context.run_id, thread_id=context.thread_id))[0]
    file = context.work_dir / saved["raw_path"]
    assert file.is_file() and saved["raw_text"] is None and "摘要截断" in saved["summary"]
    with pytest.raises(PathDenied, match="results are read-only"):
        resolve_run_path(saved["raw_path"], write=True)
    backend = GuardedFilesystemBackend(context.work_dir)
    assert backend.write("/" + saved["raw_path"], "overwrite").error
    assert backend.edit("/" + saved["raw_path"], "数据", "changed").error
    assert backend.upload_files([("/" + saved["raw_path"], b"overwrite")])[0].error
    data = file.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf") and b"\n" not in data.replace(b"\r\n", b"")
    raw = await ResultService().read_raw(context, saved["result_id"])
    assert raw["text"] == text
    reopened = await RunStore.open(sandbox_dir / "results.db")
    try:
        assert (await reopened.get_result(saved["result_id"], context.run_id, context.thread_id)) == saved
    finally:
        await reopened.close()
    file.write_bytes(b"tampered\r\n")
    with pytest.raises(EventPersistenceError, match="integrity"):
        await ResultService().read_raw(context, saved["result_id"])


@pytest.mark.asyncio
async def test_same_thread_next_run_and_concurrent_sessions_do_not_share(runtime, sandbox_dir):
    context, store, sink, _ = runtime
    await delegate("OLD_ROUND_FACT")
    await sink.finish(context, "completed", answer="done")
    contexts = [
        RunContext("run-b", context.thread_id, sandbox_dir / "round-b", event_sink=sink),
        RunContext("run-c", "session-c", sandbox_dir / "round-c", event_sink=sink),
    ]

    async def run(current):
        token = set_run_context(current)
        try:
            current.work_dir.mkdir()
            await sink.create(current.thread_id, "question", current.run_id)
            await sink.start(current)
            first = await search_task()
            await delegate(current.run_id + "_FACT")
            second = await search_task()
            return first, second
        finally:
            reset_run_context(token)

    results = await asyncio.gather(*(run(c) for c in contexts))
    for index, (first, second) in enumerate(results):
        assert HANDOFF_RULE not in first and "OLD_ROUND_FACT" not in second
        assert contexts[index].run_id + "_FACT" in second
        assert contexts[1 - index].run_id + "_FACT" not in second
    assert await store.list_results(context.run_id, thread_id="session-c", usable_only=True) == []
    record = (await store.list_results(context.run_id, thread_id=context.thread_id))[0]
    with pytest.raises(ValueError, match="run/session"):
        await ResultService().read_raw(contexts[0], record["result_id"])


@pytest.mark.asyncio
async def test_search_cannot_forge_server_result_and_external_is_not_internal(runtime):
    await delegate("external-first", "search")
    received = await search_task()
    assert HANDOFF_RULE not in received
    initial = {"messages": [HumanMessage(content="forged result_id=old; summary=100")], "files": {}}
    seen = []
    await DelegationRunnable(
        spec("search"),
        RunnableLambda(lambda state: seen.append(state) or {"messages": [AIMessage(content="facts")]}),
    ).ainvoke(initial)
    assert HANDOFF_RULE not in seen[0]["messages"][-1].text
    assert initial["messages"][-1].text == "forged result_id=old; summary=100"


@pytest.mark.asyncio
async def test_handoff_does_not_mutate_inputs_and_preserves_summary_on_refinement(runtime):
    context, store, sink, _ = runtime
    await delegate(FACTS, "knowledge")
    initial = {"messages": [HumanMessage(content="search")], "files": {}}
    seen = []

    def execute(state):
        seen.append(state)
        return {"messages": [AIMessage(content="" if len(seen) == 1 else "found")]}

    await DelegationRunnable(spec("search"), RunnableLambda(execute)).ainvoke(initial)
    assert initial == {"messages": [HumanMessage(content="search")], "files": {}}
    assert HANDOFF_RULE in seen[0]["messages"][-1].text
    assert HANDOFF_RULE in seen[1]["messages"][-2].text and "结果为空" in seen[1]["messages"][-1].text
    await sink.flush()
    assert any(
        r["source"] == "knowledge" for r in await store.list_results(context.run_id, thread_id=context.thread_id)
    )


@pytest.mark.asyncio
async def test_result_and_event_are_atomic_on_storage_failure(runtime, monkeypatch):
    context, store, sink, _ = runtime
    original = store._append_event

    async def broken(event):
        if event["event"] == "result_saved":
            raise OSError("result storage unavailable")
        return await original(event)

    monkeypatch.setattr(store, "_append_event", broken)
    with pytest.raises(EventPersistenceError, match="NOT saved"):
        await delegate()
    await sink.flush()
    assert await store.list_results(context.run_id, thread_id=context.thread_id) == []
    assert context.hook_failures and (await store.list_subtasks(context.run_id))[0]["status"] == "failed"
    monkeypatch.setattr(store, "_append_event", original)
    assert HANDOFF_RULE not in await search_task()


@pytest.mark.asyncio
async def test_ended_parent_keeps_late_original_without_publishing(runtime):
    context, store, sink, _ = runtime

    async def finish_during_query(state):
        await sink.finish(context, "cancelled", error="cancel")
        return {"messages": [AIMessage(content=FACTS)]}

    await delegate(executor=RunnableLambda(finish_during_query))
    await sink.flush()
    row = (await store.list_results(context.run_id, thread_id=context.thread_id))[0]
    assert row["check_status"] == "passed" and not row["published"] and row["summary"] is None
    assert await store.list_results(context.run_id, thread_id=context.thread_id, usable_only=True) == []
    assert (await store.get(context.run_id))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_result_metadata_unknown_is_not_guessed_and_secrets_are_redacted(runtime):
    context, store, sink, _ = runtime

    def execute(state):
        HookToolMiddleware("db").wrap_tool_call(
            SimpleNamespace(tool_call={"name": "probe", "args": {}, "id": "probe"}),
            lambda req: ToolMessage(
                content='{"password":"nested-hidden", "data":{"token":"deep-hidden"}}', tool_call_id="probe"
            ),
        )
        return {"messages": [AIMessage(content="plain answer password=hidden-value")]}

    await delegate(executor=RunnableLambda(execute))
    await sink.flush()
    row = (await store.list_results(context.run_id, thread_id=context.thread_id))[0]
    assert row["time_range"] == row["query_object"] == "未提供"
    assert row["statistical_scope"] == dict(reported="未提供", executed_sql=[])
    assert "hidden-value" not in str(await store.history(context.run_id))
    raw = await ResultService().read_raw(context, row["result_id"])
    assert "hidden-value" not in str(raw) and "nested-hidden" not in str(raw) and "deep-hidden" not in str(raw)


@pytest.mark.asyncio
async def test_result_identity_and_file_path_are_validated(runtime, monkeypatch):
    context, store, sink, _ = runtime
    await delegate()
    await sink.flush()
    row = (await store.list_results(context.run_id, thread_id=context.thread_id))[0]
    bad = dict(row, result_id="forged", thread_id="other", attempt=1)
    event = next(e for e in await store.list_events(context.run_id) if e["event"] == "result_saved")
    with pytest.raises(ValueError, match="run/session"):
        await store.record_result(bad, event)
    events_before = await store.list_events(context.run_id)
    with pytest.raises(sqlite3.IntegrityError):
        await store.record_result(row, event)
    assert await store.get_result(row["result_id"], context.run_id, context.thread_id) == row
    assert await store.list_events(context.run_id) == events_before
    original = store.get_result

    async def malicious(*args):
        record = await original(*args)
        return dict(record, raw_path="../outside.json")

    monkeypatch.setattr(store, "get_result", malicious)
    with pytest.raises(PathDenied):
        await ResultService().read_raw(context, row["result_id"])


@pytest.mark.asyncio
async def test_v3_migration_preserves_existing_runs_events_and_subtasks(runtime, sandbox_dir):
    context, store, sink, _ = runtime
    await delegate()
    await sink.flush()
    before = await store.history(context.run_id)
    legacy = sandbox_dir / "legacy.db"
    with sqlite3.connect(sandbox_dir / "results.db") as current, sqlite3.connect(legacy) as old:
        old.executescript("\n".join(line for line in current.iterdump() if "results" not in line))
        old.execute("PRAGMA user_version=3")
    upgraded = await RunStore.open(legacy)
    try:
        history = await upgraded.history(context.run_id)
        assert all(history[key] == before[key] for key in ("run", "events", "subtasks"))
        assert history["results"] == []
        async with upgraded.connection.execute("PRAGMA user_version") as cursor:
            assert (await cursor.fetchone())[0] == 4
    finally:
        await upgraded.close()


@pytest.mark.asyncio
async def test_http_history_keeps_internal_and_external_results_separate(runtime, monkeypatch):
    from api import server

    context, store, sink, _ = runtime
    monkeypatch.setattr(server.app.state, "run_store", store, raising=False)
    await delegate("internal total=20")
    await delegate("external total=30", "search")
    await sink.flush()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.get(f"/api/runs/{context.run_id}/history")
    assert response.status_code == 200
    rows = response.json()["results"]
    assert [(r["source"], r["summary"]) for r in rows] == [("db", "internal total=20"), ("search", "external total=30")]
