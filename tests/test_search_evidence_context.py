"""Bounded synthesis keeps original evidence and the current task's Skill guidance."""

import copy
import json

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from test_framework_contract import ScriptedModel

from agent.reflection import tool_evidence
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.evidence import model_evidence
from agent.runtime.hooks import HookCall, active_call
from agent.runtime.policies import load_runtime_policy
from agent.runtime.search import SearchAllowance, SearchExecutionMiddleware, search_allowance


@pytest.fixture
def runtime_context(sandbox_dir):
    context = RunContext("synthesis", "thread", sandbox_dir, policy=load_runtime_policy())
    token = set_run_context(context)
    try:
        yield context
    finally:
        reset_run_context(token)


def test_excerpt_budget_covers_all_queries_and_does_not_change_originals():
    evidence = [
        dict(
            name="internet_search",
            call_id=str(i),
            executed=True,
            status="completed",
            request_parameters={"query": str(i)},
            content={
                "query": str(i),
                "results": [
                    {
                        "url": f"https://example.test/{i}/{j}",
                        "title": str(j),
                        "content": "head" * 300 + "tail" * 300,
                        "raw_content": "hidden" * 10000,
                    }
                    for j in range(5)
                ],
            },
        )
        for i in range(8)
    ]
    original = copy.deepcopy(evidence)
    compact = model_evidence(evidence, 12000)
    rows = [row for item in compact for row in item["content"]["results"]]
    assert sum(len(row["content"]) for row in rows) <= 12000
    assert len(rows) == 40 and all(row["excerpt_truncated"] for row in rows)
    assert all("raw_content" not in row for row in rows)
    assert all("hidden" in row["content"] and "head" not in row["content"] for row in rows)
    assert {row["url"] for row in rows} == {row["url"] for item in evidence for row in item["content"]["results"]}
    assert evidence == original


def test_synthesis_has_no_tool_history_or_foreign_task_guidance(runtime_context):
    context = runtime_context
    allowance = SearchAllowance(context)
    allowance.reserve("one")
    allowance.reserve("two")
    records = [
        dict(
            name="internet_search",
            call_id="one",
            executed=True,
            status="completed",
            request_parameters={"query": "launch"},
            content={"results": [{"url": "https://example.test/launch", "content": "verified launch date"}]},
        )
    ]
    context.skill_bodies[("search", "owner", "research")] = "current method"
    context.skill_bodies[("search", "different-owner", "research")] = "another task method"
    scope = search_allowance.set(allowance)
    collector = tool_evidence.set(records)
    call = active_call.set(HookCall("search", "owner"))
    try:
        request = ModelRequest(
            model=ScriptedModel(responses=[]),
            tools=[{"name": "internet_search"}],
            system_message=SystemMessage(content="framework tool protocol"),
            messages=[
                HumanMessage(content="original task"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "internet_search", "args": {"query": "launch"}, "id": "one", "type": "tool_call"}
                    ],
                ),
                ToolMessage(content="old tool history", tool_call_id="one"),
                HumanMessage(content="correct the launch date"),
            ],
        )
        final = SearchExecutionMiddleware("role constraints").final_request(request)
        assert final.tools == [] and final.model_settings["tool_choice"] == "none"
        assert len(final.messages) == 1 and isinstance(final.messages[0], HumanMessage)
        data = json.loads(final.messages[0].text)
        assert data["tasks"] == ["original task", "correct the launch date"]
        assert data["tools"][0]["content"]["results"][0]["url"] == "https://example.test/launch"
        assert data["skill_guidance"] == ["current method"]
        assert "framework tool protocol" not in final.system_message.text
        assert "role constraints" in final.system_message.text
        assert len(request.messages) == 4 and request.tools
    finally:
        active_call.reset(call)
        tool_evidence.reset(collector)
        search_allowance.reset(scope)


def test_context_uses_relevant_middle_from_original_not_an_already_cut_excerpt():
    body = (
        "opening background " * 80
        + "ExceptionGroup: remaining failures are combined and raised."
        + " unrelated appendix" * 80
    )
    records = [
        dict(
            name="internet_search",
            call_id="one",
            executed=True,
            status="completed",
            request_parameters={"query": "TaskGroup ExceptionGroup"},
            content={"results": [{"url": "https://official.test/3.11", "content": body}]},
            model_content=json.dumps(
                {"results": [{"url": "https://official.test/3.11", "content": body[:1000], "excerpt_truncated": True}]}
            ),
        )
    ]
    original = copy.deepcopy(records)
    selected = model_evidence(records, 600)[0]["content"]["results"][0]
    assert "ExceptionGroup: remaining failures" in selected["content"]
    assert len(selected["content"]) <= 600 and selected["excerpt_truncated"]
    assert records == original


def test_single_search_keeps_query_evidence_in_middle_without_inventing_it(runtime_context):
    from tools.tavily_tool import compact_search_response

    body = "unrelated introduction " * 90 + "上市日期：2026年4月22日。" + " other background" * 90
    data = {"results": [{"url": "https://official.test/launch", "content": body}]}
    output = json.loads(compact_search_response(data, runtime_context.policy.search, "指定产品 上市日期"))
    excerpt = output["results"][0]["content"]
    assert "上市日期：2026年4月22日" in excerpt and len(excerpt) <= 1000
    assert output["results"][0]["url"] == data["results"][0]["url"]
