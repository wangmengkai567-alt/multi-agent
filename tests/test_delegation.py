"""Step 08: real framework task routes, independent IDs and durable lifecycle."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from test_framework_contract import ScriptedModel, call

from agent.runtime.context import RunContext, RunStopped, get_run_context, reset_run_context, set_run_context
from agent.runtime.delegation import DelegationRunnable
from agent.runtime.events import EventPersistenceError, EventSink
from agent.runtime.hooks import HookPipeline, HookRejected, HookToolMiddleware, active_call, default_pipeline
from agent.runtime.models import budget_model
from agent.subagent_factory import build_subagents
from agent.subagent_registry import DISPLAY_NAMES, SubagentRegistry, SubagentSpec
from api.monitor import monitor
from api.run_store import RunStore


def spec(target, tools=()):
    return SubagentSpec(target, DISPLAY_NAMES[target], "Find evidence", "Return actual evidence", tools)


def build(registry, parent, children, *, hooks=None):
    return create_deep_agent(
        model=budget_model(parent, "main_model"),
        subagents=build_subagents(registry, parent, models=children, hooks=hooks),
        middleware=[HookToolMiddleware(allowed_targets=registry.specs, managed_delegation=True, hooks=hooks)],
    )


@pytest_asyncio.fixture
async def runtime(sandbox_dir):
    store = await RunStore.open(sandbox_dir / "runs.db")
    deliveries = []
    sink = EventSink(store, lambda payload, thread: deliveries.append((payload, thread)))
    context = RunContext(
        "round", "session", sandbox_dir / "work", event_sink=sink, deadline=datetime.now(UTC) + timedelta(minutes=2)
    )
    context.work_dir.mkdir()
    token = set_run_context(context)
    await sink.create(context.thread_id, "question", context.run_id)
    await sink.start(context)
    try:
        yield context, store, sink, deliveries
    finally:
        reset_run_context(token)
        await sink.close()
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_two_real_task_routes_and_durable_server_ids(runtime, asynchronous):
    context, store, sink, deliveries = runtime
    actual = []

    @tool
    def probe(target: str):
        """Fake controlled business query."""
        actual.append((target, active_call.get().parent_call_id, get_run_context().run_id))
        return f"facts:{target}"

    registry = SubagentRegistry([spec(target, (probe,)) for target in DISPLAY_NAMES])
    parent = ScriptedModel(
        responses=[
            *[
                call("task", {"subagent_type": target, "description": f"private task {target}"}, f"model-{target}")
                for target in DISPLAY_NAMES
            ],
            AIMessage(content="done"),
        ]
    )
    children = {
        target: ScriptedModel(
            responses=[call("probe", {"target": target}, f"tool-{target}"), AIMessage(content=f"answer:{target}")]
        )
        for target in DISPLAY_NAMES
    }
    agent = build(registry, parent, children)
    state = (
        await agent.ainvoke({"messages": [("user", "query")]})
        if asynchronous
        else await asyncio.to_thread(agent.invoke, {"messages": [("user", "query")]})
    )
    await sink.flush(context.run_id)
    tasks = await store.list_subtasks(context.run_id)
    events = await store.list_events(context.run_id)
    assert state["messages"][-1].content == "done" and len(actual) == len(tasks) == 2
    assert context.call_counts == {"main_model": 3, "sub_model": 4, "review": 0, "tool": 4, "retry": 0}
    for task in tasks:
        target = task["target"]
        assert str(UUID(task["call_id"])) == task["call_id"] and task["call_id"] != task["request_call_id"]
        assert task["request_call_id"] == f"model-{target}" and task["deadline"] == context.deadline.isoformat()
        assert task["status"] == "completed" and task["result"] == f"answer:{target}"
        assert task["check_status"] == "not_checked" and task["description"] == "[REDACTED]"
        assert task["description_length"] == len(f"private task {target}") and len(task["description_sha256"]) == 64
        assert (target, task["call_id"], context.run_id) in actual
        lifecycle = [event for event in events if event["call_id"] == task["call_id"]]
        assert [event["event"] for event in lifecycle] == [
            "subagent_queued",
            "before_delegate",
            "subagent_running",
            "result_saved",
            "subagent_completed",
            "after_subagent",
        ]
        tool_events = [event for event in events if event["call_id"] == f"tool-{target}"]
        assert [event["event"] for event in tool_events] == ["before_tool", "after_tool"]
        assert all(event["parent_call_id"] == task["call_id"] for event in tool_events)
        child_inputs = children[target].seen_messages[0]
        assert any(message.content == f"private task {target}" for message in child_inputs)
        assert any(task["display_name"] in payload["message"] for payload, _ in deliveries)
        monitor.report_tool_result(ToolMessage(content=task["result"], tool_call_id=task["request_call_id"]))
    await sink.flush(context.run_id)
    returned = [event for event in await store.list_events(context.run_id) if event["event"] == "tool_returned"]
    assert {event["call_id"] for event in returned} == {task["call_id"] for task in tasks}
    assert "private task" not in str(events)
    assert len((await store.history(context.run_id))["subtasks"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_extra_task_prompt_cannot_silently_drop_constraints_and_corrected_description_reaches_child(
    runtime, asynchronous
):
    context, store, sink, _ = runtime
    description = "只查询Shopify官方订单状态，max_results=1；不得扩展到行业趋势。"
    parent = ScriptedModel(
        responses=[
            call("task", {"subagent_type": "search", "description": "订单状态", "prompt": description}, "bad-args"),
            call("task", {"subagent_type": "search", "description": description}, "corrected-args"),
            AIMessage(content="done"),
        ]
    )
    child = ScriptedModel(responses=[AIMessage(content="official documentation result")])
    agent = build(SubagentRegistry([spec("search")]), parent, {"search": child})
    state = (
        await agent.ainvoke({"messages": [("user", description)]})
        if asynchronous
        else await asyncio.to_thread(agent.invoke, {"messages": [("user", description)]})
    )
    refused = next(m for m in state["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "bad-args")
    assert refused.status == "error" and "description" in refused.text and "prompt" in refused.text
    # The first proposal never entered the executor; the corrected task is received intact.
    assert len(child.seen_messages) == 1
    assert any(m.content == description for m in child.seen_messages[0])
    assert any(isinstance(m, ToolMessage) and m.status == "error" for m in parent.seen_messages[1])
    await sink.flush(context.run_id)
    tasks = await store.list_subtasks(context.run_id)
    assert len(tasks) == 1 and tasks[0]["request_call_id"] == "corrected-args"
    assert tasks[0]["description_length"] == len(description)
    assert context.call_counts["tool"] == 1 and context.call_counts["main_model"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["missing", "general-purpose", "联网搜索助手", "knowledge"])
async def test_unknown_and_default_targets_rejected_before_routing(runtime, target):
    context, store, sink, _ = runtime
    parent = ScriptedModel(
        responses=[call("task", {"subagent_type": target, "description": "query"}), AIMessage(content="done")]
    )
    child = ScriptedModel(responses=[AIMessage(content="must not run")])
    state = await build(SubagentRegistry([spec("search")]), parent, {"search": child}).ainvoke(
        {"messages": [("user", "query")]}
    )
    result = next(message for message in state["messages"] if isinstance(message, ToolMessage))
    assert result.status == "error" and child.seen_messages == [] and len(parent.seen_messages) == 2
    await sink.flush(context.run_id)
    assert await store.list_subtasks(context.run_id) == [] and context.call_counts["tool"] == 0


@pytest.mark.asyncio
async def test_before_delegate_is_at_actual_executor_boundary(runtime):
    context, store, sink, _ = runtime
    original = default_pipeline()

    def deny(call):
        assert call.actor == "search" and call.call_id != "model-call"
        raise HookRejected("target blocked")

    handlers = dict(original.handlers)
    handlers["before_delegate"] = (deny, *handlers["before_delegate"])
    hooks = HookPipeline(handlers)
    parent = ScriptedModel(
        responses=[
            call("task", {"subagent_type": "search", "description": "query"}, "model-call"),
            AIMessage(content="done"),
        ]
    )
    child = ScriptedModel(responses=[AIMessage(content="must not run")])
    state = await build(SubagentRegistry([spec("search")]), parent, {"search": child}, hooks=hooks).ainvoke(
        {"messages": [("user", "query")]}
    )
    assert child.seen_messages == [] and context.call_counts["tool"] == 1 and context.call_counts["sub_model"] == 0
    assert next(message for message in state["messages"] if isinstance(message, ToolMessage)).status == "error"
    await sink.flush(context.run_id)
    tasks = await store.list_subtasks(context.run_id)
    assert len(tasks) == 1 and tasks[0]["status"] == "rejected"
    assert not any(event["event"] == "subagent_running" for event in await store.list_events(context.run_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "cancelled", "bad_result"])
async def test_executor_failure_cancellation_and_invalid_return_saved(runtime, outcome):
    context, store, sink, _ = runtime

    async def execute(inputs):
        if outcome == "failed":
            raise RuntimeError("password=secret-value failure")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        return {"messages": [ToolMessage(content="not final", tool_call_id="bad")]}

    wrapper = DelegationRunnable(spec("db"), RunnableLambda(execute))
    with pytest.raises(asyncio.CancelledError if outcome == "cancelled" else RuntimeError):
        await wrapper.ainvoke({"messages": [HumanMessage(content="query")]})
    await sink.flush(context.run_id)
    task = (await store.list_subtasks(context.run_id))[0]
    assert task["status"] == ("cancelled" if outcome == "cancelled" else "failed") and task["result"] is None
    assert task["ended_at"] and active_call.get() is None and "secret-value" not in str(task)


@pytest.mark.asyncio
async def test_reused_model_call_id_gets_distinct_server_ids(runtime):
    context, store, sink, _ = runtime
    parent = ScriptedModel(
        responses=[
            call("task", {"subagent_type": "db", "description": "first"}, "same-id"),
            call("task", {"subagent_type": "db", "description": "second"}, "same-id"),
            AIMessage(content="done"),
        ]
    )
    child = ScriptedModel(responses=[AIMessage(content="first result"), AIMessage(content="second result")])
    await build(SubagentRegistry([spec("db")]), parent, {"db": child}).ainvoke({"messages": [("user", "query")]})
    await sink.flush(context.run_id)
    tasks = await store.list_subtasks(context.run_id)
    assert len(tasks) == len({task["call_id"] for task in tasks}) == 2
    assert {task["result"] for task in tasks} == {"first result", "second result"}


@pytest.mark.asyncio
async def test_expired_deadline_stops_executor(runtime):
    context, store, sink, _ = runtime
    context.deadline = datetime.now(UTC) - timedelta(seconds=1)
    calls = []
    wrapper = DelegationRunnable(spec("search"), RunnableLambda(lambda state: calls.append(1)))
    with pytest.raises(RunStopped, match="timed_out"):
        await wrapper.ainvoke({"messages": [HumanMessage(content="query")]})
    await sink.flush(context.run_id)
    assert calls == [] and (await store.list_subtasks(context.run_id))[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_parallel_sessions_and_rounds_never_mix(runtime, sandbox_dir):
    _, store, sink, _ = runtime
    contexts = [
        RunContext(f"round-{i}", "same" if i < 2 else "other", sandbox_dir / str(i), event_sink=sink) for i in range(3)
    ]

    async def run(context):
        token = set_run_context(context)
        try:
            await sink.create(context.thread_id, "query", context.run_id)
            await sink.start(context)
            wrapper = DelegationRunnable(
                spec("search"),
                RunnableLambda(lambda state: {"messages": [AIMessage(content=get_run_context().run_id)]}),
            )
            await wrapper.ainvoke({"messages": [HumanMessage(content="query")]})
            await sink.finish(context, "completed", answer=context.run_id)
        finally:
            reset_run_context(token)

    await run(contexts[0])
    await asyncio.gather(*(run(context) for context in contexts[1:]))
    for context in contexts:
        tasks = await store.list_subtasks(context.run_id)
        assert len(tasks) == 1 and tasks[0]["result"] == context.run_id and tasks[0]["thread_id"] == context.thread_id


@pytest.mark.asyncio
async def test_task_row_and_event_roll_back_together(runtime, monkeypatch):
    context, store, sink, _ = runtime
    original = store._append_event

    async def broken(event):
        if event["event"].startswith("subagent_"):
            raise OSError("subtask event storage unavailable")
        return await original(event)

    monkeypatch.setattr(store, "_append_event", broken)
    with pytest.raises(EventPersistenceError, match="NOT saved"):
        await DelegationRunnable(
            spec("db"), RunnableLambda(lambda state: {"messages": [AIMessage(content="real result")]})
        ).ainvoke({"messages": [HumanMessage(content="query")]})
    with pytest.raises(EventPersistenceError):
        await sink.flush(context.run_id)
    assert context.hook_failures and await store.list_subtasks(context.run_id) == []


@pytest.mark.asyncio
async def test_database_upgrade_preserves_version_two_events(runtime, sandbox_dir):
    _, store, sink, _ = runtime
    await sink.flush()
    database = sandbox_dir / "legacy.db"
    with sqlite3.connect(database) as old:
        # Copy current schema and data excluding the new table, then simulate v2.
        with sqlite3.connect(sandbox_dir / "runs.db") as current:
            dump = "\n".join(line for line in current.iterdump() if "subtasks" not in line and "results" not in line)
        old.executescript(dump)
        old.execute("PRAGMA user_version = 2")
    upgraded = await RunStore.open(database)
    try:
        assert (await upgraded.get("round"))["thread_id"] == "session"
        assert len(await upgraded.list_events("round")) == len(await store.list_events("round"))
        assert await upgraded.list_subtasks("round") == []
    finally:
        await upgraded.close()


def test_registry_validation_and_import_contract():
    registry = SubagentRegistry([spec("db")])
    assert registry.get("db").model_config.max_retries == 0
    assert registry.get("db").allowed_skill_ids == () and not registry.get("db").review_config.enabled
    with pytest.raises(ValueError):
        SubagentRegistry([spec("db"), spec("db")])
    with pytest.raises(ValueError):
        registry.get("数据库查询助手")
    with pytest.raises(TypeError):
        registry.specs["search"] = spec("search")


@pytest.mark.asyncio
async def test_parallel_task_calls_are_linked_without_duplicate_hooks(runtime):
    context, store, sink, _ = runtime
    parent = ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "task", "args": {"subagent_type": target, "description": target}, "id": target}
                    for target in DISPLAY_NAMES
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    children = {target: ScriptedModel(responses=[AIMessage(content=target)]) for target in DISPLAY_NAMES}
    await build(SubagentRegistry([spec(target) for target in DISPLAY_NAMES]), parent, children).ainvoke(
        {"messages": [("user", "query")]}
    )
    await sink.flush(context.run_id)
    tasks = await store.list_subtasks(context.run_id)
    assert len(tasks) == len(DISPLAY_NAMES) and {task["request_call_id"] for task in tasks} == set(DISPLAY_NAMES)
    assert context.call_counts["tool"] == len(DISPLAY_NAMES) and context.call_counts["sub_model"] == len(DISPLAY_NAMES)
    events = await store.list_events(context.run_id)
    assert len([event for event in events if event["event"] == "before_delegate"]) == len(DISPLAY_NAMES)
    assert len([event for event in events if event["event"] == "after_subagent"]) == len(DISPLAY_NAMES)


@pytest.mark.asyncio
@pytest.mark.parametrize("description", ["", "   "])
async def test_empty_descriptions_rejected_before_route(runtime, description):
    context, store, sink, _ = runtime
    parent = ScriptedModel(
        responses=[call("task", {"subagent_type": "db", "description": description}), AIMessage(content="done")]
    )
    child = ScriptedModel(responses=[AIMessage(content="must not run")])
    state = await build(SubagentRegistry([spec("db")]), parent, {"db": child}).ainvoke(
        {"messages": [("user", "query")]}
    )
    await sink.flush(context.run_id)
    assert (
        child.seen_messages == []
        and context.call_counts["tool"] == 0
        and await store.list_subtasks(context.run_id) == []
    )
    assert next(message for message in state["messages"] if isinstance(message, ToolMessage)).status == "error"


@pytest.mark.asyncio
async def test_late_result_cannot_overwrite_cancelled_parent_or_terminal_subtask(runtime):
    context, store, sink, _ = runtime
    started, release = asyncio.Event(), asyncio.Event()

    async def execute(state):
        started.set()
        await release.wait()
        return {"messages": [AIMessage(content="late success")]}

    task = asyncio.create_task(
        DelegationRunnable(spec("db"), RunnableLambda(execute)).ainvoke({"messages": [HumanMessage(content="query")]})
    )
    await started.wait()
    await sink.flush(context.run_id)
    await sink.finish(context, "cancelled", error="user cancelled")
    release.set()
    await task
    await sink.flush(context.run_id)
    saved = (await store.list_subtasks(context.run_id))[0]
    assert saved["status"] == "cancelled" and saved["result"] is None
    assert (await store.get(context.run_id))["status"] == "cancelled"
    changed = dict(saved, status="completed", result="overwrite")
    from agent.runtime.events import make_event

    event = make_event(context.thread_id, context.run_id, "subagent_completed", "late", call_id=saved["call_id"])
    assert await store.record_subtask(changed, event) is None
    assert (await store.list_subtasks(context.run_id))[0] == saved


@pytest.mark.asyncio
async def test_same_call_id_cannot_be_moved_to_another_run(runtime, sandbox_dir):
    context, store, sink, _ = runtime
    await DelegationRunnable(
        spec("db"), RunnableLambda(lambda state: {"messages": [AIMessage(content="answer")]})
    ).ainvoke({"messages": [HumanMessage(content="query")]})
    await sink.flush(context.run_id)
    saved = (await store.list_subtasks(context.run_id))[0]
    await sink.create("other-session", "query", "other-round")
    changed = dict(saved, run_id="other-round", thread_id="other-session")
    from agent.runtime.events import make_event

    event = make_event("other-session", "other-round", "subagent_completed", "invalid", call_id=saved["call_id"])
    with pytest.raises(ValueError, match="identity"):
        await store.record_subtask(changed, event)
    assert (await store.list_subtasks(context.run_id))[0] == saved and await store.list_subtasks("other-round") == []


@pytest.mark.asyncio
async def test_http_history_contains_subtasks_and_chinese_display_names(runtime, monkeypatch):
    import httpx

    import api.server as server

    context, store, sink, _ = runtime
    monkeypatch.setattr(server.app.state, "run_store", store, raising=False)
    await DelegationRunnable(
        spec("search"), RunnableLambda(lambda state: {"messages": [AIMessage(content="answer")]})
    ).ainvoke({"messages": [HumanMessage(content="query")]})
    await sink.flush(context.run_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.get(f"/api/runs/{context.run_id}/history")
    assert response.status_code == 200
    assert response.json()["subtasks"][0]["display_name"] == DISPLAY_NAMES["search"]
    assert any(DISPLAY_NAMES["search"] in event["message"] for event in response.json()["events"])


@pytest.mark.asyncio
async def test_factory_child_tool_rejection_prevents_actual_business_function(runtime):
    context, store, sink, _ = runtime
    calls = []

    @tool
    def probe():
        """Fake business query."""
        calls.append(1)
        return "must not happen"

    original = default_pipeline()

    def deny(call):
        if call.actor == "search:probe":
            raise HookRejected("Business query blocked")

    handlers = dict(original.handlers)
    handlers["before_tool"] = (deny, *handlers["before_tool"])
    hooks = HookPipeline(handlers)
    parent = ScriptedModel(
        responses=[call("task", {"subagent_type": "search", "description": "query"}), AIMessage(content="done")]
    )
    child = ScriptedModel(responses=[call("probe", {}), AIMessage(content="query was blocked")])
    await build(SubagentRegistry([spec("search", (probe,))]), parent, {"search": child}, hooks=hooks).ainvoke(
        {"messages": [("user", "query")]}
    )
    await sink.flush(context.run_id)
    assert calls == [] and context.call_counts["tool"] == 1
    assert (await store.list_subtasks(context.run_id))[0]["check_status"] == "not_checked"
    assert any(
        event["status"] == "rejected" and event["actor"] == "search:probe"
        for event in await store.list_events(context.run_id)
    )


def test_factory_rejects_unbudgeted_sdk_retries():
    model = ScriptedModel(responses=[]).model_copy(update={"max_retries": 3})
    with pytest.raises(ValueError, match="retries"):
        build_subagents(SubagentRegistry([spec("db")]), model)


@pytest.mark.asyncio
async def test_factory_child_cannot_delegate_to_unregistered_generic_agent(runtime):
    context, store, sink, _ = runtime
    parent = ScriptedModel(
        responses=[call("task", {"subagent_type": "db", "description": "query"}), AIMessage(content="done")]
    )
    child = ScriptedModel(
        responses=[
            call("task", {"subagent_type": "general-purpose", "description": "bypass"}),
            AIMessage(content="bypass refused"),
        ]
    )
    await build(SubagentRegistry([spec("db")]), parent, {"db": child}).ainvoke({"messages": [("user", "query")]})
    await sink.flush(context.run_id)
    assert context.call_counts["tool"] == 1 and len(await store.list_subtasks(context.run_id)) == 1
    assert "task" not in child.tool_names
