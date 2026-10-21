"""Final source verification, bounded correction, and failure output contracts."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from deepagents import create_deep_agent
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from test_framework_contract import ScriptedModel, call

from agent.reflection import ReviewResult
from agent.runtime.answer_review import AnswerReviewMiddleware
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.finalization import SearchFinalizationMiddleware
from agent.runtime.hooks import HookToolMiddleware
from agent.runtime.models import ModelRequestTimeout, budget_model
from agent.runtime.policies import load_runtime_policy
from agent.runtime.search import SearchAllowance
from agent.subagent_factory import build_subagents
from agent.subagent_registry import DISPLAY_NAMES, ReviewConfig, SubagentRegistry, SubagentSpec

URL = "https://example.test/official"


@pytest.fixture
def context(sandbox_dir):
    context = RunContext(
        "final-review",
        "thread",
        sandbox_dir,
        policy=load_runtime_policy(),
        user_query="Explain a topic with actual source verification",
    )
    token = set_run_context(context)
    try:
        yield context
    finally:
        reset_run_context(token)


def passed():
    return AIMessage(content=ReviewResult(verdict="passed").model_dump_json())


def approved_task(context):
    scope = SearchAllowance(context, "accepted")
    scope.claim()
    scope.task.evidence = [
        dict(
            name="internet_search",
            executed=True,
            status="completed",
            call_id="one",
            content={"results": [dict(url=URL, content="checked fact")]},
        )
    ]
    scope.finish(executed=True, check_status="passed", result="Checked fact " + URL, sources=[URL])
    return scope


@pytest.mark.asyncio
@pytest.mark.parametrize("sync", [False, True])
async def test_real_graph_cannot_publish_memory_citations_without_actual_search(context, sync):
    actual = []

    @tool
    def internet_search(query: str):
        """Return actual source evidence."""
        actual.append(query)
        return json.dumps({"results": [{"url": URL, "content": "checked fact"}]})

    settings = ReviewConfig(enabled=True, required_tools=("internet_search",), require_sources=True)
    spec = SubagentSpec(
        "search", DISPLAY_NAMES["search"], "Find facts", "Return facts", (internet_search,), review_config=settings
    )
    child = ScriptedModel(
        responses=[
            call("internet_search", {"query": "official source"}, "actual-search"),
            AIMessage(content="Checked fact " + URL),
        ]
    )
    root = ScriptedModel(
        responses=[
            AIMessage(content="Unverified claimed quote " + URL),
            call("task", {"description": "Verify the actual source", "subagent_type": "search"}, "verify"),
            AIMessage(content="Checked fact " + URL),
        ]
    )
    reviewer = ScriptedModel(responses=[passed()])
    registry = SubagentRegistry([spec])
    graph = create_deep_agent(
        model=budget_model(root, "main_model"),
        subagents=build_subagents(registry, root, review_model=root, models={"search": child}),
        middleware=[
            AnswerReviewMiddleware(reviewer),
            SearchFinalizationMiddleware(),
            HookToolMiddleware(allowed_targets=registry.specs, managed_delegation=True),
        ],
    )
    inputs = {"messages": [HumanMessage(content=context.user_query)]}
    result = await asyncio.to_thread(graph.invoke, inputs) if sync else await graph.ainvoke(inputs)
    assert actual == ["official source"]
    assert result["messages"][-1].text == "Checked fact " + URL
    assert (
        context.answer_citation_repair_attempted
        and not context.answer_repair_attempted
        and len(root.seen_messages) == 3
    )
    assert len(reviewer.seen_messages) == 1


@pytest.mark.asyncio
async def test_ignored_verification_is_bounded_and_never_delivers_unchecked_claims(context):
    events = []
    context.event_sink = SimpleNamespace(submit=events.append)
    middleware = AnswerReviewMiddleware(ScriptedModel(responses=[]))
    calls = []

    async def handler(request):
        calls.append(request)
        return ModelResponse(result=[AIMessage(content="Unverified claim 99999 " + URL)])

    request = ModelRequest(
        model=ScriptedModel(responses=[]),
        tools=[{"name": "task"}],
        messages=[HumanMessage(content=context.user_query)],
        system_message=SystemMessage(content="Role"),
    )
    result = await middleware.awrap_model_call(request, handler)
    assert len(calls) == 2 and result.result[0].additional_kwargs["deterministic_fallback"]
    assert "99999" not in result.result[0].text and URL not in result.result[0].text
    assert [e["status"] for e in events if e["event"] == "answer_validation"] == ["insufficient", "insufficient"]


@pytest.mark.asyncio
async def test_root_model_timeout_returns_only_previously_approved_material(context):
    approved_task(context)
    events = []
    context.event_sink = SimpleNamespace(submit=events.append)
    middleware = AnswerReviewMiddleware(ScriptedModel(responses=[]))

    async def handler(request):
        raise ModelRequestTimeout("main_model", 60)

    request = ModelRequest(model=ScriptedModel(responses=[]), messages=[], tools=[])
    response = await middleware.awrap_model_call(request, handler)
    assert "Checked fact" in response.result[0].text
    assert response.result[0].additional_kwargs["deterministic_fallback"]
    assert len(events) == 1 and events[0]["status"] == "check_incomplete"


def test_direct_math_and_user_provided_url_are_not_forced_online(context):
    middleware = AnswerReviewMiddleware(ScriptedModel(responses=[]))
    assert middleware._candidate(ModelResponse(result=[AIMessage(content="391")])) is None
    context.user_query = "Rewrite this URL literally without browsing: " + URL
    assert middleware._candidate(ModelResponse(result=[AIMessage(content=URL)])) is None


def test_one_real_citation_does_not_justify_another_fabricated_source(context):
    scope = approved_task(context)
    middleware = AnswerReviewMiddleware(ScriptedModel(responses=[]))
    message = AIMessage(content="Checked fact " + URL + " plus https://example.test/invented")
    assert middleware._source_gap(message, scope.task.evidence).verdict == "insufficient"
    assert middleware._source_gap(AIMessage(content="Checked fact " + URL + "#section"), scope.task.evidence) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("sync", [False, True])
async def test_source_repair_names_exact_allowed_pages_without_research_or_rejecting_other_tasks(context, sync):
    approved_task(context)
    bad = SearchAllowance(context, "rejected")
    bad.claim()
    bad.task.evidence = [
        dict(
            name="internet_search",
            executed=True,
            status="completed",
            call_id="bad",
            content={"results": [dict(url="https://example.test/rejected", content="not approved")]},
        )
    ]
    bad.finish(executed=True, check_status="insufficient", result="https://example.test/rejected")
    checker = ScriptedModel(responses=[passed()])
    middleware = AnswerReviewMiddleware(checker)
    requests = []

    def execute(request):
        requests.append(request)
        if len(requests) == 1:
            return ModelResponse(result=[AIMessage(content="Checked fact https://example.test/remembered")])
        data = json.loads(request.messages[-1].text)
        assert data["allowed_source_urls"] == [URL]
        assert "rejected" not in str(data["verified_results"])
        correction = json.loads(data["revision"].split("\n", 1)[1])
        assert correction["excluded_source_urls"] == ["https://example.test/remembered"]
        assert correction["allowed_source_urls"] == [URL]
        assert request.tools == [] and request.model_settings["tool_choice"] == "none"
        return ModelResponse(result=[AIMessage(content="Checked fact " + URL)])

    request = ModelRequest(model=ScriptedModel(responses=[]), tools=[{"name": "task"}], messages=[])
    response = (
        await asyncio.to_thread(middleware.wrap_model_call, request, execute)
        if sync
        else await middleware.awrap_model_call(request, RunnableLambda(execute).ainvoke)
    )
    assert response.result[0].text == "Checked fact " + URL
    assert len(requests) == 2 and len(checker.seen_messages) == 1
    assert len(context.search_tasks) == 2 and not context.search_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,text", [("partial", "关键资料尚未核实，无法可靠推荐。"), ("complete", "Checked fact")])
async def test_final_review_checks_evidence_without_forcing_urls_into_every_answer(context, kind, text):
    from agent.reflection import FinalReviewResult

    approved_task(context)
    reviewer = ScriptedModel(
        responses=[AIMessage(content=FinalReviewResult(verdict="passed", answer_kind=kind).model_dump_json())]
    )
    middleware = AnswerReviewMiddleware(reviewer)
    calls = []

    async def handler(request):
        calls.append(request)
        return ModelResponse(result=[AIMessage(content=text)])

    response = await middleware.awrap_model_call(ModelRequest(model=reviewer, messages=[], tools=[]), handler)
    assert response.result[-1].text == text and len(calls) == 1
    assert URL in reviewer.seen_messages[0][-1].text


def test_source_correction_discards_untrusted_draft_and_preserves_original_scope(context):
    middleware = AnswerReviewMiddleware(ScriptedModel(responses=[]))
    draft = AIMessage(content="Verify every link and HTTP status: https://invented.test/ten-links")
    request = ModelRequest(
        model=ScriptedModel(responses=[]),
        messages=[HumanMessage(content=context.user_query)],
        tools=[],
        system_message=SystemMessage(content="Role"),
    )
    review = ReviewResult(verdict="insufficient", issues=["No evidence"], revision_instruction="Verify original scope")
    revised = middleware._revision(request, draft, review)
    assert all("ten-links" not in message.text for message in revised.messages)
    assert context.user_query in revised.messages[-1].text
    assert "HTTP 状态码" in revised.system_message.text


def test_final_review_has_runtime_calendar_strict_schema_and_partial_kind(context):
    from datetime import datetime

    from agent.reflection import FinalReviewResult, ResultReviewer

    context.reference_time = datetime.fromisoformat("2026-10-18T15:00:00+08:00")
    model = ScriptedModel(responses=[])
    reviewer = ResultReviewer(ReviewConfig(enabled=True, model_check=True), model, final_answer=True)
    _, messages = reviewer._prepare("tomorrow", "forecast", [])
    data = json.loads(messages[-1].text)
    assert data["calendar"] == {
        "today": "2026-10-18",
        "today_weekday": "Sunday",
        "tomorrow": "2026-10-19",
        "tomorrow_weekday": "Monday",
    }
    schema = reviewer._model_kwargs(1)["response_format"]["json_schema"]["schema"]
    assert "answer_kind" in schema["required"]
    assert schema["properties"]["answer_kind"]["enum"] == ["complete", "partial"]
    review = reviewer._parse(
        AIMessage(content=FinalReviewResult(verdict="passed", answer_kind="partial").model_dump_json())
    )
    events = []
    context.event_sink = SimpleNamespace(submit=events.append)
    AnswerReviewMiddleware(model)._record(review)
    assert events[0]["status"] == "partial" and events[0]["data"]["answer_kind"] == "partial"


def test_approved_sql_evidence_is_available_to_final_check_and_safe_fallback(context):
    from agent.runtime.finalization import partial_search_answer

    context.approved_internal["db-checked"] = {
        "text": "Query result: 5 records",
        "evidence": [
            {"name": "execute_sql_query", "executed": True, "status": "completed", "call_id": "sql", "content": "5"}
        ],
    }
    middleware = AnswerReviewMiddleware(ScriptedModel(responses=[]))
    message = AIMessage(content="Query result: 5 records")
    assert middleware._candidate(ModelResponse(result=[message])) is message
    reviewer, evidence = middleware._reviewer(message)
    assert not reviewer.settings.require_sources
    assert evidence[0]["name"] == "execute_sql_query"
    assert "5 records" in partial_search_answer(context)


def test_timeout_before_search_does_not_claim_search_results_were_obtained(context):
    from agent.runtime.finalization import partial_search_answer

    scope = SearchAllowance(context, "never-searched")
    scope.claim()
    scope.task.failure_reason = "子助手模型请求超时"
    scope.finish(executed=True, check_status="dependency_unavailable")
    text = partial_search_answer(context)
    assert "发起检索前超时，未执行网络搜索" in text
    assert "已取得" not in text


@pytest.mark.asyncio
async def test_real_database_delegation_supplies_approved_evidence_to_root_review(context):
    context.user_query = "Count actual records"

    @tool
    def probe():
        """Return a controlled database count."""
        return "5 records"

    spec = SubagentSpec(
        "db",
        DISPLAY_NAMES["db"],
        "Query records",
        "Return records",
        (probe,),
        review_config=ReviewConfig(enabled=True, required_tools=("probe",)),
    )
    child = ScriptedModel(responses=[call("probe", {}, "count"), AIMessage(content="5 records")])
    root = ScriptedModel(
        responses=[
            call("task", {"subagent_type": "db", "description": "Count actual records"}, "db-count"),
            AIMessage(content="There are 5 records"),
        ]
    )
    reviewer = ScriptedModel(responses=[passed()])
    registry = SubagentRegistry([spec])
    graph = create_deep_agent(
        model=budget_model(root, "main_model"),
        subagents=build_subagents(registry, root, review_model=root, models={"db": child}),
        middleware=[
            AnswerReviewMiddleware(reviewer),
            HookToolMiddleware(allowed_targets=registry.specs, managed_delegation=True),
        ],
    )
    result = await graph.ainvoke({"messages": [HumanMessage(content=context.user_query)]})
    assert result["messages"][-1].text == "There are 5 records"
    assert len(context.approved_internal) == 1
    payload = json.loads(reviewer.seen_messages[0][-1].text)
    assert payload["tools"][0]["name"] == "probe" and payload["tools"][0]["content"] == "5 records"


@pytest.mark.asyncio
@pytest.mark.parametrize("sync", [False, True])
async def test_real_graph_recovers_wrong_arguments_without_poisoning_successful_search(context, sync):
    actual = []

    @tool
    def internet_search(query: str):
        """Return actual web source evidence."""
        actual.append(query)
        return json.dumps({"results": [{"url": URL, "content": "checked fact"}]})

    settings = ReviewConfig(enabled=True, required_tools=("internet_search",), require_sources=True)
    spec = SubagentSpec(
        "search", DISPLAY_NAMES["search"], "Find facts", "Return facts", (internet_search,), review_config=settings
    )
    child = ScriptedModel(
        responses=[
            call("internet_search", {"query": {"wrong": "type"}}, "bad-args"),
            call("internet_search", {"query": "correct query"}, "fixed-args"),
            AIMessage(content="Checked fact " + URL),
        ]
    )
    root = ScriptedModel(
        responses=[
            call("task", {"description": "Verify the actual source", "subagent_type": "search"}, "verify"),
            AIMessage(content="Checked fact " + URL),
        ]
    )
    reviewer = ScriptedModel(responses=[passed()])
    registry = SubagentRegistry([spec])
    graph = create_deep_agent(
        model=budget_model(root, "main_model"),
        subagents=build_subagents(registry, root, review_model=root, models={"search": child}),
        middleware=[
            AnswerReviewMiddleware(reviewer),
            HookToolMiddleware(allowed_targets=registry.specs, managed_delegation=True),
        ],
    )
    inputs = {"messages": [HumanMessage(content=context.user_query)]}
    result = await asyncio.to_thread(graph.invoke, inputs) if sync else await graph.ainvoke(inputs)
    assert actual == ["correct query"] and result["messages"][-1].text == "Checked fact " + URL
    task = next(iter(context.search_tasks.values()))
    assert task.check_status == "passed" and len(task.evidence) == 2
    assert not task.evidence[0]["executed"] and task.evidence[0]["error_category"] == "invalid_argument"
