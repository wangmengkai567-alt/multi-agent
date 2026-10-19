"""Bounded review repair and run-wide search quotas across delegation/refinement."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from test_framework_contract import ScriptedModel
from test_review_contract import finding, rejected

from agent.reflection import ResultReviewer, ReviewResult
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.delegation import DelegationRunnable
from agent.runtime.hooks import HookRejected, HookToolMiddleware
from agent.runtime.policies import load_runtime_policy
from agent.runtime.search import SearchAllowance, search_allowance
from agent.subagent_registry import DISPLAY_NAMES, ReviewConfig, SubagentSpec

URL = "https://example.test/forecast"


@pytest.fixture
def context(sandbox_dir):
    context = RunContext("round", "session", sandbox_dir, policy=load_runtime_policy())
    token = set_run_context(context)
    try:
        yield context
    finally:
        reset_run_context(token)


def spec(settings):
    return SubagentSpec("search", DISPLAY_NAMES["search"], "Find facts", "Return facts", (), review_config=settings)


def search(call_id, actual):
    request = SimpleNamespace(tool_call={"name": "internet_search", "id": call_id, "args": {"query": "forecast"}})

    def execute(request):
        actual.append(call_id)
        return ToolMessage(content=json.dumps({"results": [{"url": URL, "content": "forecast"}]}), tool_call_id=call_id)

    return HookToolMiddleware("search").wrap_tool_call(request, execute)


@pytest.mark.asyncio
@pytest.mark.parametrize("sync", [False, True])
async def test_review_schema_repair_reuses_same_search_results(context, sync):
    actual, executions = [], []

    def execute(state):
        executions.append(state)
        search("one", actual)
        search("two", actual)
        return {"messages": [AIMessage(content="forecast " + URL)]}

    model = ScriptedModel(
        responses=[
            AIMessage(content="invalid JSON"),
            AIMessage(content=ReviewResult(verdict="passed").model_dump_json()),
        ]
    )
    settings = ReviewConfig(enabled=True, model_check=True, required_tools=("internet_search",), require_sources=True)
    wrapper = DelegationRunnable(spec(settings), RunnableLambda(execute), reviewer=ResultReviewer(settings, model))
    inputs = {"messages": [HumanMessage(content="forecast")]}
    result = await asyncio.to_thread(wrapper.invoke, inputs) if sync else await wrapper.ainvoke(inputs)
    assert result["messages"][-1].text == "forecast " + URL
    assert actual == ["one", "two"] and len(executions) == 1
    assert context.call_counts["review"] == 2 and context.call_counts["retry"] == 1
    assert model.seen_messages[0][-1] in model.seen_messages[1]
    assert "仅重新检查" in model.seen_messages[1][-1].text


@pytest.mark.asyncio
@pytest.mark.parametrize("sync", [False, True])
async def test_persistent_schema_error_is_explicit_and_never_restarts_search(context, sync):
    model = ScriptedModel(responses=[AIMessage(content='{"verdict":"passed","issues":["unresolved-secret"]}')] * 2)
    settings = ReviewConfig(enabled=True, model_check=True)
    wrapper = DelegationRunnable(
        spec(settings),
        RunnableLambda(lambda state: {"messages": [AIMessage(content="facts")]}),
        reviewer=ResultReviewer(settings, model),
    )
    inputs = {"messages": [HumanMessage(content="task")]}
    result = await asyncio.to_thread(wrapper.invoke, inputs) if sync else await wrapper.ainvoke(inputs)
    text = result["messages"][-1].text
    assert "check_incomplete" in text and "value_error" in text
    assert "unresolved-secret" not in text and len(model.seen_messages) == 2
    with pytest.raises(HookRejected, match="重复搜索不能修复检查格式"):
        await wrapper.ainvoke(inputs)


@pytest.mark.asyncio
async def test_format_repair_cannot_turn_unresolved_issues_into_automatic_pass(context):
    model = ScriptedModel(
        responses=[
            AIMessage(content='{"verdict":"passed","issues":["missing date"]}'),
            rejected(finding(requirement_quote="date", scope_quote="date")),
        ]
    )
    result = await ResultReviewer(ReviewConfig(enabled=True, model_check=True), model).review(
        "Check date", "answer", []
    )
    assert result.verdict == "insufficient" and result.missing_evidence == ["date"]


def test_json_code_fence_is_accepted_without_extra_model_call(context):
    model = ScriptedModel(responses=[AIMessage(content='```json\n{"verdict":"passed"}\n```')])
    result = ResultReviewer(ReviewConfig(enabled=True, model_check=True), model).review_sync("task", "answer", [])
    assert result.verdict == "passed" and context.call_counts["review"] == 1
    assert context.call_counts["retry"] == 0


@pytest.mark.asyncio
async def test_format_repair_obeys_call_budget(context):
    context.call_counts["review"] = context.policy.budgets.review - 1
    model = ScriptedModel(responses=[AIMessage(content="invalid JSON")])
    result = await ResultReviewer(ReviewConfig(enabled=True, model_check=True), model).review("task", "answer", [])
    assert result.verdict == "budget_exhausted" and len(model.seen_messages) == 1
    assert context.call_counts["retry"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("sync", [False, True])
async def test_refinement_keeps_draft_evidence_and_does_not_reset_search_quota(context, sync):
    actual, executions = [], []

    def execute(state):
        executions.append(state)
        if len(executions) == 1:
            search("one", actual)
            search("two", actual)
            return {"messages": [AIMessage(content="draft missing source")]}
        assert state["messages"][-2].text == "draft missing source"
        assert search_allowance.get().exhausted()
        assert search("three", actual).status == "error"
        return {"messages": [AIMessage(content="forecast " + URL)]}

    settings = ReviewConfig(enabled=True, required_tools=("internet_search",), require_sources=True)
    wrapper = DelegationRunnable(spec(settings), RunnableLambda(execute))
    inputs = {"messages": [HumanMessage(content="forecast")]}
    result = await asyncio.to_thread(wrapper.invoke, inputs) if sync else await wrapper.ainvoke(inputs)
    assert result["messages"][-1].text == "forecast " + URL
    assert actual == ["one", "two"] and len(executions) == 2
    assert context.call_counts["tool"] == 2


@pytest.mark.asyncio
async def test_new_delegation_cannot_reset_run_search_quota(context):
    actual = []

    def execute(state):
        result = search(state["messages"][-1].text, actual)
        return {"messages": [AIMessage(content=result.content)]}

    wrapper = DelegationRunnable(spec(ReviewConfig(enabled=False)), RunnableLambda(execute))
    for index in range(context.policy.search.max_tasks_per_run):
        await wrapper.ainvoke({"messages": [HumanMessage(content=str(index))]})
    assert actual == ["0", "1", "2"] and len(context.search_calls) == 3
    with pytest.raises(HookRejected, match="已完成"):
        await wrapper.ainvoke({"messages": [HumanMessage(content=" 0 ")]})
    with pytest.raises(HookRejected, match="子任务数量"):
        await wrapper.ainvoke({"messages": [HumanMessage(content="another new task")]})
    new_round = RunContext("new-round", "session", context.work_dir, policy=context.policy)
    assert not SearchAllowance(new_round).exhausted() and new_round.search_calls == set()


@pytest.mark.asyncio
async def test_exhausted_content_refinement_blocks_new_search_delegations(context):
    executions = []
    settings = ReviewConfig(enabled=True, max_refinements=0)
    wrapper = DelegationRunnable(
        spec(settings), RunnableLambda(lambda state: executions.append(1) or {"messages": [AIMessage(content="")]})
    )
    inputs = {"messages": [HumanMessage(content="task")]}
    result = await wrapper.ainvoke(inputs)
    assert json.loads(result["messages"][-1].text)["retry_allowed"] is False
    with pytest.raises(HookRejected, match="已完成允许的修订"):
        await wrapper.ainvoke(inputs)
    assert executions == [1]
