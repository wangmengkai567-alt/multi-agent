"""Generic boundaries reproduced independently of question wording and product names."""

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from test_framework_contract import ScriptedModel
from test_search_recovery import spec

from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.delegation import DelegationRunnable
from agent.runtime.hooks import HookToolMiddleware
from agent.runtime.models import budget_model
from agent.runtime.policies import load_runtime_policy
from agent.runtime.search import search_allowance
from agent.subagent_registry import ReviewConfig


@pytest.fixture
def context(sandbox_dir):
    value = RunContext("boundaries", "thread", sandbox_dir, policy=load_runtime_policy())
    token = set_run_context(value)
    try:
        yield value
    finally:
        reset_run_context(token)


def test_empty_successful_search_can_use_reserved_content_refinement(context):
    actual = []

    def execute(state):
        number = len(actual)
        scope = search_allowance.get()
        assert scope is not None
        request = SimpleNamespace(
            tool_call={"name": "internet_search", "id": f"query-{number}", "args": {"query": "topic"}}
        )

        def provider(request):
            actual.append(request.tool_call["id"])
            results = [] if len(actual) == 1 else [{"url": "https://example.test/source", "content": "verified fact"}]
            return ToolMessage(content=json.dumps({"results": results}), tool_call_id=request.tool_call["id"])

        HookToolMiddleware("search").wrap_tool_call(request, provider)
        return {
            "messages": [
                AIMessage(
                    content="No matching source yet"
                    if len(actual) == 1
                    else "verified fact https://example.test/source"
                )
            ]
        }

    settings = ReviewConfig(enabled=True, required_tools=("internet_search",), require_sources=True)
    result = DelegationRunnable(spec(settings), RunnableLambda(execute)).invoke(
        {"messages": [HumanMessage(content="Find a fact; report gaps truthfully")]}
    )
    assert len(actual) == 2
    assert result["messages"][-1].text == "verified fact https://example.test/source"


def test_normal_main_output_does_not_expose_provider_reasoning(context):
    raw = ScriptedModel(responses=[AIMessage(content="<think>private scratchpad</think>Visible answer")])
    result = budget_model(raw, "main_model").invoke([HumanMessage(content="Any ordinary question")])
    assert result.text == "Visible answer"


def test_one_genuine_reference_cannot_hide_an_invented_reference(context):
    from agent.reflection import rule_review

    records = [
        dict(
            name="internet_search",
            executed=True,
            status="completed",
            call_id="one",
            content={"results": [dict(url="https://example.test/real", content="verified fact")]},
        )
    ]
    result = rule_review(
        "fact https://example.test/real and https://example.test/invented",
        records,
        ReviewConfig(enabled=True, require_sources=True),
    )
    assert result.verdict == "insufficient"
    assert (
        rule_review(
            "fact https://example.test/real#section", records, ReviewConfig(enabled=True, require_sources=True)
        ).verdict
        == "passed"
    )


def test_url_boundaries_handle_chinese_punctuation_and_serialized_newlines(context):
    from agent.runtime.output import cited_urls

    url = "https://docs.example.test/manual#section"
    assert cited_urls(url + "（来源说明）") == {url}
    assert cited_urls(url + r"\n\n下一段") == {url}
    assert cited_urls("`" + url + "` and example code") == set()


@pytest.mark.parametrize(
    "category,executed,success_after,expected",
    [
        ("invalid_argument", False, True, "passed"),
        ("invalid_argument", False, False, "tool_failed"),
        ("permission", False, True, "dependency_unavailable"),
        ("execution", True, True, "tool_failed"),
        ("invalid_argument", True, True, "tool_failed"),
    ],
)
def test_only_recovered_nonexecuted_argument_errors_stop_blocking(context, category, executed, success_after, expected):
    from agent.reflection import rule_review

    url = "https://example.test/recovered"
    failure = dict(
        name="internet_search",
        call_id="bad",
        executed=executed,
        status="rejected",
        error_category=category,
        content=None,
    )
    success = dict(
        name="internet_search",
        call_id="fixed",
        executed=True,
        status="completed",
        content={"results": [{"url": url, "content": "actual fact"}]},
    )
    evidence = [failure, success] if success_after else [success, failure]
    result = rule_review("actual fact " + url, evidence, ReviewConfig(enabled=True, require_sources=True))
    assert result.verdict == expected
    assert len(evidence) == 2 and evidence[0 if success_after else 1] is failure
