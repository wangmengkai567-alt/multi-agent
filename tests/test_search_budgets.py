"""Regression: independent task quotas, reserved gap searches and bounded repeated delegation."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableLambda
from pydantic import Field, ValidationError
from test_framework_contract import ScriptedModel
from test_review_contract import finding, rejected
from test_search_recovery import search, spec

from agent.reflection import ResultReviewer, ReviewResult
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.delegation import DelegationRunnable
from agent.runtime.hooks import HookRejected
from agent.runtime.policies import SearchLimitReached, SearchLimits, load_runtime_policy
from agent.runtime.search import SearchAllowance, SearchExecutionMiddleware, search_allowance
from agent.subagent_registry import ReviewConfig


@pytest.fixture
def context(sandbox_dir):
    context = RunContext("fairness", "thread", sandbox_dir, policy=load_runtime_policy())
    token = set_run_context(context)
    try:
        yield context
    finally:
        reset_run_context(token)


def test_three_tasks_cannot_steal_each_others_initial_or_refinement_slots(context):
    scopes = [SearchAllowance(context, str(index)) for index in range(3)]

    def retrieve(item):
        scope, index = item
        try:
            scope.reserve(str(index))
            return True
        except SearchLimitReached:
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(pool.map(retrieve, [(scope, index) for scope in scopes for index in range(20)])) == 6
    assert [len(scope.calls) for scope in scopes] == [2, 2, 2]
    for scope in scopes:
        scope.begin_refinement(allow_search=True)
    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(pool.map(retrieve, [(scope, index) for scope in scopes for index in range(20, 40)])) == 6
    assert len(context.search_calls) == context.policy.search.calls_per_run == 12
    assert [len(scope.calls) for scope in scopes] == [4, 4, 4]
    scopes[0].begin_refinement(allow_search=True)
    with pytest.raises(SearchLimitReached):
        scopes[0].reserve("another")
    with pytest.raises(SearchLimitReached, match="子任务数量"):
        SearchAllowance(context, "fourth")


def test_only_actual_network_retry_can_reuse_a_call_id(context):
    allowance = SearchAllowance(context)
    allowance.reserve("same")
    with pytest.raises(SearchLimitReached, match="调用 ID 已处理"):
        allowance.reserve("same")
    allowance.reserve("same", retry=True)
    assert len(context.search_calls) == 1


def test_initial_search_is_forced_for_each_new_task(context):
    first = SearchAllowance(context, "first")
    first.reserve("one")
    first.reserve("two")
    second = SearchAllowance(context, "second")
    token = search_allowance.set(second)
    try:
        request = ModelRequest(
            model=ScriptedModel(responses=[]),
            messages=[],
            tools=[{"name": "internet_search"}],
            system_message=SystemMessage(content="Search"),
        )
        result = SearchExecutionMiddleware().final_request(request)
        assert result.tool_choice == {"type": "function", "function": {"name": "internet_search"}}
        assert context.reference_time.isoformat() in result.system_message.text
    finally:
        search_allowance.reset(token)


@pytest.mark.parametrize("field,value", [("calls_per_run", 11), ("research_calls_per_run", 23)])
def test_configuration_rejects_capacity_that_can_starve_admitted_tasks(field, value):
    with pytest.raises(ValidationError, match="capacity"):
        SearchLimits(**{field: value})


@pytest.mark.asyncio
@pytest.mark.parametrize("sync", [False, True])
async def test_missing_launch_date_can_be_searched_after_initial_quota_without_starving_other_object(context, sync):
    initial_done, second_done = asyncio.Event(), asyncio.Event()
    actual, z7_inputs = [], []
    url = "https://example.test/forecast"

    async def z7(state):
        z7_inputs.append(state)
        if len(z7_inputs) == 1:
            search("Z7-overview", actual)
            search("Z7-specs", actual)
            initial_done.set()
            await second_done.wait()
            return {"messages": [AIMessage(content="Z7 specifications; launch date not yet checked " + url)]}
        assert "launch date" in state["messages"][-1].text
        assert not search_allowance.get().exhausted()
        search("Z7-launch-date", actual)
        search("Z7-official-launch", actual)
        return {"messages": [AIMessage(content="Z7 launch date verified by two real searches " + url)]}

    async def su7(state):
        await initial_done.wait()
        search("SU7-prices", actual)
        search("SU7-specs", actual)
        second_done.set()
        return {"messages": [AIMessage(content="SU7 facts " + url)]}

    settings = ReviewConfig(enabled=True, model_check=True, required_tools=("internet_search",), require_sources=True)
    model = ScriptedModel(
        responses=[
            rejected(finding(requirement_quote="launch date", scope_quote="launch date")),
            AIMessage(content=ReviewResult(verdict="passed").model_dump_json()),
        ]
    )
    first = DelegationRunnable(spec(settings), RunnableLambda(z7), reviewer=ResultReviewer(settings, model))
    second_settings = ReviewConfig(enabled=True, required_tools=("internet_search",), require_sources=True)
    second = DelegationRunnable(spec(second_settings), RunnableLambda(su7))
    # The sync path runs the same delegation lifecycle in a worker with its own event loop.
    if sync:
        # Avoid cross-loop event dependencies: run object two between first initial and refinement.
        async def z7_sequential(state):
            z7_inputs.append(state)
            if len(z7_inputs) == 1:
                search("Z7-overview", actual)
                search("Z7-specs", actual)
                second_done.set()
                await second.ainvoke({"messages": [HumanMessage(content="SU7 specs")]})
                return {"messages": [AIMessage(content="Z7 specifications " + url)]}
            search("Z7-launch-date", actual)
            search("Z7-official-launch", actual)
            return {"messages": [AIMessage(content="Z7 launch verified " + url)]}

        initial_done.set()
        first.executor = RunnableLambda(lambda state: asyncio.run(z7_sequential(state)))
        one = await asyncio.to_thread(first.invoke, {"messages": [HumanMessage(content="Z7 launch date")]})
        results = [one]
    else:
        results = await asyncio.gather(
            first.ainvoke({"messages": [HumanMessage(content="Z7 launch date")]}),
            second.ainvoke({"messages": [HumanMessage(content="SU7 specs")]}),
        )
    assert all("verified" in r["messages"][-1].text or "SU7 facts" in r["messages"][-1].text for r in results)
    assert set(actual) == {"Z7-overview", "Z7-specs", "Z7-launch-date", "Z7-official-launch", "SU7-prices", "SU7-specs"}
    assert len(actual) == 6 and len(context.search_calls) == 6
    assert len(context.search_tasks) == 2 and sorted(len(t.calls) for t in context.search_tasks.values()) == [2, 4]


@pytest.mark.asyncio
async def test_failure_of_one_object_does_not_block_another_object(context):
    actual = []

    async def execute(state):
        if state["messages"][-1].text == "unknown object":
            return {"messages": [AIMessage(content="")]}
        search("good-search", actual)
        return {"messages": [AIMessage(content="good facts https://example.test/forecast")]}

    wrapper = DelegationRunnable(spec(ReviewConfig(enabled=True, max_refinements=0)), RunnableLambda(execute))
    bad = {"messages": [HumanMessage(content="unknown object")]}
    assert "insufficient" in (await wrapper.ainvoke(bad))["messages"][-1].text
    assert (await wrapper.ainvoke({"messages": [HumanMessage(content="another object")]}))["messages"][
        -1
    ].text.startswith("good facts")
    with pytest.raises(HookRejected, match="已完成允许的修订"):
        await wrapper.ainvoke(bad)
    assert actual == ["good-search"] and "search" not in context.unavailable_agents


@pytest.mark.asyncio
async def test_identical_parallel_task_is_rejected_before_executor_and_whitespace_cannot_refresh_it(context):
    entered, release = asyncio.Event(), asyncio.Event()
    executions = []

    async def execute(state):
        executions.append(1)
        entered.set()
        await release.wait()
        return {"messages": [AIMessage(content="facts")]}

    wrapper = DelegationRunnable(spec(ReviewConfig(enabled=False)), RunnableLambda(execute))
    task = asyncio.create_task(wrapper.ainvoke({"messages": [HumanMessage(content="same task")]}))
    await entered.wait()
    try:
        with pytest.raises(HookRejected, match="已在执行"):
            await wrapper.ainvoke({"messages": [HumanMessage(content=" same  task ")]})
    finally:
        release.set()
        await task
    with pytest.raises(HookRejected, match="已完成"):
        await wrapper.ainvoke({"messages": [HumanMessage(content="same task")]})
    assert executions == [1] and len(context.search_tasks) == 1


def test_review_json_mode_is_configurable_and_does_not_relax_evidence_checks(context):
    class RecordingModel(ScriptedModel):
        formats: list = Field(default_factory=list)

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.formats.append(kwargs.get("response_format"))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = RecordingModel(responses=[AIMessage(content=ReviewResult(verdict="passed").model_dump_json())] * 2)
    reviewer = ResultReviewer(ReviewConfig(enabled=True, model_check=True), model)
    assert reviewer.review_sync("optional fields may be marked missing", "verified answer", []).verdict == "passed"
    assert model.formats[0]["type"] == "json_schema"
    assert model.formats[0]["json_schema"]["strict"] is True
    assert model.formats[0]["json_schema"]["schema"]["properties"]["verdict"]["enum"] == ["passed", "insufficient"]
    context.policy = context.policy.model_copy(
        update={"review": context.policy.review.model_copy(update={"json_mode": False})}
    )
    assert reviewer.review_sync("task", "verified answer", []).verdict == "passed"
    assert model.formats[-1] is None
    required = ResultReviewer(ReviewConfig(enabled=True, model_check=True, required_tools=("internet_search",)), model)
    assert required.review_sync("query", "claimed facts", []).verdict == "insufficient"
    assert len(model.formats) == 2


@pytest.mark.asyncio
async def test_original_user_scope_reaches_executor_and_reviewer_without_mutating_task(context):
    context.user_query = "Compare object A and B; confirm A launch date"
    description = "Gather key facts, plus optional chip model and monthly sales"
    initial = {"messages": [HumanMessage(content=description)]}
    seen, actual = [], []

    async def execute(state):
        seen.append(state["messages"][-1].text)
        search("scoped-search", actual)
        return {
            "messages": [
                AIMessage(content="Verified launch date https://example.test/forecast; optional chip unavailable")
            ]
        }

    settings = ReviewConfig(enabled=True, model_check=True, required_tools=("internet_search",), require_sources=True)
    model = ScriptedModel(responses=[AIMessage(content=ReviewResult(verdict="passed").model_dump_json())])
    wrapper = DelegationRunnable(spec(settings), RunnableLambda(execute), reviewer=ResultReviewer(settings, model))
    result = await wrapper.ainvoke(initial)
    assert "Verified launch date" in result["messages"][-1].text
    assert context.user_query in seen[0] and description in seen[0]
    review_payload = json.loads(model.seen_messages[-1][-1].text)
    assert review_payload["user_query"] == context.user_query
    assert "optional chip unavailable" in review_payload["result"]
    assert initial["messages"][-1].text == description
    assert actual == ["scoped-search"]
