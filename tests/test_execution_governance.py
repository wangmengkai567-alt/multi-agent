"""Acceptance tests for layered deadlines, retry semantics, logical IDs and source repair."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage

from agent.reflection import normalize_source_citations, rule_review, source_id
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.governance import near_budget, register_turn
from agent.runtime.hooks import HookCall, HookRejected, HookToolMiddleware
from agent.runtime.policies import load_runtime_policy
from agent.runtime.recovery import ToolCallTimeout, retry_after, retry_delay
from agent.runtime.search import SearchAllowance, needs_search_refinement, search_must_finish
from agent.runtime.task_plan import plan_research, resolve_task
from agent.subagent_registry import ReviewConfig


@pytest.fixture
def context(sandbox_dir):
    policy = load_runtime_policy()
    policy = policy.model_copy(
        update={"retry": policy.retry.model_copy(update={"delay_seconds": 0.02, "max_retries": 3})}
    )
    context = RunContext("run", "thread", sandbox_dir, policy=policy)
    token = set_run_context(context)
    try:
        yield context
    finally:
        reset_run_context(token)


def request(name="internet_search", id="call"):
    return SimpleNamespace(tool_call={"name": name, "id": id, "args": {"query": "q"}})


def http_error(status, headers=None):
    response = httpx.Response(status, headers=headers, request=httpx.Request("GET", "https://example.test"))
    return httpx.HTTPStatusError("controlled", request=response.request, response=response)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_permanent_and_conflict_errors_never_blindly_retry(context, status):
    req = request()
    call = HookCall("main:internet_search", "call", args={"query": "q"})
    assert retry_delay(req, call, http_error(status)) is None


@pytest.mark.parametrize("status", [408, 500, 501, 502, 503, 504])
def test_transients_have_exponential_jitter_and_attempt_budget(context, status, monkeypatch):
    req = request()
    call = HookCall("main:internet_search", "call", args={"query": "q"})
    monkeypatch.setattr("agent.runtime.recovery.random.uniform", lambda a, b: b)
    assert retry_delay(req, call, http_error(status)) == pytest.approx(0.02)
    call.attempt = 1
    assert retry_delay(req, call, http_error(status)) == pytest.approx(0.04)
    call.attempt = 3
    assert retry_delay(req, call, http_error(status)) is None


def test_rate_limit_uses_header_and_will_not_sleep_past_deadline(context):
    req = request()
    call = HookCall("main:internet_search", "call", args={"query": "q"})
    error = http_error(429, {"Retry-After": "17"})
    assert retry_delay(req, call, error) >= 17
    moment = datetime(2026, 10, 19, tzinfo=UTC)
    assert retry_after(http_error(429, {"Retry-After": "Mon, 19 Oct 2026 00:00:20 GMT"}), moment) == 20
    context.deadline = datetime.now(UTC) + timedelta(seconds=2)
    assert retry_delay(req, call, error) is None
    assert "时长预算" in call.workflow_data["retry_blocked"]


def test_unprotected_writes_never_retry_even_when_whitelisted(context):
    context.policy = context.policy.model_copy(
        update={"retry": context.policy.retry.model_copy(update={"tools": ["write_file"]})}
    )
    req = request("write_file")
    call = HookCall("main:write_file", "call", args={"idempotency_key": "key"})
    assert retry_delay(req, call, TimeoutError()) is None
    req.tool = SimpleNamespace(metadata={"retry_safety": "idempotency_key", "idempotency_argument": "idempotency_key"})
    assert retry_delay(req, call, TimeoutError()) is not None
    call.args = {}
    assert retry_delay(req, call, TimeoutError()) is None


@pytest.mark.asyncio
async def test_tool_timeout_is_independent_of_run_deadline(context):
    context.deadline = datetime.now(UTC) + timedelta(seconds=10)
    context.policy = context.policy.model_copy(
        update={
            "timeouts": context.policy.timeouts.model_copy(update={"tool_seconds": 0.03}),
            "retry": context.policy.retry.model_copy(update={"max_retries": 0}),
        }
    )

    async def slow(req):
        await asyncio.sleep(1)
        return ToolMessage(content="late", tool_call_id="call")

    with pytest.raises(ToolCallTimeout):
        await HookToolMiddleware().awrap_tool_call(request(), slow)
    assert context.stop_status is None and context.remaining() > 9


@pytest.mark.asyncio
async def test_same_model_turn_has_a_separate_tool_cap(context):
    context.policy = context.policy.model_copy(
        update={"budgets": context.policy.budgets.model_copy(update={"tools_per_turn": 1})}
    )
    register_turn(
        AIMessage(
            content="", tool_calls=[dict(name="internet_search", args={"query": "q"}, id=id) for id in ("one", "two")]
        )
    )
    executions = []

    async def execute(req):
        executions.append(req.tool_call["id"])
        return ToolMessage(content="ok", tool_call_id=req.tool_call["id"])

    one = await HookToolMiddleware().awrap_tool_call(request(id="one"), execute)
    two = await HookToolMiddleware().awrap_tool_call(request(id="two"), execute)
    assert one.status == "success" and two.status == "error" and executions == ["one"]
    register_turn(AIMessage(content="", tool_calls=[dict(name="internet_search", args={"query": "q"}, id="three")]))
    await HookToolMiddleware().awrap_tool_call(request(id="three"), execute)
    assert executions == ["one", "three"]


def test_registered_scope_cannot_be_rewritten_to_refresh_budget(context):
    context.enforce_plan = True
    scopes = json.loads(
        plan_research.invoke(
            {"tasks": [{"label": "晋城", "description": "调查晋城"}, {"label": "新乡", "description": "调查新乡"}]}
        )
    )
    assert len(scopes) == 2
    jincheng = scopes[0]
    key, description, label = resolve_task(
        context, json.dumps({"task_id": jincheng["task_id"], "description": "重新全面调查晋城"})
    )
    assert description == "调查晋城" and label == "晋城"
    allowance = SearchAllowance(context, key)
    allowance.claim()
    allowance.finish(executed=True, check_status="insufficient", reason="修订已结束")
    same = json.loads(plan_research.invoke({"tasks": [{"label": "晋城补充", "description": "换说法"}]}))
    assert same == scopes
    with pytest.raises(HookRejected):
        SearchAllowance(context, key).claim()
    with pytest.raises(HookRejected):
        resolve_task(context, "换说法重查晋城")
    other = SearchAllowance(context, scopes[1]["task_id"])
    other.claim()
    other.finish(executed=True, check_status="passed")
    assert search_must_finish(context)


def test_near_threshold_detects_time_and_calls(context):
    context.deadline = datetime.now(UTC) + timedelta(seconds=10)
    assert near_budget(context)
    context.deadline = None
    context.call_counts["tool"] = context.policy.budgets.tool
    assert near_budget(context)


def test_source_ids_render_exact_addresses_and_bad_links_get_precise_feedback(context):
    url = "https://www.jcgov.gov.cn/mljc/jcgk"
    evidence = [
        dict(
            name="internet_search",
            call_id="s",
            executed=True,
            status="completed",
            content={"results": [{"url": url, "content": "facts"}]},
        )
    ]
    text = normalize_source_citations("事实 [source:" + source_id(url) + "]", evidence)
    assert url in text
    settings = ReviewConfig(require_sources=True)
    assert rule_review(text, evidence, settings).verdict == "passed"
    bad = text + "\n错误来源 https://jcgov.gov.cn/mljc/jcgk"
    review = rule_review(bad, evidence, settings)
    assert review.verdict == "insufficient"
    assert "https://jcgov.gov.cn/mljc/jcgk" in review.revision_instruction
    assert "claim_lines" in review.revision_instruction
    assert not needs_search_refinement(review, evidence)


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows long paths")
def test_atomic_evidence_writer_supports_long_windows_paths(context):
    from agent.runtime.checkpoints import write_json

    path = context.work_dir / ("nested-" + "a" * 90) / ("evidence-" + "b" * 90 + ".json")
    write_json(path, {"中文": "证据"})
    absolute = "\\\\?\\" + str(path.resolve())
    from pathlib import Path

    assert json.loads(Path(absolute).read_text("utf-8")) == {"中文": "证据"}


@pytest.mark.asyncio
async def test_unknown_tool_is_never_executed_and_does_not_erase_real_evidence(context):
    from agent.reflection import tool_evidence

    context.enforce_plan = True
    evidence = []
    token = tool_evidence.set(evidence)
    actual = []
    req = request("update_todos_placeholder")
    req.tool = None
    try:
        result = await HookToolMiddleware().awrap_tool_call(req, lambda r: actual.append(1))
        assert result.status == "error" and actual == [] and not evidence[0]["executed"]
        assert evidence[0]["error_category"] == "invalid_argument"
    finally:
        tool_evidence.reset(token)
    url = "https://example.test/city"
    evidence.append(
        dict(
            name="internet_search",
            call_id="real",
            executed=True,
            status="completed",
            content={"results": [{"url": url, "content": "facts"}]},
        )
    )
    settings = ReviewConfig(required_tools=("internet_search",), require_sources=True)
    assert rule_review("facts " + url, evidence, settings).verdict == "passed"
    assert rule_review("no source", evidence[:1], settings).verdict != "passed"


@pytest.mark.asyncio
async def test_accepted_plan_with_only_a_promise_cannot_pass_final_source_gate(context):
    from agent.runtime.answer_review import AnswerReviewMiddleware

    context.accepted_plan = True
    review = AnswerReviewMiddleware(None)._source_gap(AIMessage(content="让我调用搜索工具"), [])
    assert review.verdict == "insufficient"


def test_unknown_source_id_is_not_satisfied_by_another_valid_url(context):
    url = "https://example.test/source"
    evidence = [
        dict(
            name="internet_search",
            call_id="real",
            executed=True,
            status="completed",
            content={"results": [{"url": url}]},
        )
    ]
    review = rule_review("有出处 " + url + " 另一个断言 [source:unknown]", evidence, ReviewConfig(require_sources=True))
    assert review.verdict == "insufficient" and "unknown" in review.revision_instruction
    assert not needs_search_refinement(review, evidence)


def test_tavily_adapter_preserves_real_rate_limit_headers(context):
    from tavily import TavilyClient

    from agent.runtime.recovery import http_status
    from tools.tavily_tool import preserve_http_errors

    with TavilyClient(api_key="offline-test-only") as client:
        preserve_http_errors(client)
        response = httpx.Response(
            429, headers={"Retry-After": "13"}, request=httpx.Request("POST", "https://api.tavily.com/search")
        )
        with pytest.raises(httpx.HTTPStatusError) as caught:
            client.session.hooks["response"][-1](response)
        assert http_status(caught.value) == 429 and retry_after(caught.value) == 13


def test_reused_provider_tool_ids_are_unique_across_agents_and_turns(context):
    from agent.runtime.events import make_event
    from agent.runtime.hooks import active_call

    context.enforce_plan = True
    message = AIMessage(
        content="", tool_calls=[dict(name="internet_search", args={"query": "q"}, id="provider-reused")]
    )
    ids = []
    for parent in ("agent-a", "agent-b", "agent-a"):
        token = active_call.set(HookCall("search", parent))
        try:
            normalized = register_turn(message)
            id = normalized.tool_calls[0]["id"]
            ids.append(id)
            event = make_event("thread", "run", "before_tool", "tool", call_id=id, parent_call_id=parent)
            assert event["data"]["provider_call_id"] == "provider-reused"
            assert (parent, id) in context.tool_turns
        finally:
            active_call.reset(token)
    assert len(set(ids)) == 3
    assert message.tool_calls[0]["id"] == "provider-reused"


def test_final_citation_and_fact_repairs_each_have_one_slot(context):
    from langchain.agents.middleware import ModelRequest
    from langchain_core.messages import HumanMessage, SystemMessage

    from agent.reflection import ReviewResult
    from agent.runtime.answer_review import AnswerReviewMiddleware

    task = SearchAllowance(context, "accepted")
    task.task.check_status = "passed"
    task.task.result = "facts https://example.test/source"
    task.task.evidence = [
        dict(
            name="internet_search",
            call_id="s",
            executed=True,
            status="completed",
            content={"results": [{"url": "https://example.test/source", "content": "facts"}]},
        )
    ]
    middleware = AnswerReviewMiddleware(None)
    request = ModelRequest(
        model=None,
        messages=[HumanMessage(content="question")],
        system_message=SystemMessage(content="system"),
        tools=[],
    )
    message = AIMessage(content="draft")
    citation = ReviewResult(
        verdict="insufficient", issues=["部分引用没有本轮实际检索依据"], revision_instruction="fix link"
    )
    fact = ReviewResult(
        verdict="insufficient", issues=["结论与实际依据冲突"], revision_instruction="delete unsupported number"
    )
    assert middleware._revision(request, message, citation) is not None
    assert middleware._revision(request, message, citation) is None
    assert middleware._revision(request, message, fact) is not None
    assert middleware._revision(request, message, fact) is None
    assert context.call_counts["retry"] == 2
    middleware._record(fact)
    assert context.delivery_status == "partial"


def test_recovery_retains_tool_turn_and_provider_identity(context):
    from agent.runtime.checkpoints import restore_job_checkpoint, save_job_checkpoint

    context.enforce_plan = True
    context.job_checkpoint_path = context.work_dir / "checkpoint.json"
    message = register_turn(
        AIMessage(content="", tool_calls=[dict(name="internet_search", args={"query": "q"}, id="provider")])
    )
    id = message.tool_calls[0]["id"]
    turn = context.tool_turns[(context.run_id, id)]
    context.turn_counts[turn] = 2
    context.call_counts["tool"] = 2
    save_job_checkpoint(context)
    restored = RunContext(
        "run", "thread", context.work_dir, policy=context.policy, job_checkpoint_path=context.job_checkpoint_path
    )
    restore_job_checkpoint(restored)
    assert restored.tool_turns == context.tool_turns and restored.turn_counts[turn] == 2
    assert restored.original_call_ids[id] == "provider" and restored.call_counts["tool"] == 2


@pytest.mark.parametrize("format", ["[source: {id}]", "[source:  {id}  ]", "[SOURCE: {upper}]"])
def test_registered_source_whitespace_does_not_consume_a_revision(context, format):
    url = "https://example.test/actual"
    evidence = [
        dict(
            name="internet_search", call_id="s", executed=True, status="completed", content={"results": [{"url": url}]}
        )
    ]
    text = normalize_source_citations(format.format(id=source_id(url), upper=source_id(url).upper()), evidence)
    assert text == "[来源](" + url + ")"
    assert rule_review(text, evidence, ReviewConfig(require_sources=True)).verdict == "passed"
