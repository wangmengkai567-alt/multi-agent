"""Grounded facts survive compression, temporal resolution and final correction."""

import copy
import json

import pytest
from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from test_framework_contract import ScriptedModel, call
from test_review_contract import finding, rejected

from agent.reflection import ResultReviewer, ReviewResult, rule_review, source_id
from agent.runtime.answer_review import AnswerReviewMiddleware
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.evidence import model_evidence
from agent.runtime.facts import ReportError, approved_reports, parse_report, report_references
from agent.runtime.hooks import HookToolMiddleware
from agent.runtime.models import budget_model
from agent.runtime.policies import load_runtime_policy
from agent.runtime.search import SearchTaskBudget
from agent.subagent_factory import build_subagents
from agent.subagent_registry import DISPLAY_NAMES, ReviewConfig, SubagentRegistry, SubagentSpec

URL = "https://official.example.test/device"
BODY = "设备A在中国大陆售价100元，自2026年10月20日生效。"


def evidence(body=BODY, url=URL, name="internet_search"):
    return dict(
        name=name, call_id=url, executed=True, status="completed", content={"results": [{"url": url, "content": body}]}
    )


def fact(ident="price", value="100元", scope="中国大陆", when=None, body=BODY, url=URL):
    return dict(
        id=ident,
        subject="设备A",
        aspect="售价",
        value=value,
        scope=scope,
        evidence=[dict(source_id=source_id(url), quote=body)],
        as_of=when,
        time_evidence=dict(source_id=source_id(url), quote=body) if when else None,
    )


def report(*facts, analysis=(), gaps=()):
    return json.dumps(dict(facts=list(facts), analysis=list(analysis), gaps=list(gaps)), ensure_ascii=False)


@pytest.fixture
def context(sandbox_dir):
    context = RunContext(
        "facts", "thread", sandbox_dir, policy=load_runtime_policy(), user_query="比较设备A的当前价格并说明适合谁购买"
    )
    token = set_run_context(context)
    try:
        yield context
    finally:
        reset_run_context(token)


def test_report_requires_actual_local_source_and_verbatim_body():
    item = fact()
    assert parse_report(report(item), [evidence()], 4000)["facts"][0]["value"] == "100元"
    for changed in (dict(source_id="other-task", quote=BODY), dict(source_id=source_id(URL), quote="编造的原文")):
        item["evidence"] = [changed]
        with pytest.raises(ReportError, match="真实来源"):
            parse_report(report(item), [evidence()], 4000)


@pytest.mark.parametrize("status,executed", [("failed", True), ("completed", False)])
def test_failed_or_unexecuted_records_cannot_support_a_fact(status, executed):
    record = evidence()
    record.update(status=status, executed=executed)
    with pytest.raises(ReportError, match="真实来源"):
        parse_report(report(fact()), [record], 4000)


def test_actual_effective_date_is_required_in_the_time_quote():
    item = fact(when="2026-10-21")
    with pytest.raises(ReportError, match="生效日期"):
        parse_report(report(item), [evidence()], 4000)
    item["as_of"] = None
    with pytest.raises(ReportError, match="同时提供"):
        parse_report(report(item), [evidence()], 4000)


def test_newest_dated_fact_supersedes_old_value_in_the_same_scope():
    old_body = "设备A在中国大陆售价120元，自2026年9月1日生效。"
    old = fact("old", "120元", when="2026-09-01", body=old_body, url=URL + "/old")
    new = fact("new", when="2026-10-20")
    result = parse_report(report(old, new), [evidence(old_body, URL + "/old"), evidence()], 4000)
    assert [item["id"] for item in result["facts"]] == ["new"]
    assert [item["id"] for item in result["superseded"]] == ["old"]
    assert result["conflicts"] == []


@pytest.mark.parametrize("old_date", [None, "2026-10-20"])
def test_undated_or_same_day_disagreement_is_unresolved(old_date):
    other_body = "设备A在中国大陆售价120元，自2026年10月20日生效。"
    old = fact("old", "120元", when=old_date, body=other_body, url=URL + "/other")
    result = parse_report(
        report(old, fact("new", when="2026-10-20")), [evidence(other_body, URL + "/other"), evidence()], 4000
    )
    assert result["facts"] == [] and len(result["conflicts"]) == 1
    assert result["superseded"] == []


def test_different_regions_are_not_temporally_overwritten():
    other_body = "设备A在香港售价120港元，自2026年9月1日生效。"
    other = fact("hk", "120港元", "香港", "2026-09-01", other_body, URL + "/hk")
    result = parse_report(
        report(other, fact("cn", when="2026-10-20")), [evidence(other_body, URL + "/hk"), evidence()], 4000
    )
    assert len(result["facts"]) == 2 and not result["superseded"] and not result["conflicts"]


def test_analysis_cannot_cite_unknown_or_superseded_facts():
    for ident in ("unknown", "old"):
        old_body = "设备A售价120元，自2026年9月1日生效。"
        old = fact("old", "120元", when="2026-09-01", body=old_body, url=URL + "/old")
        with pytest.raises(ReportError, match="分析引用"):
            parse_report(
                report(old, fact("new", when="2026-10-20"), analysis=[dict(statement="价格下降", fact_ids=[ident])]),
                [evidence(old_body, URL + "/old"), evidence()],
                4000,
            )


def test_empty_report_must_disclose_a_gap_and_plain_prose_is_rejected():
    assert parse_report(report(gaps=["当前价格尚未核实"]), [], 4000)["gaps"]
    for text in (report(), "设备A售价100元 " + URL):
        with pytest.raises(ReportError):
            parse_report(text, [evidence()], 4000)


def test_pinned_fact_quote_survives_compression_without_mutating_evidence():
    body = "irrelevant introduction " * 500 + BODY + " irrelevant appendix" * 500
    records = [evidence(body), evidence("background " * 100, URL + "/background", "read_web_page")]
    original = copy.deepcopy(records)
    parsed = parse_report(report(fact()), records, 4000)
    compact = model_evidence(records, 300, references=report_references([parsed]))
    rows = [row for record in compact for row in record["content"]["results"]]
    assert BODY in rows[0]["content"]
    assert sum(len(row["content"]) for row in rows) <= 300
    assert records == original
    with pytest.raises(ValueError, match="budget"):
        model_evidence(records, 5, references=report_references([parsed]))


def test_cross_task_resolution_keeps_newest_fact_and_removes_obsolete_analysis(context):
    old_body = "设备A在中国大陆售价120元，自2026年9月1日生效。"
    old_fact = fact("price", "120元", when="2026-09-01", body=old_body, url=URL + "/old")
    old = parse_report(
        report(old_fact, analysis=[dict(statement="预算至少120元", fact_ids=["price"])]),
        [evidence(old_body, URL + "/old")],
        4000,
    )
    new = parse_report(report(fact(when="2026-10-20")), [evidence()], 4000)
    context.search_tasks = {
        "old": SearchTaskBudget(check_status="passed", report=old),
        "new": SearchTaskBudget(check_status="passed", report=new),
    }
    merged = approved_reports(context)[0]
    assert [item["value"] for item in merged["facts"]] == ["100元"]
    assert [item["value"] for item in merged["superseded"]] == ["120元"]
    assert merged["analysis"] == []
    assert len({item["fact_id"] for item in merged["origins"]}) == 2
    assert old["analysis"] and old["facts"][0]["id"] == "price"


def test_grounded_report_survives_job_checkpoint(context):
    from agent.runtime.checkpoints import restore_job_checkpoint, save_job_checkpoint

    parsed = parse_report(report(fact(when="2026-10-20")), [evidence()], 4000)
    context.search_tasks["search"] = SearchTaskBudget(
        check_status="passed", report=parsed, result="reviewed result", evidence=[evidence()]
    )
    context.job_checkpoint_path = context.work_dir / "checkpoint.json"
    save_job_checkpoint(context)
    restored = RunContext(
        "resumed", "thread", context.work_dir, policy=context.policy, job_checkpoint_path=context.job_checkpoint_path
    )
    restore_job_checkpoint(restored)
    assert restored.search_tasks["search"].report == parsed
    assert approved_reports(restored) == approved_reports(context)


@pytest.mark.parametrize("unavailable", [False, True])
def test_error_page_cannot_be_used_as_fact_evidence(unavailable):
    record = evidence(name="read_web_page")
    if unavailable:
        record["content"]["read_status"] = "unavailable"
    else:
        record["content"]["results"][0]["title"] = "404 Not Found"
    with pytest.raises(ReportError, match="真实来源"):
        parse_report(report(fact()), [record], 4000)


def test_bounded_report_rejects_too_many_quote_characters():
    item = fact(body="supported sentence " * 30)
    body = item["evidence"][0]["quote"]
    with pytest.raises(ReportError, match="过长"):
        parse_report(report(item), [evidence(body)], 300)


@pytest.mark.parametrize("enabled,model_check", [(False, True), (True, False)])
def test_grounded_reports_cannot_disable_semantic_review(enabled, model_check):
    with pytest.raises(ValueError, match="semantic review"):
        ReviewConfig(grounded_report=True, enabled=enabled, model_check=model_check)


@pytest.mark.asyncio
async def test_real_graph_uses_grounded_child_facts_and_corrects_invented_main_fact(context):
    actual = []

    @tool
    def internet_search(query: str):
        """Return controlled source text."""
        actual.append(query)
        return json.dumps({"results": [{"url": URL, "content": BODY}]})

    settings = ReviewConfig(
        enabled=True, model_check=True, grounded_report=True, required_tools=("internet_search",), require_sources=True
    )
    spec = SubagentSpec(
        "search",
        DISPLAY_NAMES["search"],
        "Find facts",
        "Return verified facts",
        (internet_search,),
        review_config=settings,
    )
    registry = SubagentRegistry([spec])
    child = ScriptedModel(
        responses=[
            call("internet_search", {"query": "设备A当前价格"}, "search"),
            AIMessage(content="资料收集完成"),
            AIMessage(content=report(fact(when="2026-10-20"))),
        ]
    )
    draft = "设备A售价100元，发布时间为2026年4月20日。" + URL
    corrected = "设备A在中国大陆自2026年10月20日起售价100元。" + URL
    root = ScriptedModel(
        responses=[
            call("task", {"description": "核实设备A当前价格", "subagent_type": "search"}, "task"),
            AIMessage(content=draft),
            AIMessage(content=corrected),
        ]
    )
    reviewer = ScriptedModel(
        responses=[
            AIMessage(content=ReviewResult(verdict="passed").model_dump_json()),
            AIMessage(
                content=json.dumps(
                    {
                        **json.loads(
                            rejected(
                                finding(
                                    "unsupported_claim",
                                    requirement_quote="",
                                    scope_quote="",
                                    claim_quote="发布时间为2026年4月20日",
                                )
                            ).text
                        ),
                        "answer_kind": "partial",
                    },
                    ensure_ascii=False,
                )
            ),
            AIMessage(content=ReviewResult(verdict="passed").model_dump_json()),
        ]
    )
    graph = create_deep_agent(
        model=budget_model(root, "main_model"),
        subagents=build_subagents(registry, child, review_model=reviewer),
        middleware=[
            AnswerReviewMiddleware(reviewer),
            HookToolMiddleware(allowed_targets=registry.specs, managed_delegation=True),
        ],
    )
    result = await graph.ainvoke({"messages": [HumanMessage(content=context.user_query)]})
    assert result["messages"][-1].text == corrected
    assert actual == ["设备A当前价格"] and len(child.seen_messages) == 3
    assert len(reviewer.seen_messages) == 3 and context.answer_repair_attempted
    data = json.loads(root.seen_messages[-1][-1].text)
    assert data["verified_reports"][0]["facts"][0]["value"] == "100元"
    assert BODY in str(data["tools"])
    assert next(iter(context.search_tasks.values())).report is not None


def test_verbatim_quote_does_not_bypass_semantic_review(context):
    # A real quote can still be assigned to an unsupported value.
    text = report(fact(value="90元"))
    settings = ReviewConfig(enabled=True, model_check=True, grounded_report=True)
    assert rule_review(text, [evidence()], settings).verdict == "passed"
    model = ScriptedModel(
        responses=[rejected(finding("unsupported_claim", requirement_quote="", scope_quote="", claim_quote="90元"))]
    )
    assert ResultReviewer(settings, model).review_sync("核实售价", text, [evidence()]).verdict == "insufficient"
    payload = json.loads(model.seen_messages[0][-1].text)
    assert payload["verified_reports"] == [] and payload["candidate_report"]["facts"][0]["value"] == "90元"


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_semantic_review_records_its_own_phase_once(context, asynchronous):
    from types import SimpleNamespace

    events = []
    context.event_sink = SimpleNamespace(submit=events.append)
    model = ScriptedModel(responses=[AIMessage(content=ReviewResult(verdict="passed").model_dump_json())])
    checker = ResultReviewer(ReviewConfig(enabled=True, model_check=True, grounded_report=True), model)
    result = (
        await checker.review("核实售价", report(fact()), [evidence()])
        if asynchronous
        else checker.review_sync("核实售价", report(fact()), [evidence()])
    )
    assert result.verdict == "passed" and context.call_counts["review"] == 1
    assert len(events) == 1 and events[0]["data"]["phase"] == "semantic_review"
    assert BODY not in json.dumps(events, ensure_ascii=False)


def test_report_type_error_explains_array_requirement_without_echoing_input():
    draft = json.loads(report(fact()))
    draft.update(analysis="private-draft-value", gaps="private-draft-value")
    with pytest.raises(ReportError) as error:
        parse_report(json.dumps(draft, ensure_ascii=False), [evidence()], 4000)
    message = str(error.value)
    assert "analysis" in message and "gaps" in message and "array" in message
    assert "private-draft-value" not in message


def test_research_does_not_mix_report_schema_with_tools(context):
    from langchain.agents.middleware import ModelRequest
    from langchain_core.messages import SystemMessage

    from agent.runtime.search import SearchAllowance, SearchExecutionMiddleware, search_allowance

    allowance = SearchAllowance(context)
    token = search_allowance.set(allowance)
    try:
        middleware = SearchExecutionMiddleware()
        request = ModelRequest(
            model=ScriptedModel(responses=[]),
            messages=[],
            tools=[{"name": "internet_search"}],
            system_message=SystemMessage(content="report"),
        )
        collection = middleware.final_request(request)
        assert collection.model_settings["strict"] is True
        assert "response_format" not in collection.model_settings
        allowance.task.finalize_only = True
        summary = middleware.final_request(request)
        assert summary.tools == [] and "strict" not in summary.model_settings
        assert "response_format" not in summary.model_settings
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
        HookCall("search", "child", "parent", args={"description": "核实售价"}, target="search")
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


@pytest.mark.asyncio
@pytest.mark.parametrize("corrected", [True, False])
async def test_malformed_report_is_corrected_once_without_another_search(context, corrected):
    actual = []

    @tool
    def internet_search(query: str):
        """Return actual controlled evidence."""
        actual.append(query)
        return json.dumps({"results": [{"url": URL, "content": BODY}]})

    settings = ReviewConfig(enabled=True, model_check=True, grounded_report=True, required_tools=("internet_search",))
    spec = SubagentSpec(
        "search", DISPLAY_NAMES["search"], "Find facts", "Research", (internet_search,), review_config=settings
    )
    bad = report(fact(), analysis=["bad analysis"])
    child = ScriptedModel(
        responses=[
            call("internet_search", {"query": "售价"}),
            AIMessage(content="research-history-marker"),
            AIMessage(content=bad),
            AIMessage(content=report(fact()) if corrected else bad),
        ]
    )
    reviewer = ScriptedModel(responses=[AIMessage(content=ReviewResult(verdict="passed").model_dump_json())])
    runnable = build_subagents(SubagentRegistry([spec]), child, review_model=reviewer)[0]["runnable"]
    result = await runnable.ainvoke({"messages": [HumanMessage(content="核实售价")]})
    assert ("verified_report" in json.loads(result["messages"][-1].text)) == corrected
    assert len(actual) == 1 and len(child.seen_messages) == 4 and len(reviewer.seen_messages) == int(corrected)
    repaired = child.seen_messages[-1]
    assert len(repaired) == 2 and "research-history-marker" not in str(repaired)
    assert json.loads(repaired[-1].text)["draft"] == bad
    assert context.call_counts["retry"] == 1
    task = next(iter(context.search_tasks.values()))
    assert (task.report is not None) == corrected
    if not corrected:
        assert "bad analysis" not in result["messages"][-1].text


@pytest.mark.asyncio
async def test_report_timeout_keeps_evidence_without_adopting_a_report(context):
    class TimeoutModel(ScriptedModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            if not self.responses:
                raise TimeoutError("controlled report timeout")
            return await super()._agenerate(messages, stop, run_manager, **kwargs)

    @tool
    def internet_search(query: str):
        """Return controlled evidence before the report timeout."""
        return json.dumps({"results": [{"url": URL, "content": BODY}]})

    settings = ReviewConfig(enabled=True, model_check=True, grounded_report=True)
    spec = SubagentSpec(
        "search", DISPLAY_NAMES["search"], "Find facts", "Research", (internet_search,), review_config=settings
    )
    child = TimeoutModel(responses=[call("internet_search", {"query": "售价"}), AIMessage(content="收集完成")])
    reviewer = ScriptedModel(responses=[])
    runnable = build_subagents(SubagentRegistry([spec]), child, review_model=reviewer)[0]["runnable"]
    result = await runnable.ainvoke({"messages": [HumanMessage(content="核实售价")]})
    assert json.loads(result["messages"][-1].text)["review"]["verdict"] == "dependency_unavailable"
    task = next(iter(context.search_tasks.values()))
    assert task.report is None and task.evidence[0]["executed"] and task.sources == [URL]
    assert reviewer.seen_messages == [] and context.call_counts["retry"] == 0


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
        executor.invoke({"messages": [HumanMessage(content="核实售价")]})
    assert model.seen_messages == [] and context.call_counts["sub_model"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("corrected", [False, True])
async def test_invalid_tool_json_uses_the_existing_single_refinement(context, corrected):
    actual = []

    @tool
    def internet_search(query: str):
        """Return controlled evidence after the model repairs its request."""
        actual.append(query)
        return json.dumps({"results": [{"url": URL, "content": BODY}]})

    settings = ReviewConfig(enabled=True, model_check=True, grounded_report=True)
    spec = SubagentSpec(
        "search", DISPLAY_NAMES["search"], "Find facts", "Research", (internet_search,), review_config=settings
    )
    invalid = AIMessage(
        content="",
        invalid_tool_calls=[{"name": "internet_search", "args": "{}extra", "id": "bad", "error": "invalid JSON"}],
    )
    child = ScriptedModel(
        responses=[
            invalid,
            call("internet_search", {"query": "售价"}),
            AIMessage(content="收集完成"),
            AIMessage(content=report(fact())),
        ]
        if corrected
        else [invalid, invalid]
    )
    reviewer = ScriptedModel(responses=[AIMessage(content=ReviewResult(verdict="passed").model_dump_json())])
    runnable = build_subagents(SubagentRegistry([spec]), child, review_model=reviewer)[0]["runnable"]
    result = await runnable.ainvoke({"messages": [HumanMessage(content="核实售价")]})
    assert ("verified_report" in json.loads(result["messages"][-1].text)) == corrected
    assert actual == (["售价"] if corrected else []) and context.call_counts["retry"] == 1
    assert len(child.seen_messages) == (4 if corrected else 2)
    assert not any(getattr(message, "invalid_tool_calls", []) for message in child.seen_messages[1])


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
