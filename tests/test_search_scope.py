"""Current finalization uses the reviewed fact pool and truthful collection status."""

import json

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import HumanMessage, SystemMessage
from test_framework_contract import ScriptedModel

from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.finalization import SearchFinalizationMiddleware
from agent.runtime.output import visible_model_text
from agent.runtime.policies import load_runtime_policy
from agent.runtime.requirements import RequirementSpec, register
from agent.runtime.review_state import ReviewState
from agent.runtime.search import SearchTaskBudget


@pytest.fixture
def context(sandbox_dir):
    value = RunContext("scope", "thread", sandbox_dir, user_query="核实工具调用", policy=load_runtime_policy())
    marker = set_run_context(value)
    try:
        yield value
    finally:
        reset_run_context(marker)


def test_finished_failed_collection_generates_only_registered_limitations(context):
    req = register(context, None, [RequirementSpec(anchor="工具调用", label="工具调用", kind="fact")], owner="main")[0]
    context.search_tasks["a"] = SearchTaskBudget(
        state=ReviewState(check_status="tool_failed"), closed_reason="provider failed"
    )
    context.collecting = False
    request = ModelRequest(
        model=ScriptedModel(responses=[]),
        messages=[HumanMessage(content="old research history")],
        tools=[],
        system_message=SystemMessage(content="role"),
        model_settings={"strict": True},
    )
    final = SearchFinalizationMiddleware().final_request(request)
    payload = json.loads(final.messages[0].text)
    assert payload["fact_pool"]["facts"] == {}
    assert payload["requirements"][0]["rid"] == req["rid"]
    assert payload["collection_status"][0]["check_status"] == "tool_failed"
    assert final.tools == [] and "old research history" not in str(payload)
    assert "strict" not in final.model_settings


@pytest.mark.parametrize(
    "text", ["ordinary answer", "Explain </think> literally", '{"unrelated":true}</think>quoted content']
)
def test_reasoning_cleanup_preserves_unrelated_literal_text(text):
    assert visible_model_text(text) == text


def test_provider_reasoning_envelope_is_removed():
    assert visible_model_text("<think>private reasoning</think>Visible reply") == "Visible reply"
