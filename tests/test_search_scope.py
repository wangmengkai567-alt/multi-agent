"""Reproduce scope leakage, omitted URL schemes and finalization after failed comparisons."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from deepagents import create_deep_agent
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import Field
from test_framework_contract import ScriptedModel

from agent.reflection import ResultReviewer, normalize_source_citations, rule_review
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.finalization import SearchFinalizationMiddleware, visible_search_summary
from agent.runtime.hooks import HookCall, HookToolMiddleware, active_call
from agent.runtime.models import budget_model
from agent.runtime.policies import SearchLimitReached, load_runtime_policy
from agent.runtime.search import SearchAllowance
from agent.subagent_factory import build_subagents
from agent.subagent_registry import DISPLAY_NAMES, ReviewConfig, SubagentRegistry, SubagentSpec


@pytest.fixture
def context(sandbox_dir):
    context = RunContext(
        "scope", "thread", sandbox_dir, policy=load_runtime_policy(), user_query="Compare WATCH FIT 4 Pro and FIT 5"
    )
    token = set_run_context(context)
    try:
        yield context
    finally:
        reset_run_context(token)


def evidence(url):
    return [
        dict(
            name="internet_search",
            call_id="source",
            executed=True,
            status="completed",
            content={"results": [{"url": url, "content": "actual source"}]},
        )
    ]


def test_citations_only_restore_a_scheme_for_an_exact_returned_page(context):
    records = evidence("https://consumer.example.test/cn/watch-fit5/specs")
    original = "Specs: consumer.example.test/cn/watch-fit5/specs。"
    normalized = normalize_source_citations(original, records)
    assert normalized == "Specs: https://consumer.example.test/cn/watch-fit5/specs。"
    settings = ReviewConfig(enabled=True, require_sources=True)
    assert rule_review(normalized, records, settings).verdict == "passed"
    for wrong in (
        "consumer.example.test",
        "evil.consumer.example.test/cn/watch-fit5/specs",
        "consumer.example.test/cn/watch-fit5/specs?other=1",
        "consumer.example.test/cn/watch-fit5/specs/other",
    ):
        assert normalize_source_citations(wrong, records) == wrong
        assert rule_review(wrong, records, settings).verdict == "insufficient"
    assert normalize_source_citations(normalized, records) == normalized


def test_review_has_explicit_current_subtask_scope_separate_from_background(context):
    call = HookCall(
        "search", "fit4", args={"description": "Collect WATCH FIT 4 Pro only; missing optional fields may be disclosed"}
    )
    token = active_call.set(call)
    try:
        _, messages = ResultReviewer(
            ReviewConfig(enabled=True, model_check=True), ScriptedModel(responses=[])
        )._prepare("wrapped task plus background", "source facts", [])
    finally:
        active_call.reset(token)
    data = json.loads(messages[-1].text)
    assert data["delegated_task"] == call.args["description"]
    assert data["user_query"] == context.user_query
    assert "task" not in data  # Do not repeat the delegation inside its context wrapper.
    assert "不得要求这个子任务检索其他并行子任务的对象" in messages[0].text


def test_quota_summary_keeps_original_acceptance_and_current_object_explicit(context):
    from agent.runtime.search import SearchExecutionMiddleware

    call = HookCall("search", "fit4", args={"description": "Collect WATCH FIT 4 Pro; prefer verbatim English"})
    token = active_call.set(call)
    try:
        allowance = SearchAllowance(context, "fit4")
        request = ModelRequest(
            model=ScriptedModel(responses=[]),
            tools=[],
            messages=[HumanMessage(content="nested task wrapper and prior revisions")],
            system_message=SystemMessage(content="protocol"),
        )
        summary = SearchExecutionMiddleware("protocol").summary_request(request, allowance)
    finally:
        active_call.reset(token)
    payload = json.loads(summary.messages[-1].text)
    assert payload["user_query"] == context.user_query
    assert payload["delegated_task"] == call.args["description"]
    assert "FIT 5" not in payload["delegated_task"]
    assert "不接管其他并行对象" in summary.system_message.text
    assert "只是建议" in summary.system_message.text
    assert summary.tools == [] and summary.model_settings["tool_choice"] == "none"


@pytest.mark.asyncio
async def test_deadline_cancellation_sentinel_is_not_exposed_as_an_error(context):
    events = []
    context.event_sink = SimpleNamespace(submit=events.append)

    async def cancel(request):
        context.stop("timed_out")
        raise asyncio.CancelledError(object())

    request = SimpleNamespace(tool_call={"name": "internet_search", "id": "cancel", "args": {"query": "forecast"}})
    with pytest.raises(asyncio.CancelledError):
        await HookToolMiddleware("search").awrap_tool_call(request, cancel)
    errors = [e["data"]["error"] for e in events if e["data"].get("error")]
    assert errors and all("达到总时限" in e and "object object" not in e for e in errors)
    assert all(e["status"] == "cancelled" for e in events if e["event"] in ("on_error", "after_tool"))


@pytest.mark.parametrize("second_status", ["insufficient", "dependency_unavailable", "tool_failed"])
def test_one_failed_task_does_not_close_other_work_and_two_do_not_spawn_rewrites(context, second_status):
    first = SearchAllowance(context, "first")
    first.claim()
    first.finish(executed=True, check_status="insufficient", result='{"review":{"issues":["missing first"]}}')
    second = SearchAllowance(context, "second")
    second.claim()
    request = ModelRequest(
        model=ScriptedModel(responses=[]),
        messages=[HumanMessage(content="old turns")],
        tools=[{"name": "task"}],
        system_message=SystemMessage(content="protocol"),
    )
    middleware = SearchFinalizationMiddleware("role")
    assert middleware.final_request(request) is request
    second.finish(executed=True, check_status=second_status, result='{"review":{"issues":["missing second"]}}')
    with pytest.raises(SearchLimitReached, match="汇总阶段"):
        SearchAllowance(context, "rewritten descriptions")
    final = middleware.final_request(request)
    assert final.tools == [] and final.model_settings["tool_choice"] == "none"
    data = json.loads(final.messages[0].text)
    assert data["verified_results"] == [] and len(data["failed_checks"]) == 2
    assert data["user_query"] == context.user_query


@pytest.mark.asyncio
async def test_real_framework_finalizes_after_two_failed_tasks_without_third_delegation(context):
    class Parent(ScriptedModel):
        choices: list = Field(default_factory=list)

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.choices.append(kwargs.get("tool_choice"))
            return super()._generate(messages, stop, run_manager, **kwargs)

    parent = Parent(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    dict(name="task", args={"subagent_type": "search", "description": name}, id=name)
                    for name in ("FIT 4 Pro facts", "FIT 5 facts")
                ],
            ),
            AIMessage(
                content='{"failed_checks": [], "verified_results": []}</think>关键依据未核实，无法给出可靠的型号推荐。'
            ),
        ]
    )
    child = ScriptedModel(responses=[AIMessage(content="") for _ in range(4)])
    settings = ReviewConfig(enabled=True)
    spec = SubagentSpec("search", DISPLAY_NAMES["search"], "Find facts", "Return facts", (), review_config=settings)
    registry = SubagentRegistry([spec])
    graph = create_deep_agent(
        model=budget_model(parent, "main_model"),
        subagents=build_subagents(registry, parent, review_model=parent, models={"search": child}),
        middleware=[
            SearchFinalizationMiddleware("Summarize actual facts"),
            HookToolMiddleware(allowed_targets=registry.specs, managed_delegation=True),
        ],
    )
    result = await graph.ainvoke({"messages": [HumanMessage(content=context.user_query)]})
    assert result["messages"][-1].text == "关键依据未核实，无法给出可靠的型号推荐。"
    assert not result["messages"][-1].additional_kwargs.get("deterministic_fallback")
    assert len(context.search_tasks) == 2 and len(parent.seen_messages) == 2
    assert len(child.seen_messages) == 4


def test_summary_cleanup_preserves_normal_text_and_unrelated_literal_markers():
    for text in ("ordinary answer", "Explain </think> literally", '{"unrelated":true}</think>quoted content'):
        assert visible_search_summary(text) == text
    assert visible_search_summary("<think>internal reasoning</think>Visible reply") == "Visible reply"
    assert visible_search_summary('{"failed_checks":[],"verified_results":[]}</think>Visible reply') == "Visible reply"
