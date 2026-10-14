"""Step 06: actual framework routing and handlers, no network or credentials."""

import asyncio
import sys
from types import SimpleNamespace

import pytest
from deepagents import create_deep_agent
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from langgraph.types import Command
from test_framework_contract import ScriptedModel, call

from agent.runtime.context import RunContext, get_run_context, reset_run_context, set_run_context
from agent.runtime.events import EventPersistenceError, EventSink
from agent.runtime.hooks import (
    HOOK_NAMES,
    HookCall,
    HookPipeline,
    HookRejected,
    HookSubagentRunnable,
    HookToolMiddleware,
    active_call,
    default_pipeline,
    finish_run,
    hook_subagents,
)
from api.run_store import RunStore


def trace_pipeline(trace, deny=None, broken_record=False):
    def handler(name):
        def emit(item):
            trace.append((name, item.actor, item.status))
            if name == deny:
                raise HookRejected("blocked by test")
            if name == "after_tool" and broken_record:
                raise OSError("record storage unavailable")

        return emit

    return HookPipeline({name: [handler(name)] for name in HOOK_NAMES})


def request(name="probe", args=None):
    return SimpleNamespace(tool_call={"name": name, "args": args or {}, "id": "real-call"})


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("path", ["success", "denied", "error"])
@pytest.mark.asyncio
async def test_order_and_exact_execution_count(asynchronous, path):
    trace, executions = [], []
    hooks = trace_pipeline(trace, deny="before_tool" if path == "denied" else None)
    wrapper = HookToolMiddleware(hooks=hooks)

    def execute(req):
        trace.append(("business", "probe", "running"))
        executions.append(1)
        if path == "error":
            raise ValueError("business failed")
        return ToolMessage(content="ok", tool_call_id=req.tool_call["id"])

    async def aexecute(req):
        return execute(req)

    if path == "error":
        with pytest.raises(ValueError, match="business failed"):
            if asynchronous:
                await wrapper.awrap_tool_call(request(), aexecute)
            else:
                wrapper.wrap_tool_call(request(), execute)
    else:
        result = (
            await wrapper.awrap_tool_call(request(), aexecute)
            if asynchronous
            else wrapper.wrap_tool_call(request(), execute)
        )
        assert result.tool_call_id == "real-call"
        assert result.status == ("error" if path == "denied" else "success")
    expected = {
        "success": ["before_tool", "business", "after_tool"],
        "denied": ["before_tool", "on_error", "after_tool"],
        "error": ["before_tool", "business", "on_error", "after_tool"],
    }
    assert [item[0] for item in trace] == expected[path]
    assert len(executions) == (0 if path == "denied" else 1)
    assert active_call.get() is None


@pytest.mark.asyncio
async def test_actual_graph_default_tools_and_custom_file_tool_cannot_bypass(sandbox_dir):
    names = ["write_todos", "ls", "read_file", "write_file", "edit_file", "glob", "grep", "generate_markdown"]
    args = [
        {"todos": []},
        {"path": "/"},
        {"file_path": "/x"},
        {"file_path": "/x", "content": "x"},
        {"file_path": "/x", "old_string": "x", "new_string": "y"},
        {"pattern": "*"},
        {"pattern": "x"},
        {"content": "x", "filename": "x"},
    ]
    executions = []

    @tool
    def generate_markdown(content: str, filename: str):
        """Fake file business tool."""
        executions.append(filename)
        return content

    model = ScriptedModel(
        responses=[
            *[call(name, arg, f"call-{name}") for name, arg in zip(names, args, strict=True)],
            AIMessage(content="done"),
        ]
    )
    trace = []
    agent = create_deep_agent(
        model=model,
        tools=[generate_markdown],
        middleware=[HookToolMiddleware(hooks=trace_pipeline(trace, deny="before_tool"))],
    )
    state = await agent.ainvoke({"messages": [("user", "try tools")]})
    messages = [item for item in state["messages"] if isinstance(item, ToolMessage)]
    assert len(messages) == len(names)
    assert all(item.status == "error" and "blocked by test" in item.content for item in messages)
    assert executions == [] and state.get("files", {}) == {} and state.get("todos", []) == []
    assert len([item for item in trace if item[0] == "before_tool"]) == len(names)
    assert model.tool_names == set(names) | {"task"}


@pytest.mark.asyncio
@pytest.mark.parametrize("compiled", [False, True])
async def test_real_task_routes_child_tools_and_returns_once(compiled):
    trace, executions = [], []
    hooks = trace_pipeline(trace)

    @tool
    def probe(value: str):
        """Return evidence."""
        executions.append(value)
        return "evidence"

    child = ScriptedModel(
        responses=[call("probe", {"value": "child"}, "child-tool"), AIMessage(content="child answer")]
    )
    spec = {
        "name": "expert",
        "description": "evidence expert",
        "system_prompt": "query",
        "model": child,
        "tools": [probe],
        "middleware": [HookToolMiddleware("expert", hooks=hooks)],
    }
    if compiled:
        executor = create_agent(child, tools=[probe], middleware=[HookToolMiddleware("expert", hooks=hooks)])
        spec = {
            "name": "expert",
            "description": "evidence expert",
            "runnable": HookSubagentRunnable("expert", executor, hooks=hooks),
        }
    parent = ScriptedModel(
        responses=[
            call("task", {"subagent_type": "expert", "description": "query"}, "delegate"),
            AIMessage(content="done"),
        ]
    )
    agent = create_deep_agent(
        model=parent, subagents=[spec], middleware=[HookToolMiddleware(allowed_targets=["expert"], hooks=hooks)]
    )
    state = await agent.ainvoke({"messages": [("user", "query")]})
    assert executions == ["child"]
    assert [item[0] for item in trace] == [
        "before_delegate",
        "before_tool",
        "before_tool",
        "after_tool",
        "after_tool",
        "after_subagent",
    ]
    assert next(item for item in state["messages"] if isinstance(item, ToolMessage)).content == "child answer"
    assert active_call.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["general-purpose", "missing", "expert"])
async def test_delegation_rejected_before_real_child_execution(target):
    trace = []
    child = ScriptedModel(responses=[AIMessage(content="must not run")])
    parent = ScriptedModel(
        responses=[call("task", {"subagent_type": target, "description": "query"}), AIMessage(content="done")]
    )
    agent = create_deep_agent(
        model=parent,
        subagents=[{"name": "expert", "description": "expert", "system_prompt": "query", "model": child}],
        middleware=[
            HookToolMiddleware(allowed_targets=["expert"], hooks=trace_pipeline(trace, deny="before_delegate"))
        ],
    )
    state = await agent.ainvoke({"messages": [("user", "query")]})
    assert child.seen_messages == []
    assert len(parent.seen_messages) == 2  # GP uses parent model; it must not run either.
    assert next(item for item in state["messages"] if isinstance(item, ToolMessage)).status == "error"
    assert not any(item[0] == "after_subagent" for item in trace)


@pytest.mark.asyncio
async def test_cancelled_tool_cleanup_and_command_identity():
    trace = []
    wrapper = HookToolMiddleware(hooks=trace_pipeline(trace))

    async def cancelled(req):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await wrapper.awrap_tool_call(request(), cancelled)
    assert [item[0] for item in trace] == ["before_tool", "on_error", "after_tool"]
    assert active_call.get() is None
    result = Command(update={"files": {"/test": "data"}})
    assert wrapper.wrap_tool_call(request(), lambda req: result) is result


@pytest.mark.asyncio
async def test_record_failure_preserves_result_and_durable_success(sandbox_dir, monkeypatch, caplog):
    import agent.runtime.hooks as module

    store = await RunStore.open(sandbox_dir / "runs.db")
    sink = EventSink(store, lambda *args: None)
    context = RunContext("record-failure", "thread", sandbox_dir, event_sink=sink)
    token = set_run_context(context)
    trace = []
    hooks = trace_pipeline(trace, broken_record=True)
    monkeypatch.setattr(module, "pipeline", hooks)
    try:
        await sink.create(context.thread_id, "query", context.run_id)
        await sink.start(context)
        result = ToolMessage(content="real success", tool_call_id="real-call")
        assert HookToolMiddleware(hooks=hooks).wrap_tool_call(request(), lambda req: result) is result
        with pytest.raises(EventPersistenceError, match="Hook recording incomplete"):
            await finish_run(context, HookCall("main", context.run_id, status="completed"), answer="real success")
        saved = await store.get(context.run_id)
        assert saved["status"] == "completed" and saved["answer"] == "real success"
        assert (await store.history(context.run_id))["events"][-1]["data"]["recording_status"] == "incomplete"
        assert "NOT saved" in caplog.text
    finally:
        reset_run_context(token)
        await sink.close()
        await store.close()


def test_fixed_handler_order_and_child_configuration():
    order = []
    hooks = HookPipeline({"before_run": [lambda item: order.append(1), lambda item: order.append(2)]})
    hooks.emit("before_run", HookCall("main", "run"))
    assert order == [1, 2]
    specs = hook_subagents([{"name": name, "tools": []} for name in ("db", "search", "knowledge")])
    assert all(isinstance(spec["middleware"][-1], HookToolMiddleware) for spec in specs)
    assert get_run_context() is None


@pytest.mark.asyncio
async def test_production_builder_and_persisted_child_correlation(sandbox_dir, monkeypatch):
    import agent.main_agent as runtime

    executions = []

    @tool
    def probe(value: str):
        """Fake business query."""
        executions.append(value)
        assert (
            active_call.get().parent_call_id == get_run_context().delegation_calls[f"delegate-{value}"][-1]["call_id"]
        )
        return "private document must not be logged"

    responses = []
    for target in ("db", "search", "knowledge"):
        responses.extend(
            [
                call("task", {"subagent_type": target, "description": "query"}, f"delegate-{target}"),
                call("probe", {"value": target}, f"tool-{target}"),
                AIMessage(content=f"answer-{target}"),
            ]
        )
    responses.append(AIMessage(content="done"))
    model = ScriptedModel(responses=responses)
    fake_modules = {
        "agent.llm": SimpleNamespace(model=model),
        "agent.prompts": SimpleNamespace(main_agent_content={"system_prompt": "query"}),
        "tools.markdown_tools": SimpleNamespace(generate_markdown=probe),
        "tools.pdf_tools": SimpleNamespace(convert_md_to_pdf=None),
        "tools.upload_file_read_tool": SimpleNamespace(read_file_content=None),
    }

    # Separate inert tools preserve production builder's three file-tool slots.
    @tool
    def fake_pdf():
        """Fake PDF conversion."""
        return "pdf"

    @tool
    def fake_read():
        """Fake upload read."""
        return "upload"

    fake_modules["tools.pdf_tools"].convert_md_to_pdf = fake_pdf
    fake_modules["tools.upload_file_read_tool"].read_file_content = fake_read
    for module, target, variable in (
        ("database_query_agent", "db", "database_query_agent"),
        ("network_search_agent", "search", "network_search_agent"),
        ("knowledge_base_agent", "knowledge", "knowledge_base_agent"),
    ):
        fake_modules[f"agent.subagents.{module}"] = SimpleNamespace(
            **{variable: {"name": target, "description": target, "system_prompt": "query", "tools": [probe]}}
        )
    for name, module in fake_modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    store = await RunStore.open(sandbox_dir / "runs.db")
    sink = EventSink(store, lambda *args: None)
    context = RunContext("production", "thread", sandbox_dir, event_sink=sink)
    token = set_run_context(context)
    try:
        await sink.create(context.thread_id, "query", context.run_id)
        await sink.start(context)
        state = await runtime._build_agent(None).ainvoke({"messages": [("user", "query")]})
        assert state["messages"][-1].content == "done"
        assert executions == ["db", "search", "knowledge"]
        assert context.call_counts == {"main_model": 4, "sub_model": 6, "review": 0, "tool": 6, "retry": 0}
        await sink.flush(context.run_id)
        events = (await store.history(context.run_id))["events"]
        for target in executions:
            child = [event for event in events if event["actor"] == f"{target}:probe"]
            assert [event["event"] for event in child] == ["before_tool", "after_tool"]
            subtask = context.delegation_calls[f"delegate-{target}"][-1]
            assert all(event["parent_call_id"] == subtask["call_id"] for event in child)
            assert all(event["call_id"] == f"tool-{target}" for event in child)
        assert len([event for event in events if event["event"] == "after_subagent"]) == 3
        assert all(
            event["data"]["check_status"] == "not_checked" for event in events if event["event"] == "after_subagent"
        )
        assert "private document" not in str(events)
    finally:
        reset_run_context(token)
        await sink.close()
        await store.close()


@pytest.mark.asyncio
async def test_before_record_failure_does_not_block_business(sandbox_dir, caplog):
    class BrokenSink:
        def submit(self, event):
            raise EventPersistenceError("closed storage")

    context = RunContext("run", "thread", sandbox_dir, event_sink=BrokenSink())
    token = set_run_context(context)
    executed = []
    try:
        HookToolMiddleware(hooks=default_pipeline()).wrap_tool_call(request(), lambda req: executed.append(1))
        assert executed == [1]
        assert len(context.hook_failures) == 2 and "NOT saved" in caplog.text
    finally:
        reset_run_context(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_run_boundary_hooks_and_context_cleanup(sandbox_dir, monkeypatch, outcome):
    import agent.runtime.hooks as module
    import api.server as server

    trace = []
    hooks = trace_pipeline(trace)
    monkeypatch.setattr(module, "pipeline", hooks)
    monkeypatch.setattr(server, "pipeline", hooks)
    store = await RunStore.open(sandbox_dir / "runs.db")
    sink = EventSink(store, lambda *args: None)
    context = RunContext("boundary", "thread", sandbox_dir, event_sink=sink)

    async def execute(query, thread):
        assert get_run_context() is context
        trace.append(("business", "main", "running"))
        if outcome == "failed":
            raise ValueError("failure")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        return "answer"

    monkeypatch.setattr(server, "run_deep_agent", execute)
    try:
        await sink.create(context.thread_id, "query", context.run_id)
        if outcome == "completed":
            await server._execute_run({"question": "query"}, context)
        else:
            with pytest.raises(ValueError if outcome == "failed" else asyncio.CancelledError):
                await server._execute_run({"question": "query"}, context)
        assert [item[0] for item in trace] == [
            "before_run",
            "business",
            *([] if outcome == "completed" else ["on_error"]),
            "after_run",
        ]
        assert (await store.get(context.run_id))["status"] == outcome
        assert get_run_context() is None
    finally:
        await sink.close()
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("deny", [False, True])
async def test_direct_compiled_executor_cannot_skip_delegation_hook(asynchronous, deny):
    trace, executions = [], []

    def execute(value):
        executions.append(value)
        return "answer"

    wrapper = HookSubagentRunnable(
        "expert", RunnableLambda(execute), hooks=trace_pipeline(trace, deny="before_delegate" if deny else None)
    )
    if deny:
        with pytest.raises(HookRejected):
            if asynchronous:
                await wrapper.ainvoke("query")
            else:
                wrapper.invoke("query")
    else:
        assert (await wrapper.ainvoke("query") if asynchronous else wrapper.invoke("query")) == "answer"
    assert len(executions) == (0 if deny else 1)
    assert [item[0] for item in trace] == ["before_delegate", "on_error" if deny else "after_subagent"]
    assert active_call.get() is None


def test_error_tool_response_and_command_are_recorded_without_rewriting():
    for command in (False, True):
        trace = []
        error = ToolMessage(content="private body", tool_call_id="real-call", status="error")
        result = Command(update={"messages": [error]}) if command else error
        wrapper = HookToolMiddleware(hooks=trace_pipeline(trace))
        assert wrapper.wrap_tool_call(request(), lambda req, result=result: result) is result
        assert [item[0] for item in trace] == ["before_tool", "on_error", "after_tool"]
        assert trace[-1][-1] == "failed"
