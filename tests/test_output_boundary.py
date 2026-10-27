"""Actual model output and URL lexical boundaries."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from test_framework_contract import ScriptedModel

from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.models import budget_model
from agent.runtime.policies import load_runtime_policy


@pytest.fixture
def context(sandbox_dir):
    value = RunContext("boundaries", "thread", sandbox_dir, policy=load_runtime_policy())
    token = set_run_context(value)
    try:
        yield value
    finally:
        reset_run_context(token)


def test_normal_main_output_does_not_expose_provider_reasoning(context):
    raw = ScriptedModel(responses=[AIMessage(content="<think>private scratchpad</think>Visible answer")])
    result = budget_model(raw, "main_model").invoke([HumanMessage(content="Any ordinary question")])
    assert result.text == "Visible answer"


def test_url_boundaries_handle_chinese_punctuation_and_serialized_newlines(context):
    from agent.runtime.output import cited_urls

    url = "https://docs.example.test/manual#section"
    assert cited_urls(url + "（来源说明）") == {url}
    assert cited_urls(url + r"\n\n下一段") == {url}
    assert cited_urls("`" + url + "` and example code") == set()
