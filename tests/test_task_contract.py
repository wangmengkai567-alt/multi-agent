"""Phase one boundaries: real orchestration and typed delivery, not model trivia."""

import json
from types import SimpleNamespace

import pytest
from deepagents import create_deep_agent
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError
from test_framework_contract import ScriptedModel

from agent.runtime.answer_review import AnswerReviewMiddleware
from agent.runtime.checkpoints import restore_job_checkpoint, save_job_checkpoint
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.contracts import RouteDecision, TaskContractMiddleware, assert_contract, bind_contract
from agent.runtime.delivery import DirectDraft, parse_delivery
from agent.runtime.finalization import SearchFinalizationMiddleware
from agent.runtime.hooks import HookRejected, HookToolMiddleware
from agent.runtime.models import budget_model
from agent.runtime.policies import load_runtime_policy

RESEARCH = "请查看 Deep Agents、Open Deep Research 和 smolagents 的源码，分析它们如何进行子 agent 协作与工具调用，并结合我的项目提出改进建议。关键结论需要提供源码依据。"
REWRITE = "不再联网，把上一轮的调研结果整理成一份实施计划，包含修改位置、依赖关系和验收标准。不要补充未经核实的新结论。"
SOURCE = "已查证：Deep Agents 通过 task 委派子任务。[源码](https://github.com/langchain-ai/deepagents)。当前项目需要统一任务交付协议；尚未完成修复。"


@pytest.fixture
def context(sandbox_dir):
    context = RunContext(
        "phase-one",
        "same-session",
        sandbox_dir,
        user_query=REWRITE,
        previous_run_id="reviewed-run",
        previous_answer=SOURCE,
        policy=load_runtime_policy(),
        enforce_plan=True,
    )
    marker = set_run_context(context)
    try:
        yield context
    finally:
        reset_run_context(marker)


def lock(context, purpose="rewrite", reuse=True):
    return bind_contract(context, RouteDecision(purpose=purpose, reuse_previous=reuse))


def test_contract_binds_original_task_and_reviewed_source_and_cannot_be_replaced(context):
    contract = lock(context)
    assert contract.source_text == SOURCE and not contract.network_allowed
    with pytest.raises(ValidationError):
        contract.delivery_kind = "grounded"
    with pytest.raises(ValueError, match="already locked"):
        lock(context)
    context.user_query = RESEARCH
    with pytest.raises(ValueError, match="scope"):
        assert_contract(context)


def test_source_mutation_and_foreign_source_identity_are_rejected(context):
    lock(context)
    context.previous_answer = "another run's unchecked draft"
    with pytest.raises(ValueError, match="identity"):
        assert_contract(context)


@pytest.mark.parametrize("purpose", ["conversation", "calculation", "rewrite", "explanation"])
def test_code_evidence_request_cannot_be_routed_around_review(context, purpose):
    context.user_query = RESEARCH
    with pytest.raises(ValueError, match="evidence requirements"):
        lock(context, purpose, False)
    assert context.task_contract is None


def test_rewrite_cannot_invent_a_previous_source(context):
    context.previous_run_id, context.previous_answer = None, ""
    with pytest.raises(ValueError, match="previous delivery"):
        lock(context)


def test_explicit_data_requirements_cannot_be_downgraded(context):
    context.required_sources = ("db",)
    with pytest.raises(ValueError, match="evidence requirements"):
        lock(context)


def test_direct_schema_does_not_accept_grounded_or_arbitrary_extra_fields(context):
    lock(context)
    assert parse_delivery(DirectDraft(answer="实施建议：先统一契约，再验收。").model_dump_json(), context)
    with pytest.raises(ValidationError):
        parse_delivery('{"answer":"计划","blocks":[]}', context)


def test_direct_contract_blocks_invented_tool_calls_before_execution(context):
    lock(context)
    executed = []
    request = SimpleNamespace(
        tool_call={"name": "task", "id": "attempt", "args": {"subagent_type": "search", "description": "research"}}
    )
    with pytest.raises(HookRejected, match="禁止业务工具"):
        HookToolMiddleware()._before(request, HookToolMiddleware()._call(request))
    assert executed == []


def test_research_without_network_permission_cannot_call_search(context):
    context.user_query = "不再联网，仅查询数据库"
    context.required_sources = ("db",)
    lock(context, "database", False)
    request = SimpleNamespace(
        tool_call={"name": "task", "id": "attempt", "args": {"subagent_type": "search", "description": "research"}}
    )
    with pytest.raises(HookRejected, match="禁止联网"):
        HookToolMiddleware()._before(request, HookToolMiddleware()._call(request))


def test_checkpoint_restores_the_same_contract_and_source(context):
    lock(context)
    context.job_checkpoint_path = context.work_dir / "checkpoint.json"
    save_job_checkpoint(context)
    restored = RunContext(
        "resumed",
        context.thread_id,
        context.work_dir,
        user_query=REWRITE,
        policy=context.policy,
        research_scope_id=context.research_scope_id,
        job_checkpoint_path=context.job_checkpoint_path,
    )
    restore_job_checkpoint(restored)
    assert restored.task_contract == context.task_contract
    assert restored.previous_answer == SOURCE
    assert assert_contract(restored) == context.task_contract


@pytest.mark.asyncio
@pytest.mark.parametrize("sync", [False, True])
async def test_real_graph_rewrites_bound_delivery_without_research_or_fact_pool(context, sync):
    import asyncio

    answer = "实施计划：建议修改 agent/runtime/delivery.py 统一交付契约；验收应检查证据传递。修复尚未完成。"
    parent = ScriptedModel(
        responses=[
            AIMessage(content='{"purpose":"rewrite","reuse_previous":true}'),
            AIMessage(content=DirectDraft(answer=answer).model_dump_json()),
        ]
    )
    reviewer = ScriptedModel(responses=[AIMessage(content='{"verdict":"passed","issues":[]}')])
    graph = create_deep_agent(
        model=budget_model(parent, "main_model"),
        middleware=[
            TaskContractMiddleware(),
            AnswerReviewMiddleware(reviewer),
            SearchFinalizationMiddleware(),
            HookToolMiddleware(),
        ],
    )
    invoke = {"messages": [HumanMessage(content=REWRITE)]}
    result = await asyncio.to_thread(graph.invoke, invoke) if sync else await graph.ainvoke(invoke)
    assert result["messages"][-1].text == answer
    assert len(parent.seen_messages) == 2 and len(reviewer.seen_messages) == 1
    assert context.search_calls == set() and not context.search_tasks
    assert context.coverage == "complete"
    assert SOURCE in parent.seen_messages[-1][-1].text
    assert context.call_counts == {"main_model": 2, "sub_model": 0, "review": 1, "tool": 0, "retry": 0}

    from api.run_store import RunStore

    store = await RunStore.open(context.work_dir / "runs.db")
    try:
        await store.create(context.thread_id, REWRITE, run_id=context.run_id)
        assert await store.finish(context.run_id, "completed", answer=answer, coverage=context.coverage)
        saved = await store.get(context.run_id)
        assert saved["status"] == "completed" and saved["coverage"] == "complete"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_invented_claim_is_revised_once_and_still_rejected(context):
    parent = ScriptedModel(
        responses=[
            AIMessage(content='{"purpose":"rewrite","reuse_previous":true}'),
            AIMessage(content='{"answer":"项目已实现完整任务契约。"}'),
            AIMessage(content='{"answer":"项目已实现完整任务契约。"}'),
        ]
    )
    reviewer = ScriptedModel(
        responses=[
            AIMessage(content='{"verdict":"insufficient","issues":["原始材料明确尚未完成修复，不能写成已实现。"]}')
        ]
        * 2
    )
    graph = create_deep_agent(
        model=budget_model(parent, "main_model"),
        middleware=[
            TaskContractMiddleware(),
            AnswerReviewMiddleware(reviewer),
            SearchFinalizationMiddleware(),
        ],
    )
    with pytest.raises(RuntimeError, match="未满足"):
        await graph.ainvoke({"messages": [HumanMessage(content=REWRITE)]})
    assert context.call_counts["retry"] == 1 and context.coverage == "unknown"
    assert len(reviewer.seen_messages) == 2
    repaired = json.loads(parent.seen_messages[-1][-1].text)
    assert "尚未完成" in repaired["revision"]
    assert repaired["untrusted_previous_draft"] == '{"answer":"项目已实现完整任务契约。"}'


def test_direct_finalization_has_no_tools_and_only_the_authoritative_source(context):
    lock(context)
    request = ModelRequest(
        model=ScriptedModel(responses=[]),
        messages=[HumanMessage(content="untrusted old turn")],
        tools=[],
        model_settings={"strict": True},
    )
    final = SearchFinalizationMiddleware().final_request(request)
    assert final.tools == [] and final.tool_choice == "none"
    assert "strict" not in final.model_settings
    payload = json.loads(final.messages[0].text)
    assert payload["task_contract"]["source_text"] == SOURCE
    assert "untrusted old turn" not in str(payload)


def test_planning_schema_does_not_invent_an_official_source_obligation(context):
    from agent.runtime.task_plan import plan_research

    context.user_query = RESEARCH
    lock(context, "research", False)
    original = plan_research.args_schema.model_json_schema()
    request = ModelRequest(model=ScriptedModel(responses=[]), messages=[], tools=[plan_research])
    constrained = TaskContractMiddleware()._business_request(request)
    schema = constrained.tools[0].args_schema.model_json_schema()
    assert "required_tier" not in schema["$defs"]["PlanRequirement"]["properties"]
    assert context.task_contract.required_source_tiers == ()
    assert plan_research.args_schema.model_json_schema() == original
