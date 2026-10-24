"""Only reviewer-format failures get bounded rechecking of the same evidence."""

import json

import pytest
from langchain_core.messages import AIMessage
from protocol_fixtures import decision, evidence, fact, report
from test_framework_contract import ScriptedModel

from agent.reflection import ResultReviewer
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.policies import load_runtime_policy
from agent.subagent_registry import DISPLAY_NAMES, ReviewConfig, SubagentSpec


def spec(settings):
    return SubagentSpec("search", DISPLAY_NAMES["search"], "Find facts", "Return facts", (), review_config=settings)


@pytest.fixture
def context(sandbox_dir):
    value = RunContext("repair", "thread", sandbox_dir, user_query="Agent A tools", policy=load_runtime_policy())
    token = set_run_context(value)
    try:
        yield value
    finally:
        reset_run_context(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("sync", [False, True])
async def test_invalid_review_rechecks_same_candidate_without_research(context, sync):
    model = ScriptedModel(responses=[AIMessage(content="invalid JSON"), AIMessage(content=json.dumps(decision()))])
    reviewer = ResultReviewer(ReviewConfig(enabled=True, model_check=True), model)
    result = (
        reviewer.review_sync("Agent A", report(fact()), [evidence()])
        if sync
        else await reviewer.review("Agent A", report(fact()), [evidence()])
    )
    assert result.verdict == "passed" and len(model.seen_messages) == 2
    assert context.call_counts["retry"] == 1 and not context.search_calls
    first = json.loads(model.seen_messages[0][1].text)
    second = json.loads(model.seen_messages[1][1].text)
    assert first == second


def test_repeated_legacy_review_is_rejected_without_compatibility(context):
    old = AIMessage(content='{"verdict":"passed","findings":[]}')
    model = ScriptedModel(responses=[old, old])
    result = ResultReviewer(ReviewConfig(enabled=True, model_check=True), model).review_sync(
        "Agent A", report(fact()), [evidence()]
    )
    assert result.verdict == "check_incomplete" and len(model.seen_messages) == 2
    assert result.accepted_report is None


def test_review_budget_exhaustion_keeps_the_report_unapproved(context):
    context.call_counts["review"] = context.policy.budgets.review
    result = ResultReviewer(ReviewConfig(enabled=True, model_check=True), ScriptedModel(responses=[])).review_sync(
        "Agent A", report(fact()), [evidence()]
    )
    assert result.verdict == "budget_exhausted" and result.accepted_report is None
