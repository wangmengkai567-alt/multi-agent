"""Review model output must satisfy the current fact protocol, including bounded repair."""

import json

import pytest
from langchain_core.messages import AIMessage
from protocol_fixtures import decision, evidence, fact, report
from test_framework_contract import ScriptedModel

from agent.reflection import ResultReviewer
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.policies import load_runtime_policy
from agent.subagent_registry import ReviewConfig


@pytest.fixture
def context(sandbox_dir):
    value = RunContext("review", "thread", sandbox_dir, user_query="Agent A tools", policy=load_runtime_policy())
    token = set_run_context(value)
    try:
        yield value
    finally:
        reset_run_context(token)


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.asyncio
async def test_model_review_accepts_only_complete_per_fact_judgments(context, sync):
    model = ScriptedModel(responses=[AIMessage(content=json.dumps(decision()))])
    reviewer = ResultReviewer(ReviewConfig(enabled=True, model_check=True), model)
    value = (
        reviewer.review_sync("Agent A", report(fact()), [evidence()])
        if sync
        else await reviewer.review("Agent A", report(fact()), [evidence()])
    )
    assert value.verdict == "passed" and value.state.judged
    assert len(value.accepted_report["facts"]) == 1 and len(model.seen_messages) == 1


@pytest.mark.asyncio
async def test_foreign_fact_judgment_never_publishes_a_fact(context):
    invalid = AIMessage(content=json.dumps(decision(fact_id="another-task")))
    reviewer = ResultReviewer(ReviewConfig(enabled=True, model_check=True), ScriptedModel(responses=[invalid, invalid]))
    value = await reviewer.review("Agent A", report(fact()), [evidence()])
    assert value.verdict == "check_incomplete" and value.accepted_report is None
    assert context.call_counts["review"] == 2 and context.call_counts["retry"] == 1


def test_missing_provider_configuration_cannot_publish_its_draft(context):
    reviewer = ResultReviewer(ReviewConfig(enabled=True, model_check=True), None)
    result = reviewer.review_sync("Agent A", report(fact()), [evidence()])
    assert result.verdict == "check_incomplete" and not result.state.judged


def test_grounded_report_review_cannot_be_disabled():
    for settings in ({"enabled": False, "model_check": True}, {"enabled": True, "model_check": False}):
        with pytest.raises(ValueError, match="semantic"):
            ReviewConfig(grounded_report=True, **settings)
