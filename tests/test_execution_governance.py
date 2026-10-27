"""Retained execution protections after removal of fact/plan protocols."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage

from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.governance import register_turn
from agent.runtime.hooks import HookCall
from agent.runtime.policies import load_runtime_policy
from agent.runtime.recovery import retry_after, retry_delay


@pytest.fixture
def context(sandbox_dir):
    policy = load_runtime_policy()
    policy = policy.model_copy(
        update={
            "retry": policy.retry.model_copy(
                update={"delay_seconds": 0.02, "max_retries": 3, "tools": ["internet_search"]}
            )
        }
    )
    context = RunContext("run", "thread", sandbox_dir, policy=policy)
    token = set_run_context(context)
    try:
        yield context
    finally:
        reset_run_context(token)


def request(name="internet_search", id="call"):
    return SimpleNamespace(
        tool=SimpleNamespace(metadata={}), tool_call={"name": name, "id": id, "args": {"query": "q"}}
    )


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


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows long paths")
def test_atomic_evidence_writer_supports_long_windows_paths(context):
    from agent.runtime.checkpoints import write_json

    path = context.work_dir / ("nested-" + "a" * 90) / ("evidence-" + "b" * 90 + ".json")
    write_json(path, {"中文": "证据"})
    absolute = "\\\\?\\" + str(path.resolve())
    from pathlib import Path

    assert json.loads(Path(absolute).read_text("utf-8")) == {"中文": "证据"}


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
