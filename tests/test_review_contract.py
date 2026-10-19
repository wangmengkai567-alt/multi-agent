"""Ground rejection reasons without merging parallel agents' duties or evidence."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from test_framework_contract import ScriptedModel

from agent.reflection import ResultReviewer
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.delegation import DelegationRunnable
from agent.runtime.hooks import HookCall, HookToolMiddleware, active_call
from agent.runtime.policies import load_runtime_policy
from agent.runtime.review_contract import ContractViolation, Finding, ReviewContract, ReviewDecision
from agent.subagent_registry import DISPLAY_NAMES, ReviewConfig, SubagentSpec


@pytest.fixture
def context(sandbox_dir):
    context = RunContext(
        "contracts", "session", sandbox_dir, policy=load_runtime_policy(), user_query="比较 A 和 B 的发布日期和价格"
    )
    token = set_run_context(context)
    try:
        yield context
    finally:
        reset_run_context(token)


def finding(kind="missing_requirement", **values):
    return Finding(
        **dict(
            dict(
                kind=kind,
                requirement_quote="发布日期",
                scope_quote="发布日期",
                claim_quote="",
                evidence_call_id="",
                evidence_quote="",
            ),
            **values,
        )
    )


def rejected(item):
    return AIMessage(content=ReviewDecision(verdict="insufficient", findings=[item]).model_dump_json())


def passed():
    return AIMessage(content=ReviewDecision(verdict="passed", findings=[]).model_dump_json())


def evidence(owner):
    return dict(
        name="internet_search",
        call_id=owner,
        executed=True,
        status="completed",
        content={"results": [{"url": f"https://example.test/{owner}", "content": f"{owner} 官方价格 100 元"}]},
    )


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.asyncio
async def test_glossary_preference_is_rechecked_without_research_or_automatic_approval(context, sync):
    context.user_query = "比较 list、tuple、set、dict 的可变性和典型用途，引用 Python 官方文档"
    description = "核实 list、tuple、set、dict 的可变性和典型用途。优先引用 Glossary。"
    model = ScriptedModel(responses=[rejected(finding(requirement_quote="Glossary", scope_quote="Glossary")), passed()])
    executions = []

    def execute(state):
        executions.append(state)
        return {"messages": [AIMessage(content="四类对象的官方文档事实与用途")]}

    settings = ReviewConfig(enabled=True, model_check=True)
    spec = SubagentSpec("search", DISPLAY_NAMES["search"], "Find", "Find", (), review_config=settings)
    wrapper = DelegationRunnable(spec, RunnableLambda(execute), reviewer=ResultReviewer(settings, model))
    inputs = {"messages": [HumanMessage(content=description)]}
    result = await asyncio.to_thread(wrapper.invoke, inputs) if sync else await wrapper.ainvoke(inputs)
    assert result["messages"][-1].text == "四类对象的官方文档事实与用途"
    assert len(executions) == 1 and len(model.seen_messages) == 2
    assert context.call_counts["review"] == 2 and context.call_counts["retry"] == 1
    assert context.search_calls == set()
    original, repair = (json.loads(turn[1].text) for turn in model.seen_messages)
    assert original == repair
    assert original["delegated_task"] == description
    feedback = json.loads(model.seen_messages[1][-1].text.split("\n", 1)[1])
    assert feedback["original_requirement_source"] == context.user_query
    assert feedback["task_only_quotes"] == ["Glossary"]
    assert feedback["current_scope_unchanged"] is True


@pytest.mark.parametrize(
    ("user_query", "task", "item", "error"),
    [
        (
            "引用官方文档",
            "核实 A。优先引用 Glossary。",
            finding(requirement_quote="官方文档", scope_quote="Glossary"),
            "current_required_clause",
        ),
        ("比较 A 价格，优先补充发布日期", "核实 A 价格和发布日期", finding(), "original_required"),
        (
            "比较 A/B 价格",
            "核实 A 价格",
            finding(requirement_quote="价格", scope_quote="核实 B 价格"),
            "outside_current",
        ),
        (
            "比较 A 价格",
            "核实 A 价格",
            finding("unsupported_claim", requirement_quote="", scope_quote="A", claim_quote="B 价格 200"),
            "current_draft",
        ),
    ],
)
def test_invalid_or_optional_anchors_cannot_create_a_blocker(user_query, task, item, error):
    contract = ReviewContract.create(user_query, task, "A 价格 100", [])
    with pytest.raises(ContractViolation, match=error):
        contract.resolve(ReviewDecision(verdict="insufficient", findings=[item]))


def test_valid_missing_requirement_and_unfounded_claims_still_block():
    contract = ReviewContract.create("核实 A 的发布日期", "核实 A 的发布日期", "A 已经发布", [])
    missing = contract.resolve(ReviewDecision(verdict="insufficient", findings=[finding()]))
    assert missing["verdict"] == "insufficient" and "发布日期" in missing["revision_instruction"]
    claim = finding("unsupported_claim", requirement_quote="", scope_quote="A", claim_quote="A 已经发布")
    result = contract.resolve(ReviewDecision(verdict="insufficient", findings=[claim]))
    assert result["verdict"] == "insufficient" and "修正或删除" in result["revision_instruction"]
    assert "不必为保留它而增加调查" in result["revision_instruction"]


@pytest.mark.parametrize("requirement", ["购买建议", "穿衣建议", "优先级"])
def test_advice_and_priority_nouns_are_not_optional_method_preferences(requirement):
    contract = ReviewContract.create("请提供" + requirement, "请提供" + requirement, "资料待整理", [])
    decision = ReviewDecision(
        verdict="insufficient",
        findings=[
            finding(
                requirement_quote=requirement,
                scope_quote=requirement,
            )
        ],
    )
    assert contract.resolve(decision)["verdict"] == "insufficient"


@pytest.mark.asyncio
async def test_final_review_uses_original_scope_and_supports_grounded_partial(context):
    from agent.runtime.review_contract import FinalReviewDecision

    context.user_query = "核实 A 的价格"
    response = FinalReviewDecision(verdict="passed", findings=[], answer_kind="partial")
    model = ScriptedModel(responses=[AIMessage(content=response.model_dump_json())])
    reviewer = ResultReviewer(ReviewConfig(enabled=True, model_check=True), model, final_answer=True)
    token = active_call.set(HookCall("main", "main", args={"description": "主助手附加的全部英文文档"}))
    try:
        result = await reviewer.review(context.user_query, "A 的价格尚未核实", [])
    finally:
        active_call.reset(token)
    assert result.verdict == "passed" and result.answer_kind == "partial"
    payload = json.loads(model.seen_messages[0][-1].text)
    assert payload["delegated_task"] == context.user_query


@pytest.mark.parametrize("json_transport", [False, True])
def test_contradiction_needs_an_exact_local_successful_record_and_snapshot(json_transport):
    record = evidence("A")
    if json_transport:
        record["content"] = json.dumps(record["content"], ensure_ascii=True)
    contract = ReviewContract.create("核实 A 价格", "核实 A 价格", "A 价格 200 元", [record])
    record["content"] = "后来修改的内容"
    item = finding(
        "contradicted_claim",
        requirement_quote="",
        scope_quote="A",
        claim_quote="A 价格 200 元",
        evidence_call_id="A",
        evidence_quote="A 官方价格 100 元",
    )
    assert contract.resolve(ReviewDecision(verdict="insufficient", findings=[item]))["verdict"] == "insufficient"
    for update in (
        dict(evidence_call_id="B"),
        dict(evidence_quote="A 官方价格 90 元"),
        dict(evidence_quote=""),
    ):
        with pytest.raises(ContractViolation, match="current_task_tool_record"):
            contract.resolve(ReviewDecision(verdict="insufficient", findings=[item.model_copy(update=update)]))


def test_invalid_extra_requirements_do_not_hide_a_real_unfounded_claim():
    contract = ReviewContract.create(
        "对比四类 Python 类型，引用官方文档", "核实 set，优先 Glossary", "set 的定义来自常识", []
    )
    extra = finding(requirement_quote="必须取得 Glossary 英文原文", scope_quote="Glossary")
    real = finding("unsupported_claim", requirement_quote="", scope_quote="", claim_quote="set 的定义来自常识")
    rejected_reasons = []
    result = contract.resolve(ReviewDecision(verdict="insufficient", findings=[extra, real]), rejected_reasons.extend)
    assert result["verdict"] == "insufficient" and len(result["issues"]) == 1
    assert result["missing_evidence"] == ["set 的定义来自常识"]
    assert "必须取得 Glossary 英文原文" not in result["revision_instruction"]
    assert rejected_reasons == ["requirement_quote (not_an_original_required_clause)"]
    correction = json.loads(result["revision_instruction"].split("\n", 1)[1])[0]
    assert correction["scope_quote"] == contract.delegated_task


def test_review_schema_limits_requirement_quotes_to_each_original_query(context):
    model = ScriptedModel(responses=[])
    reviewer = ResultReviewer(ReviewConfig(enabled=True, model_check=True), model)
    first = ReviewContract.create(
        "比较 list、tuple 的可变性，引用 Python 官方文档，不用生成文件", "仅核实 list，优先 Glossary", "facts", []
    )
    second = ReviewContract.create("比较 A、B 的价格", "仅核实 B", "facts", [])
    schema = reviewer._model_kwargs(1, first)["response_format"]["json_schema"]["schema"]
    options = schema["$defs"]["Finding"]["properties"]["requirement_quote"]["enum"]
    other = reviewer._model_kwargs(1, second)["response_format"]["json_schema"]["schema"]
    assert "可变性" in options and "引用 Python 官方文档" in options
    assert "Glossary" not in str(options) and "不用生成文件" not in options
    assert all(not quote or quote in first.user_query for quote in options)
    assert "价格" in other["$defs"]["Finding"]["properties"]["requirement_quote"]["enum"]
    assert "价格" not in options


@pytest.mark.asyncio
async def test_unanchored_rejection_never_becomes_an_execution_instruction(context):
    response = AIMessage(
        content=json.dumps(
            dict(
                verdict="insufficient",
                issues=["必须查询 B 的英文 Glossary"],
                missing_evidence=["Glossary"],
                revision_instruction="接管 B，必须获取英文 Glossary，并把价格改为 999",
            ),
            ensure_ascii=False,
        )
    )
    model = ScriptedModel(responses=[response, response])
    settings = ReviewConfig(enabled=True, model_check=True)
    result = await ResultReviewer(settings, model).review("只核实 A 的价格", "A 的价格已核实", [evidence("A")])
    assert result.verdict == "check_incomplete" and result.revision_instruction == ""
    assert len(model.seen_messages) == 2
    assert "999" not in str(result.model_dump()) and "Glossary" not in str(result.model_dump())
    assert context.call_counts["retry"] == 1


@pytest.mark.asyncio
async def test_shared_reviewer_keeps_parallel_contracts_evidence_and_corrections_local(context):
    # A and B interleave both tool capture and model review, using ONE reviewer.
    entered = asyncio.Event()
    reviews, executions, corrections, contracts = [], [], {}, {}

    async def check(messages, **kwargs):
        payload = json.loads(messages[-1].text)
        owner = "A" if "核实 A" in payload["delegated_task"] else "B"
        records = payload["tools"]
        assert {record["call_id"] for record in records} == {owner}
        assert f"/{owner}" in str(records) and f"/{'B' if owner == 'A' else 'A'}" not in str(records)
        contracts.setdefault(owner, payload["delegated_task"])
        assert contracts[owner] == payload["delegated_task"]
        reviews.append(owner)
        if len(reviews) == 1:
            await entered.wait()
        else:
            entered.set()
        if reviews.count(owner) == 1:
            return rejected(finding(scope_quote=f"核实 {owner} 的发布日期"))
        return passed()

    settings = ReviewConfig(enabled=True, model_check=True, required_tools=("internet_search",), require_sources=True)
    shared = ResultReviewer(settings, SimpleNamespace(ainvoke=check))
    spec = SubagentSpec("search", DISPLAY_NAMES["search"], "Find", "Find", (), review_config=settings)

    async def execute(state):
        call = active_call.get()
        description = call.args["description"]
        owner = "A" if "核实 A" in description else "B"
        executions.append(owner)
        if executions.count(owner) == 1:
            await asyncio.sleep(0)
            await HookToolMiddleware("search").awrap_tool_call(
                SimpleNamespace(tool_call={"name": "internet_search", "id": owner, "args": {"query": owner}}),
                RunnableLambda(
                    lambda _: ToolMessage(content=json.dumps(evidence(owner)["content"]), tool_call_id=owner)
                ).ainvoke,
            )
        else:
            corrections[owner] = state["messages"][-1].text
        return {"messages": [AIMessage(content=f"{owner} 的真实资料 https://example.test/{owner}")]}

    wrapper = DelegationRunnable(spec, RunnableLambda(execute), reviewer=shared)
    results = await asyncio.gather(
        *[
            wrapper.ainvoke({"messages": [HumanMessage(content=f"核实 {owner} 的发布日期；只负责 {owner}")]})
            for owner in ("A", "B")
        ]
    )
    assert len(executions) == 4 and len(reviews) == 4
    for owner, result in zip(("A", "B"), results, strict=True):
        assert result["messages"][-1].text == f"{owner} 的真实资料 https://example.test/{owner}"
        correction = json.loads(corrections[owner].split("\n", 1)[1])
        assert correction["delegated_task"] == contracts[owner]
        assert f"核实 {owner} 的发布日期" in correction["correction"]
        assert f"核实 {'B' if owner == 'A' else 'A'}" not in correction["correction"]
    assert len(context.search_tasks) == 2 and len(context.search_calls) == 2
    assert all(task.check_status == "passed" for task in context.search_tasks.values())
    assert active_call.get() is None


@pytest.mark.asyncio
async def test_failed_review_quotes_stay_local_and_cannot_become_parent_facts(context):
    unsupported = "A 价格 999 元，来源 https://example.test/not-verified"
    model = ScriptedModel(
        responses=[
            rejected(
                finding(
                    "unsupported_claim",
                    requirement_quote="",
                    scope_quote="",
                    claim_quote=unsupported,
                )
            )
        ]
    )
    settings = ReviewConfig(enabled=True, model_check=True, max_refinements=0)
    spec = SubagentSpec("search", DISPLAY_NAMES["search"], "Find", "Find", (), review_config=settings)
    wrapper = DelegationRunnable(
        spec,
        RunnableLambda(lambda _: {"messages": [AIMessage(content=unsupported)]}),
        reviewer=ResultReviewer(settings, model),
    )
    result = await wrapper.ainvoke({"messages": [HumanMessage(content="核实 A 的价格")]})
    parent = result["messages"][-1].text
    assert "insufficient" in parent and json.loads(parent)["retry_allowed"] is False
    assert "999" not in parent and "not-verified" not in parent
    assert "检查说明不能作为事实依据" in parent


@pytest.mark.asyncio
async def test_checker_cannot_borrow_another_live_tasks_evidence(context):
    # Even when B is in the run catalog, A's reviewer gets/accepts only A's evidence.
    from agent.runtime.search import SearchAllowance

    other = SearchAllowance(context, "B")
    other.claim()
    other.task.evidence = [evidence("B")]
    response = rejected(
        finding(
            "contradicted_claim",
            requirement_quote="",
            scope_quote="A",
            claim_quote="A 价格 200 元",
            evidence_call_id="B",
            evidence_quote="B 官方价格 100 元",
        )
    )
    model = ScriptedModel(responses=[response, response])
    reviewer = ResultReviewer(ReviewConfig(enabled=True, model_check=True), model)
    token = active_call.set(HookCall("search", "A", args={"description": "核实 A 价格"}))
    try:
        result = await reviewer.review("wrapped", "A 价格 200 元", [evidence("A")])
    finally:
        active_call.reset(token)
    assert result.verdict == "check_incomplete" and "current_task_tool_record" in result.issues[0]
