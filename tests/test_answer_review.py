"""Grounded final delivery cannot bypass fact support or complete claim review."""

import json

import pytest
from langchain_core.messages import AIMessage
from protocol_fixtures import evidence, fact, report
from test_framework_contract import ScriptedModel

from agent.runtime.answer_review import AnswerReviewMiddleware
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.delivery import fact_pool, parse_delivery
from agent.runtime.facts import parse_report, resolve_facts
from agent.runtime.policies import load_runtime_policy
from agent.runtime.review_state import ReviewState
from agent.runtime.search import SearchTaskBudget


@pytest.fixture
def context(sandbox_dir):
    value = RunContext("final", "thread", sandbox_dir, user_query="Agent A delegation", policy=load_runtime_policy())
    parsed = parse_report(report(fact()), [evidence()], 4000)
    accepted = {**parsed, **resolve_facts(parsed["facts"])}
    value.search_tasks["a"] = SearchTaskBudget(
        state=ReviewState.decided("all_passed", "covered"), report=accepted, evidence=[evidence()], closed_reason="done"
    )
    marker = set_run_context(value)
    try:
        yield value
    finally:
        reset_run_context(marker)


def draft(context, *, text="Agent A delegates through task", fact_ids=None):
    ids = list(fact_pool(context)["facts"]) if fact_ids is None else fact_ids
    return json.dumps(
        {
            "blocks": [
                {
                    "claim_id": "c1",
                    "kind": "fact",
                    "text": text,
                    "fact_ids": ids,
                    "analysis_ids": [],
                    "analysis_kind": None,
                    "requirement_ids": [],
                }
            ]
        }
    )


def test_unknown_fact_and_unbound_fact_are_rejected(context):
    for ids in ([], ["another-task:0:f1"]):
        with pytest.raises(ValueError):
            parse_delivery(draft(context, fact_ids=ids), context)


def test_foreign_source_cannot_be_attached_to_valid_fact(context):
    with pytest.raises(ValueError, match="outside"):
        parse_delivery(draft(context, text="claim https://example.test/invented"), context)


@pytest.mark.asyncio
async def test_every_final_block_requires_a_claim_judgment(context):
    text = parse_delivery(draft(context), context)
    invalid = AIMessage(content='{"claims":[],"verdict":"passed","coverage":{},"findings":[]}')
    middleware = AnswerReviewMiddleware(ScriptedModel(responses=[invalid, invalid]))
    result = await middleware._review(text)
    assert result.verdict == "check_incomplete"


@pytest.mark.asyncio
async def test_complete_claim_review_can_publish_checked_source(context):
    text = parse_delivery(draft(context), context)
    response = AIMessage(
        content=json.dumps(
            {
                "claims": [{"claim_id": "c1", "verdict": "passed", "reason": "supported"}],
                "verdict": "passed",
                "coverage": {},
                "findings": [],
            }
        )
    )
    result = await AnswerReviewMiddleware(ScriptedModel(responses=[response]))._review(text)
    assert result.verdict == "passed"
    assert "https://example.test/source" in text


def test_omitted_facts_are_calculated_by_program(context):
    parse_delivery(draft(context), context)
    assert context.delivery_draft["omitted"] == []
    with pytest.raises(ValueError):
        parse_delivery(json.dumps({"blocks": [], "omitted": []}), context)
