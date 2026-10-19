"""Actual page evidence, bounded access and parallel child isolation."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import SystemMessage
from test_framework_contract import ScriptedModel

from agent.reflection import capture_tool, source_urls, tool_evidence
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.evidence import model_evidence
from agent.runtime.hooks import HookCall, HookToolMiddleware
from agent.runtime.policies import PolicyError, SearchLimitReached, load_runtime_policy
from agent.runtime.search import SearchAllowance, SearchExecutionMiddleware, search_allowance
from tools import tavily_tool


def discovered(host="official.example.test"):
    return dict(
        name="internet_search",
        call_id="discovery",
        executed=True,
        status="completed",
        content={"results": [{"url": f"https://{host}/new-version", "content": "discovery only"}]},
    )


@pytest.fixture
def context(sandbox_dir):
    context = RunContext("page-test", "thread", sandbox_dir, policy=load_runtime_policy())
    token = set_run_context(context)
    try:
        yield context
    finally:
        reset_run_context(token)


def test_reader_preserves_real_artifact_and_exact_version_source(context, monkeypatch):
    body = "Actual older-version section. Remaining tasks are cancelled. " * 55
    payload = {
        "results": [{"url": "https://official.example.test/old-version", "raw_content": body}],
        "failed_results": [],
    }
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract(self, **kwargs):
            calls.append(kwargs)
            return copy.deepcopy(payload)

    monkeypatch.setenv("TAVILY_API_KEY", "fake")
    monkeypatch.setattr(tavily_tool, "TavilyClient", Client)
    records = [discovered()]
    collector = tool_evidence.set(records)
    try:
        result = tavily_tool.read_web_page.invoke(
            {
                "type": "tool_call",
                "id": "read",
                "name": "read_web_page",
                "args": {"url": "https://official.example.test/old-version", "query": "remaining tasks cancellation"},
            }
        )
        capture_tool(
            HookCall(
                "search:read_web_page",
                "read",
                args={"query": "remaining tasks cancellation"},
                result=result,
                executed=True,
                status="completed",
            )
        )
        assert records[-1]["content"]["results"] == payload["results"]
        assert "old-version" in source_urls(records)[-1]
        data = json.loads(result.content)
        assert data["results"][0]["content"] == body
        compact = model_evidence(records, 6000, 1000)
        assert compact[-1]["content"]["results"][0]["content"] == body
        assert records[-1]["request_parameters"]["query"] == "remaining tasks cancellation"
        assert calls[0]["urls"] == ["https://official.example.test/old-version"]
        assert calls[0]["query"] == "remaining tasks cancellation"
        assert calls[0]["chunks_per_source"] == 5
        assert calls[0]["timeout"] <= context.policy.search.timeout_seconds
    finally:
        tool_evidence.reset(collector)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1",
        "http://localhost",
        "https://private.local/page",
        "https://u:p@official.example.test/",
        "https://foreign.example.test/page",
        "https://official.example.test:8000/page",
    ],
)
def test_unknown_origins_and_nonpublic_urls_rejected_before_execution(context, url):
    records = [discovered()]
    collector = tool_evidence.set(records)
    actual = []
    try:
        request = SimpleNamespace(
            tool_call={"name": "read_web_page", "id": "bad", "args": {"url": url, "query": "fact"}}
        )
        result = HookToolMiddleware("search").wrap_tool_call(request, lambda request: actual.append(1))
        assert result.status == "error" and actual == []
        assert records[-1]["executed"] is False
    finally:
        tool_evidence.reset(collector)


@pytest.mark.asyncio
async def test_parallel_children_cannot_borrow_each_others_origin(context):
    async def child(own, other):
        token = tool_evidence.set([discovered(own)])
        try:
            await asyncio.sleep(0)
            tavily_tool.validate_page_request(f"https://{own}/specific-version", "fact")
            with pytest.raises(PolicyError, match="当前子任务"):
                tavily_tool.validate_page_request(f"https://{other}/specific-version", "fact")
        finally:
            tool_evidence.reset(token)

    await asyncio.gather(child("a.example.test", "b.example.test"), child("b.example.test", "a.example.test"))


def test_search_exhaustion_allows_only_bounded_page_reads_and_cannot_reset(context):
    own, other = SearchAllowance(context, "own"), SearchAllowance(context, "other")
    own.reserve("search-one")
    own.reserve("search-two")
    token = search_allowance.set(own)
    try:
        request = ModelRequest(
            model=ScriptedModel(responses=[]),
            messages=[],
            system_message=SystemMessage(content="role"),
            tools=[tavily_tool.internet_search, tavily_tool.read_web_page],
        )
        middleware = SearchExecutionMiddleware("role")
        prepared = middleware.final_request(request)
        assert prepared.tools == [tavily_tool.read_web_page]
        assert "tool_choice" not in prepared.model_settings
        # Match the actual framework call: request.tool_choice is already explicit.
        prepared.model.bind_tools(prepared.tools, tool_choice=prepared.tool_choice, **prepared.model_settings)
        for index in range(context.policy.search.page_reads_per_task):
            own.reserve_page(str(index))
        assert middleware.final_request(request).tools == []
        own.begin_refinement(allow_search=True)
        assert not own.can_read_page()
        with pytest.raises(SearchLimitReached):
            own.reserve_page("new")
        own.reserve_page("0", retry=True)
        assert len(own.task.page_calls) == 3
        assert other.can_read_page() and other.task.page_calls == set()
        assert len(context.search_calls) == 2
    finally:
        search_allowance.reset(token)


def test_raw_body_reaches_worker_and_checker_instead_of_discovery_snippet(context):
    payload = {
        "results": [
            {
                "url": "https://official.example.test/page",
                "content": "discovery only",
                "raw_content": "Actual body: distinct keys retain insertion order.",
            }
        ]
    }
    output = json.loads(tavily_tool.compact_search_response(payload, context.policy.search, "distinct keys"))
    assert "Actual body" in output["results"][0]["content"]
    records = [dict(discovered(), content=payload)]
    assert "Actual body" in model_evidence(records, 6000)[0]["content"]["results"][0]["content"]


def test_failed_page_does_not_create_a_citable_source():
    records = [
        dict(
            discovered(),
            name="read_web_page",
            content={
                "results": [],
                "failed_results": [{"url": "https://official.example.test/missing", "error": "not found"}],
            },
        )
    ]
    assert source_urls(records) == []


def test_schema_normalization_and_policy_use_same_actual_search_arguments(context):
    records = []
    token = tool_evidence.set(records)
    actual = []
    try:
        request = SimpleNamespace(
            tool=tavily_tool.internet_search,
            tool_call={
                "name": "internet_search",
                "id": "typed",
                "args": {"query": "official fact", "max_results": "3", "include_domains": '["official.example.test"]'},
            },
        )

        def run(request):
            from langchain_core.messages import ToolMessage

            actual.append(request.tool_call["args"])
            return ToolMessage(content="discovered", tool_call_id="typed")

        result = HookToolMiddleware("search").wrap_tool_call(request, run)
        assert result.status == "success"
        assert actual == [{"query": "official fact", "max_results": 3, "include_domains": ["official.example.test"]}]
        assert records[0]["request_parameters"] == actual[0]
    finally:
        tool_evidence.reset(token)
