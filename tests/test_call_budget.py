"""Atomic reservations against real model and tool execution paths."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from types import SimpleNamespace

import pytest
from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from test_framework_contract import ScriptedModel, call

from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.hooks import HookPipeline, HookToolMiddleware, default_pipeline
from agent.runtime.models import budget_model
from agent.runtime.policies import BudgetExceeded, RuntimePolicy, load_runtime_policy, reserve_calls


def limits(**budgets):
    data = load_runtime_policy().model_dump()
    data["budgets"].update(budgets)
    data["retry"]["delay_seconds"] = 0.0
    return RuntimePolicy.model_validate(data)


@pytest.fixture
def context(sandbox_dir):
    context = RunContext("budget", "session", sandbox_dir, policy=limits())
    token = set_run_context(context)
    try:
        yield context
    finally:
        reset_run_context(token)


def request(name="internet_search"):
    return SimpleNamespace(tool_call={"name": name, "args": {"query": "test"}, "id": "tool-call"})


def test_atomic_reservation_between_tool_threads(context):
    context.policy = limits(tool=5, total=5)
    executions = []
    lock = Lock()

    def execute(index):
        token = set_run_context(context)
        try:
            reserve_calls("tool")
            with lock:
                executions.append(index)
        except BudgetExceeded:
            return "denied"
        finally:
            reset_run_context(token)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(execute, range(100)))
    assert len(executions) == 5 and results.count("denied") == 95
    assert context.call_counts["tool"] == 5


def test_multicategory_reservation_is_all_or_none(context):
    context.policy = limits(retry=0)
    with pytest.raises(BudgetExceeded):
        reserve_calls("retry", "tool")
    assert sum(context.call_counts.values()) == 0


@pytest.mark.asyncio
async def test_async_reservations_share_total_budget(context):
    context.policy = limits(total=7)

    async def reserve(kind):
        await asyncio.sleep(0)
        try:
            reserve_calls(kind)
            return 1
        except BudgetExceeded:
            return 0

    results = await asyncio.gather(
        *(reserve(kind) for kind in ("main_model", "sub_model", "review", "tool", "retry") * 10)
    )
    assert sum(results) == 7 and sum(context.call_counts.values()) == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("kind", ["main_model", "sub_model", "review"])
async def test_model_denied_before_underlying_invocation(context, asynchronous, kind):
    context.policy = limits(**{kind: 1})
    underlying = ScriptedModel(responses=[AIMessage(content="first"), AIMessage(content="must not execute")])
    model = budget_model(underlying, kind)
    if asynchronous:
        assert (await model.ainvoke("one")).content == "first"
        with pytest.raises(BudgetExceeded):
            await model.ainvoke("two")
    else:
        assert model.invoke("one").content == "first"
        with pytest.raises(BudgetExceeded):
            model.invoke("two")
    assert len(underlying.seen_messages) == 1 and context.call_counts[kind] == 1


@pytest.mark.asyncio
async def test_actual_task_model_tool_and_total_counts(context):
    executions = []

    @tool
    def evidence():
        """Fake evidence lookup."""
        executions.append(1)
        return "facts"

    child = ScriptedModel(responses=[call("evidence", {}), AIMessage(content="child")])
    parent = ScriptedModel(
        responses=[call("task", {"subagent_type": "expert", "description": "query"}), AIMessage(content="done")]
    )
    agent = create_deep_agent(
        model=budget_model(parent, "main_model"),
        subagents=[
            {
                "name": "expert",
                "description": "expert",
                "system_prompt": "query",
                "tools": [evidence],
                "model": budget_model(child, "sub_model"),
                "middleware": [HookToolMiddleware("expert")],
            }
        ],
        middleware=[HookToolMiddleware(allowed_targets=["expert"])],
    )
    state = await agent.ainvoke({"messages": [("user", "query")]})
    assert state["messages"][-1].content == "done" and executions == [1]
    assert context.call_counts == {"main_model": 2, "sub_model": 2, "review": 0, "tool": 2, "retry": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_retry_reserves_tool_and_retry_before_each_attempt(context, asynchronous):
    executions = []
    original = default_pipeline()
    events = []
    hooks = HookPipeline(
        {
            name: (*handlers, lambda item, name=name: events.append((name, item.attempt, item.status)))
            for name, handlers in original.handlers.items()
        }
    )
    wrapper = HookToolMiddleware(hooks=hooks)

    def execute(req):
        executions.append(dict(context.call_counts))
        if len(executions) == 1:
            raise ConnectionError("temporary")
        return ToolMessage(content="ok", tool_call_id="tool-call")

    async def aexecute(req):
        return execute(req)

    result = (
        await wrapper.awrap_tool_call(request(), aexecute)
        if asynchronous
        else wrapper.wrap_tool_call(request(), execute)
    )
    assert result.content == "ok" and len(executions) == 2
    assert executions[0]["tool"] == 1 and executions[0]["retry"] == 0
    assert executions[1]["tool"] == 2 and executions[1]["retry"] == 1
    assert sum(context.call_counts.values()) == 3
    assert [event[0] for event in events] == ["before_tool", "on_error", "after_tool", "before_tool", "after_tool"]


@pytest.mark.asyncio
async def test_retry_cannot_exceed_total_or_category_budget(context):
    context.policy = limits(total=2)
    executions = []

    async def failed(req):
        executions.append(1)
        raise ConnectionError("temporary")

    result = await HookToolMiddleware().awrap_tool_call(request(), failed)
    assert result.status == "error" and "budget" in result.content
    assert executions == [1] and context.call_counts["retry"] == 0 and context.call_counts["tool"] == 1


def test_repeated_rejections_still_exhaust_main_model_budget(context):
    context.policy = limits(main_model=2)
    model = ScriptedModel(
        responses=[call("task", {"subagent_type": "missing", "description": "query"}) for _ in range(3)]
    )
    agent = create_deep_agent(model=budget_model(model, "main_model"), middleware=[HookToolMiddleware()])
    with pytest.raises(BudgetExceeded):
        agent.invoke({"messages": [("user", "query")]})
    assert len(model.seen_messages) == 2 and context.call_counts["tool"] == 0


def test_framework_summarizer_uses_same_model_budget(context):
    from langchain.agents.middleware.summarization import SummarizationMiddleware

    underlying = ScriptedModel(responses=[AIMessage(content="summary")])
    context.policy = limits(main_model=1)
    summarizer = SummarizationMiddleware(
        model=budget_model(underlying, "main_model"), trigger=("messages", 1), keep=("messages", 1)
    )
    # Run the framework's actual summary creation, outside normal model middleware.
    from langchain_core.messages import HumanMessage

    assert summarizer._create_summary([HumanMessage(content="summarize")]) == "summary"
    with pytest.raises(BudgetExceeded):
        summarizer.model.invoke("another summary")
    assert len(underlying.seen_messages) == 1


def test_two_runs_do_not_share_counters(context, sandbox_dir):
    reserve_calls("tool")
    other = RunContext("other", "other-session", sandbox_dir / "other", policy=limits())
    token = set_run_context(other)
    try:
        reserve_calls("review")
        assert other.call_counts["tool"] == 0 and other.call_counts["review"] == 1
    finally:
        reset_run_context(token)
    assert context.call_counts["tool"] == 1 and context.call_counts["review"] == 0
