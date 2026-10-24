"""Canonical fact reports preserve source, identity and date validation."""

import copy
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from protocol_fixtures import BODY, URL, evidence, fact, report
from pydantic import ValidationError
from test_framework_contract import ScriptedModel, call

from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.evidence import model_evidence
from agent.runtime.facts import ReportError, parse_report, resolve_facts
from agent.runtime.models import budget_model
from agent.runtime.policies import load_runtime_policy
from agent.runtime.review_contract import ReviewDecision


def test_report_has_verbatim_local_value_and_identity_evidence():
    value = parse_report(report(fact()), [evidence()], 4000)
    assert not value["fact_errors"] and not value["identity_errors"]
    assert value["sources"][0]["url"] == URL


def test_old_fact_shape_is_rejected():
    item = fact()
    del item["identity_evidence"]
    with pytest.raises(ReportError, match="identity_evidence"):
        parse_report(report(item), [evidence()], 4000)


@pytest.mark.parametrize("field", ["evidence", "identity_evidence"])
def test_invalid_quote_is_recorded_for_its_own_fact(field):
    item = fact()
    item[field][0]["quote"] = "invented source text"
    value = parse_report(report(item), [evidence()], 4000)
    assert value["fact_errors" if field == "evidence" else "identity_errors"]


@pytest.mark.parametrize("status,executed", [("failed", True), ("completed", False), ("rejected", False)])
def test_failed_or_unexecuted_calls_do_not_support_a_fact(status, executed):
    row = evidence()
    row.update(status=status, executed=executed)
    assert parse_report(report(fact()), [row], 4000)["fact_errors"]


def test_one_invalid_fact_does_not_erase_valid_sibling_evidence():
    good, bad = fact(), fact("f2", value="unsupported")
    bad["evidence"][0]["quote"] = "not in the actual response"
    value = parse_report(report(good, bad), [evidence()], 4000)
    assert set(value["fact_errors"]) == {"f2"}
    assert len(value["facts"]) == 2


def test_missing_or_unproved_effective_date_is_fact_local():
    item = fact()
    item["as_of"] = "2026-10-24"
    assert parse_report(report(item), [evidence()], 4000)["fact_errors"]
    item["time_evidence"] = item["evidence"][0]
    assert parse_report(report(item), [evidence()], 4000)["fact_errors"]


def test_undated_conflicting_values_do_not_get_ranked_by_retrieval_time():
    values = [
        parse_report(report(fact()), [evidence()], 4000)["facts"][0],
        parse_report(report(fact("f2", value="different")), [evidence()], 4000)["facts"][0],
    ]
    resolved = resolve_facts(values)
    assert not resolved["facts"] and len(resolved["conflicts"]) == 1


def test_empty_report_requires_a_disclosed_gap():
    assert parse_report(report(gaps=["本轮未核实工具调用"]), [], 4000)["gaps"]
    with pytest.raises(ReportError):
        parse_report(report(), [], 4000)
    with pytest.raises(ReportError):
        parse_report("unstructured prose", [evidence()], 4000)


def test_approved_quote_survives_compression_without_mutating_evidence():
    rows = [evidence("background " * 500 + BODY + " appendix " * 500)]
    saved = copy.deepcopy(rows)
    ref = fact()["evidence"][0]
    compact = model_evidence(rows, 300, references=[ref])
    assert ref["quote"] in compact[0]["content"]["results"][0]["content"]
    assert rows == saved
    with pytest.raises(ValueError, match="budget"):
        model_evidence(rows, 5, references=[ref])


def test_duplicate_fact_ids_and_excess_quote_budget_are_rejected():
    with pytest.raises(ReportError, match="唯一"):
        parse_report(report(fact(), fact()), [evidence()], 4000)
    with pytest.raises(ReportError, match="过长"):
        parse_report(report(fact()), [evidence()], 20)


def test_runtime_result_is_not_a_model_review_decision():
    with pytest.raises(ValidationError):
        ReviewDecision.model_validate({"verdict": "passed", "issues": [], "state": {}})


@pytest.fixture
def context(sandbox_dir):
    value = RunContext("reports", "thread", sandbox_dir, user_query="Agent A tools", policy=load_runtime_policy())
    token = set_run_context(value)
    try:
        yield value
    finally:
        reset_run_context(token)


def test_report_type_error_explains_array_requirement_without_echoing_input():
    draft = json.loads(report(fact()))
    draft.update(analysis="private-draft-value", gaps="private-draft-value")
    with pytest.raises(ReportError) as error:
        parse_report(json.dumps(draft, ensure_ascii=False), [evidence()], 4000)
    message = str(error.value)
    assert "analysis" in message and "gaps" in message and "array" in message
    assert "private-draft-value" not in message


def test_collection_exit_never_generates_a_report_or_calls_tools(context):
    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain_core.messages import SystemMessage

    from agent.runtime.search import SearchAllowance, SearchExecutionMiddleware, search_allowance

    allowance = SearchAllowance(context)
    allowance.task.finalize_only = True
    token = search_allowance.set(allowance)
    try:
        request = ModelRequest(
            model=ScriptedModel(responses=[]), messages=[], tools=[], system_message=SystemMessage(content="research")
        )
        response = SearchExecutionMiddleware().prepare_request(request)
        assert isinstance(response, ModelResponse)
        assert not response.result[-1].tool_calls
        assert context.call_counts["sub_model"] == 0
    finally:
        search_allowance.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_report_revision_has_schema_but_no_research_history_or_tools(context, asynchronous):
    from langchain_core.runnables import RunnableLambda

    from agent.reflection import tool_evidence
    from agent.runtime.hooks import HookCall, active_call
    from agent.runtime.reporting import GroundedSearchExecutor, ReportRevision

    def unexpected_research(_):
        raise AssertionError("Format revision must not restart research")

    model = ScriptedModel(responses=[AIMessage(content=report(fact()))])
    executor = GroundedSearchExecutor(RunnableLambda(unexpected_research), budget_model(model, "sub_model"))
    schema = executor.model.kwargs["response_format"]["json_schema"]["schema"]
    assert schema["$defs"]["Analysis"]["properties"]["statement"]["type"] == "string"
    assert "as_of" in schema["$defs"]["Fact"]["required"]
    assert "tools" not in executor.model.kwargs and "strict" not in executor.model.kwargs
    call_token = active_call.set(
        HookCall("search", "child", "parent", args={"description": "Agent A tools"}, target="search")
    )
    evidence_token = tool_evidence.set([evidence()])
    try:
        draft = report(fact(), analysis=["bad analysis"])
        revision = ReportRevision(draft, "analysis.0: Input should be an object")
        result = await executor.ainvoke(revision) if asynchronous else executor.invoke(revision)
    finally:
        active_call.reset(call_token)
        tool_evidence.reset(evidence_token)
    assert len(model.seen_messages) == 1 and len(model.seen_messages[0]) == 2
    data = json.loads(model.seen_messages[0][-1].text)
    assert data["draft"] == draft and "analysis.0" in data["correction"]
    assert data["tools"][0]["content"]["results"][0]["content"] == BODY
    assert "messages" not in data and "tasks" not in data
    assert parse_report(result["messages"][-1].text, [evidence()], 4000)["facts"]
    assert context.call_counts["sub_model"] == 1 and context.call_counts["tool"] == 0


@pytest.mark.parametrize("pending", [False, True])
def test_incomplete_or_invalid_research_never_enters_report_generation(context, pending):
    from langchain_core.runnables import RunnableLambda

    from agent.runtime.reporting import GroundedSearchExecutor

    message = (
        call("internet_search", {"query": "pending"})
        if pending
        else AIMessage(
            content="",
            invalid_tool_calls=[{"name": "internet_search", "args": "{}extra", "id": "bad", "error": "invalid JSON"}],
        )
    )
    model = ScriptedModel(responses=[])
    executor = GroundedSearchExecutor(
        RunnableLambda(lambda _: {"messages": [message]}), budget_model(model, "sub_model")
    )
    with pytest.raises(RuntimeError, match="不能生成事实报告|不是合法 JSON"):
        executor.invoke({"messages": [HumanMessage(content="Agent A tools")]})
    assert model.seen_messages == [] and context.call_counts["sub_model"] == 0


def test_invalid_tool_json_is_not_replayed_as_executed_evidence(context):
    from agent.runtime.reporting import GroundedSearchExecutor, ResearchOutputError

    invalid = AIMessage(
        content="",
        invalid_tool_calls=[{"name": "research_step", "args": "{}extra", "id": "bad", "error": "invalid JSON"}],
    )
    with pytest.raises(ResearchOutputError) as error:
        GroundedSearchExecutor._research_finished({"messages": [HumanMessage(content="task"), invalid]})
    final = error.value.result["messages"][-1]
    assert not final.tool_calls and not final.invalid_tool_calls
    assert context.call_counts["tool"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "timeout", "cancelled"])
async def test_model_phase_records_sizes_usage_and_failures_without_prompt_bodies(context, failure):
    import asyncio
    from types import SimpleNamespace

    from langchain_core.runnables import RunnableLambda

    from agent.runtime.models import ModelRequestTimeout, model_phase

    events = []
    context.event_sink = SimpleNamespace(submit=events.append)
    started = asyncio.Event()

    async def respond(messages, **kwargs):
        if failure == "timeout":
            raise TimeoutError("private-service-detail")
        if failure == "cancelled":
            started.set()
            await asyncio.Event().wait()
        return AIMessage(
            content="private-report",
            usage_metadata={
                "input_tokens": 9,
                "output_tokens": 7,
                "total_tokens": 16,
                "output_token_details": {"reasoning": 3},
            },
        )

    token = model_phase.set("report_revision")
    try:
        request = budget_model(RunnableLambda(respond), "sub_model")
        if failure == "cancelled":
            task = asyncio.create_task(request.ainvoke([HumanMessage(content="private-prompt")]))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif failure == "timeout":
            with pytest.raises(ModelRequestTimeout):
                await request.ainvoke([HumanMessage(content="private-prompt")])
        else:
            await request.ainvoke([HumanMessage(content="private-prompt")])
    finally:
        model_phase.reset(token)
    event = events[-1]
    assert event["data"]["phase"] == "report_revision" and event["data"]["elapsed_ms"] >= 0
    assert event["data"]["input_chars"] == len("private-prompt")
    assert event["status"] == ("failed" if failure else "completed")
    if not failure:
        assert event["data"]["input_units"] == 9 and event["data"]["reasoning_units"] == 3
    assert "private-" not in json.dumps(event)
