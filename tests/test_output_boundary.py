"""Generic boundaries reproduced independently of question wording and product names."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from test_framework_contract import ScriptedModel

from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.models import budget_model
from agent.runtime.policies import load_runtime_policy
from agent.subagent_registry import ReviewConfig


@pytest.fixture
def context(sandbox_dir):
    value = RunContext("boundaries", "thread", sandbox_dir, policy=load_runtime_policy())
    token = set_run_context(value)
    try:
        yield value
    finally:
        reset_run_context(token)


def test_unstructured_draft_does_not_become_approved_evidence(context):
    from agent.reflection import ResultReviewer

    result = ResultReviewer(ReviewConfig(enabled=True, model_check=True), ScriptedModel(responses=[])).review_sync(
        "Find a fact", "unstructured draft https://example.test/source", []
    )
    assert result.verdict == "check_incomplete" and result.accepted_report is None


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


@pytest.mark.parametrize("category", ["invalid_argument", "permission", "execution"])
def test_failed_attempts_never_become_sources_and_valid_siblings_remain_usable(context, category):
    from agent.reflection import rule_review
    from agent.runtime.facts import local_sources

    failed = dict(
        name="internet_search",
        call_id="bad",
        executed=True,
        status="failed",
        error_category=category,
        content={"results": [{"url": "https://example.test/invalid", "content": "bad"}]},
    )
    success = dict(
        name="internet_search",
        call_id="good",
        executed=True,
        status="completed",
        content={"results": [{"url": "https://example.test/real", "content": "actual fact"}]},
    )
    rows = [failed, success]
    sources = local_sources(rows)
    assert len(sources) == 1 and next(iter(sources.values()))[0]["url"] == "https://example.test/real"
    assert (
        rule_review(
            "actual fact https://example.test/real", rows, ReviewConfig(enabled=True, require_sources=True)
        ).verdict
        == "passed"
    )
