"""Regression coverage for search evidence size and model-vs-network timeouts."""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from test_framework_contract import ScriptedModel

from agent.reflection import ResultReviewer, capture_tool, rule_review, tool_evidence
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.delegation import DelegationRunnable
from agent.runtime.hooks import HookCall, HookRejected
from agent.runtime.models import ModelRequestTimeout, budget_model
from agent.runtime.policies import load_runtime_policy
from agent.subagent_registry import DISPLAY_NAMES, ReviewConfig, SubagentSpec


@pytest.fixture
def context(sandbox_dir):
    context = RunContext("search-test", "thread", sandbox_dir, policy=load_runtime_policy())
    token = set_run_context(context)
    try:
        yield context
    finally:
        reset_run_context(token)


@pytest.mark.parametrize("refinement", [False, True])
@pytest.mark.parametrize("serialized", [False, True])
def test_search_excerpts_keep_urls_and_full_original_evidence(context, monkeypatch, refinement, serialized):
    from tools import tavily_tool

    url = "https://weather.example.test/wuhan"
    raw = {
        "query": "normalized",
        "results": [{"url": url, "title": "Forecast", "content": "x" * 9000}],
        "response_time": 1.1,
    }
    requests = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def search(self, **kwargs):
            requests.append(kwargs)
            return raw

    monkeypatch.setenv("TAVILY_API_KEY", "fake")
    monkeypatch.setattr(tavily_tool, "TavilyClient", Client)
    from agent.runtime.search import SearchAllowance, search_allowance

    allowance = SearchAllowance(context)
    if refinement:
        allowance.begin_refinement(allow_search=True)
    search_token = search_allowance.set(allowance)
    try:
        message = tavily_tool.internet_search.invoke(
            {
                "type": "tool_call",
                "name": "internet_search",
                "id": "weather",
                "args": {
                    "query": "Wuhan tomorrow",
                    "include_domains": '["weather.example.test"]' if serialized else ["weather.example.test"],
                },
            }
        )
    finally:
        search_allowance.reset(search_token)
    displayed = json.loads(message.content)
    assert displayed["results"][0]["url"] == url
    assert len(displayed["results"][0]["content"]) == context.policy.search.result_chars
    assert displayed["results"][0]["excerpt_truncated"] is True and message.artifact == raw
    assert requests[0]["include_domains"] == ["weather.example.test"]
    assert requests[0]["max_results"] == 3 and requests[0]["timeout"] == 20
    assert requests[0]["search_depth"] == ("advanced" if refinement else "basic")
    evidence = []
    token = tool_evidence.set(evidence)
    try:
        call = HookCall(
            "search:internet_search",
            "weather",
            args={"query": "Wuhan tomorrow"},
            executed=True,
            status="completed",
            result=message,
        )
        capture_tool(call)
    finally:
        tool_evidence.reset(token)
    assert evidence[0]["content"] == raw
    assert evidence[0]["model_content"] == message.content
    settings = ReviewConfig(enabled=True, required_tools=("internet_search",), require_sources=True, model_check=True)
    assert rule_review("Forecast " + url, evidence, settings).verdict == "passed"
    model = ScriptedModel(
        responses=[
            AIMessage(content='{"verdict":"passed","issues":[],"missing_evidence":[],"revision_instruction":""}')
        ]
    )
    result = ResultReviewer(settings, model).review_sync("forecast", "Forecast " + url, evidence)
    assert result.verdict == "passed"
    review_text = model.seen_messages[0][-1].text
    assert "x" * 2000 not in review_text and url in review_text
    assert len(evidence[0]["content"]["results"][0]["content"]) == 9000


def test_total_excerpt_budget_is_bounded(context):
    from tools.tavily_tool import compact_search_response

    rows = [{"title": str(i), "url": f"https://example.test/{i}", "content": "z" * 8000} for i in range(20)]
    result = json.loads(compact_search_response({"results": rows}, context.policy.search))
    assert sum(len(row["content"]) for row in result["results"]) == context.policy.search.content_chars
    assert [row["url"] for row in result["results"]] == [row["url"] for row in rows]


@pytest.mark.asyncio
async def test_model_timeout_is_identified_and_does_not_restart_same_search(context):
    calls = []
    context.policy = context.policy.model_copy(
        update={"timeouts": context.policy.timeouts.model_copy(update={"request_seconds": 0.02})}
    )

    async def waits(messages, **kwargs):
        calls.append(1)
        await asyncio.Event().wait()

    model = budget_model(
        RunnableLambda(lambda messages, **kwargs: AIMessage(content="unused"), afunc=waits), "sub_model"
    )

    async def execute(state):
        return {"messages": [await model.ainvoke(state["messages"])]}

    spec = SubagentSpec(
        "search", DISPLAY_NAMES["search"], "Search", "Return facts", (), review_config=ReviewConfig(enabled=True)
    )
    agent = DelegationRunnable(spec, RunnableLambda(execute))
    result = await agent.ainvoke({"messages": [HumanMessage(content="forecast")]})
    assert "子助手模型请求超时" in result["messages"][-1].text
    with pytest.raises(HookRejected, match="本轮不重复调用"):
        await agent.ainvoke({"messages": [HumanMessage(content="try again")]})
    assert calls == [1] and context.call_counts["sub_model"] == 1


def test_sync_model_timeout_is_identified(context):
    def times_out(messages, **kwargs):
        raise TimeoutError()

    model = budget_model(RunnableLambda(times_out), "main_model")
    with pytest.raises(ModelRequestTimeout, match="任务协调模型请求超时"):
        model.invoke("hello")


@pytest.mark.asyncio
async def test_review_timeout_is_bounded_and_named(context):
    context.policy = context.policy.model_copy(
        update={"timeouts": context.policy.timeouts.model_copy(update={"request_seconds": 0.02})}
    )

    async def waits(messages, **kwargs):
        await asyncio.Event().wait()

    model = RunnableLambda(lambda messages, **kwargs: None, afunc=waits)
    result = await ResultReviewer(ReviewConfig(enabled=True, model_check=True), model).review("task", "answer", [])
    assert result.verdict == "check_incomplete" and "结果检查模型请求超时" in result.issues[0]


def test_search_allowance_is_atomic_and_retries_keep_the_same_slot(context):
    from concurrent.futures import ThreadPoolExecutor

    from agent.runtime.policies import PolicyError
    from agent.runtime.search import SearchAllowance

    allowance = SearchAllowance(context)

    def attempt(index):
        try:
            allowance.reserve(str(index))
            return True
        except PolicyError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        allowed = list(pool.map(attempt, range(12)))
    assert sum(allowed) == 2 and len(allowance.calls) == 2
    allowance.reserve(next(iter(allowance.calls)), retry=True)
    assert len(allowance.calls) == 2
    context.skill_ids = ("research",)
    assert allowance.limit() == 6


def test_search_limit_is_enforced_before_tool_execution_and_forces_final_answer(context):
    from types import SimpleNamespace

    from langchain.agents.middleware import ModelRequest
    from langchain_core.messages import SystemMessage, ToolMessage

    from agent.runtime.hooks import HookToolMiddleware
    from agent.runtime.search import SearchAllowance, SearchExecutionMiddleware, search_allowance

    allowance = SearchAllowance(context)
    token = search_allowance.set(allowance)
    actual = []
    try:
        request = ModelRequest(
            model=ScriptedModel(responses=[]),
            messages=[],
            tools=[{"name": "internet_search"}],
            system_message=SystemMessage(content="Search"),
        )
        first = SearchExecutionMiddleware().final_request(request)
        assert first.tool_choice == {"type": "function", "function": {"name": "internet_search"}}
        for index in range(3):
            args = SimpleNamespace(
                tool_call={"name": "internet_search", "id": str(index), "args": {"query": "forecast"}}
            )

            def execute(req):
                actual.append(req.tool_call["id"])
                return ToolMessage(content="facts", tool_call_id=req.tool_call["id"])

            result = HookToolMiddleware("search").wrap_tool_call(args, execute)
        assert len(actual) == 2 and result.status == "error"
        final = SearchExecutionMiddleware().final_request(request)
        assert final.tools == [] and final.tool_choice == "none"
        assert final.model_settings["tool_choice"] == "none"
        assert "实际来源 URL" in final.system_message.text
        assert request.tools and context.call_counts["tool"] == 2
    finally:
        search_allowance.reset(token)


def test_model_extra_body_is_optional_and_validated(monkeypatch):
    import runpy
    from pathlib import Path
    from types import SimpleNamespace

    import dotenv
    import langchain.chat_models

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(dotenv, "find_dotenv", lambda *args, **kwargs: "")
    monkeypatch.delenv("LLM_EXTRA_BODY", raising=False)
    monkeypatch.setattr(langchain.chat_models, "init_chat_model", lambda **kwargs: SimpleNamespace(**kwargs))
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "agent/llm.py"))
    options = module["model_options"]
    assert options(None) == {} and options("") == {}
    assert options('{"thinking":{"type":"disabled"}}') == {"extra_body": {"thinking": {"type": "disabled"}}}
    for value in ("[]", "null", "invalid"):
        with pytest.raises(ValueError, match="LLM_EXTRA_BODY"):
            options(value)


def test_intentional_search_limit_preserves_successful_source_evidence(context):
    from types import SimpleNamespace

    from langchain_core.messages import ToolMessage

    from agent.runtime.hooks import HookToolMiddleware
    from agent.runtime.search import SearchAllowance, search_allowance

    evidence = []
    allowance_token = search_allowance.set(SearchAllowance(context))
    evidence_token = tool_evidence.set(evidence)
    url = "https://weather.example.test/forecast"
    try:
        for index in range(3):
            request = SimpleNamespace(
                tool_call={"name": "internet_search", "id": str(index), "args": {"query": "forecast"}}
            )

            def execute(req):
                return ToolMessage(
                    content=json.dumps({"results": [{"url": url, "content": "forecast"}]}),
                    tool_call_id=req.tool_call["id"],
                )

            HookToolMiddleware("search").wrap_tool_call(request, execute)
    finally:
        tool_evidence.reset(evidence_token)
        search_allowance.reset(allowance_token)
    assert len(evidence) == 3
    assert evidence[-1]["error_category"] == "search_limit" and not evidence[-1]["executed"]
    assert (
        rule_review(
            "Forecast " + url,
            evidence,
            ReviewConfig(enabled=True, required_tools=("internet_search",), require_sources=True),
        ).verdict
        == "passed"
    )


@pytest.mark.parametrize("domains", [["https://example.test/path"], ["example.test/path"], [], ["localhost"]])
def test_domain_filter_rejects_invalid_shape_before_network(context, domains):
    from tools.tavily_tool import internet_search

    with pytest.raises(ValueError, match="domain names"):
        internet_search.invoke({"query": "official facts", "include_domains": domains})


@pytest.mark.asyncio
@pytest.mark.parametrize("sync", [False, True])
async def test_bad_domain_argument_is_recoverable_in_same_task_without_searching_or_borrowing_evidence(
    context, monkeypatch, sync
):
    from deepagents import create_deep_agent
    from test_framework_contract import call

    from agent.runtime.hooks import HookToolMiddleware
    from agent.subagent_factory import build_subagents
    from agent.subagent_registry import SubagentRegistry
    from tools import tavily_tool

    actual = []
    url = "https://docs.example.test/3.11/facts"

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def search(self, **kwargs):
            actual.append(kwargs)
            return {"results": [{"url": url, "content": "official fact"}]}

    monkeypatch.setenv("TAVILY_API_KEY", "fake")
    monkeypatch.setattr(tavily_tool, "TavilyClient", Client)
    child = ScriptedModel(
        responses=[
            call(
                "internet_search",
                {"query": "official fact", "include_domains": ["https://docs.example.test/3.11"]},
                "invalid-domain",
            ),
            call(
                "internet_search", {"query": "official fact", "include_domains": ["docs.example.test"]}, "valid-domain"
            ),
            AIMessage(content="official fact " + url),
        ]
    )
    parent = ScriptedModel(
        responses=[
            call("task", {"description": "official fact", "subagent_type": "search"}),
            AIMessage(content="done"),
        ]
    )
    settings = ReviewConfig(enabled=True, required_tools=("internet_search",), require_sources=True)
    spec = SubagentSpec(
        "search", DISPLAY_NAMES["search"], "Find", "Find", (tavily_tool.internet_search,), review_config=settings
    )
    registry = SubagentRegistry([spec])
    graph = create_deep_agent(
        model=budget_model(parent, "main_model"),
        subagents=build_subagents(registry, parent, models={"search": child}),
        middleware=[HookToolMiddleware(allowed_targets=registry.specs, managed_delegation=True)],
    )
    initial = {"messages": [HumanMessage(content="official fact")]}
    await asyncio.to_thread(graph.invoke, initial) if sync else await graph.ainvoke(initial)
    assert len(actual) == 1 and actual[0]["include_domains"] == ["docs.example.test"]
    assert len(context.search_tasks) == 1 and len(context.search_calls) == 1
    task = next(iter(context.search_tasks.values()))
    assert task.check_status == "passed"
    invalid = next(item for item in task.evidence if item["call_id"] == "invalid-domain")
    valid = next(item for item in task.evidence if item["call_id"] == "valid-domain")
    assert not invalid["executed"] and invalid["error_category"] == "invalid_argument"
    assert valid["executed"] and valid["status"] == "completed"
