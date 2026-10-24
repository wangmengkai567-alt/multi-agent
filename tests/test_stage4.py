"""Expert extension, actual tool routing and durable ownership; no live model service."""

import asyncio
import inspect
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from deepagents.middleware.subagents import SubAgentMiddleware
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from test_framework_contract import ScriptedModel, call

from agent.runtime.backend import GuardedFilesystemBackend, run_backend
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.events import EventSink
from agent.runtime.experts import ExpertRegistryMiddleware, SerialDelegationMiddleware, bind_registry
from agent.runtime.hooks import HookRejected, HookToolMiddleware, active_call
from agent.runtime.models import budget_model
from agent.runtime.policies import RuntimePolicy, load_runtime_policy
from agent.runtime.scheduler import QueueFull, child_owner
from agent.runtime.skills import ProjectSkillsMiddleware, SkillBackend, parse_metadata
from agent.subagent_factory import build_subagents
from agent.subagent_registry import (
    ExecutionConfig,
    ModelConfig,
    ResourceConfig,
    ReviewConfig,
    SubagentRegistry,
    SubagentSpec,
    SummaryConfig,
    load_default_registry,
)
from api.run_store import RunStore

EXECUTIONS = []


@tool
def execute_sql_query(query: str) -> str:
    """Read a bounded local configuration fixture through the real tool middleware."""
    EXECUTIONS.append((active_call.get().parent_call_id, query))
    return '{"concurrent_slots": 2}'


def spec(name="auditor", *, targets=(), group="queries", network=False):
    return SubagentSpec(
        name,
        "专家 " + name,
        "核查调度配置并返回可追溯事实",
        "只核查分工内的配置",
        () if targets else (execute_sql_query,),
        review_config=ReviewConfig(enabled=True, model_check=True, grounded_report=True, max_refinements=0),
        execution=ExecutionConfig(network_access=network),
        resources=ResourceConfig(group=group, concurrency=1, queue_capacity=8),
        delegate_targets=targets,
    )


class WorkerModel(ScriptedModel):
    nested_target: str | None = None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_messages.append(list(messages))
        response_format = kwargs.get("response_format", {}).get("json_schema", {})
        if response_format.get("name") == "search_report":
            data = json.loads(messages[-1].text)
            assert "tools" not in data and data["citation_options"]
            reference = data["citation_options"][0]
            assert reference["quote"] == '{"concurrent_slots": 2}'
            assert set(response_format["schema"]["$defs"]["QuoteReference"]["properties"]) == {"quote_id"}
            response = AIMessage(
                content=json.dumps(
                    dict(
                        facts=[
                            dict(
                                id="slots",
                                dimensions={
                                    k: None
                                    for k in (
                                        "model",
                                        "variant",
                                        "region",
                                        "channel",
                                        "window",
                                        "eligibility",
                                        "price_kind",
                                    )
                                },
                                subject="调度配置",
                                aspect="并发槽",
                                value="并发槽为2",
                                scope="当前配置",
                                evidence=[{"quote_id": reference["quote_id"]}],
                                identity_evidence=[],
                                as_of=None,
                                time_evidence=None,
                            )
                        ],
                        analysis=[],
                        gaps=[],
                    ),
                    ensure_ascii=False,
                )
            )
        elif not any(isinstance(m, ToolMessage) for m in messages):
            response = (
                call("task", {"subagent_type": self.nested_target, "description": "核实当前并发槽"}, "nested")
                if self.nested_target
                else call("execute_sql_query", {"query": "SELECT concurrent_slots FROM orders"}, "sql")
            )
        else:
            response = AIMessage(content="资料收集完成")
        from langchain_core.outputs import ChatGeneration, ChatResult

        return ChatResult(generations=[ChatGeneration(message=response)])


class ReviewModel(ScriptedModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_messages.append(list(messages))
        data = json.loads(messages[-1].text)
        report = data["candidate_report"]
        assert report["facts"][0]["evidence"][0]["quote"] == '{"concurrent_slots": 2}'
        result = dict(
            facts=[
                dict(
                    fact_id=f["id"],
                    verdict="passed",
                    reason="",
                    object_match=dict(object_id=None, relation="unresolved", reason="仅核查配置，不绑定产品对象"),
                )
                for f in report["facts"]
            ],
            analysis=[],
            coverage={
                rid: dict(
                    status="covered" if "covered" in v["statuses"] else "unknown",
                    fact_ids=v["fact_ids"] if "covered" in v["statuses"] else [],
                    claim_ids=[],
                )
                for rid, v in data["coverage_candidates"].items()
            },
            findings=[],
        )
        from langchain_core.outputs import ChatGeneration, ChatResult

        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=json.dumps(result, ensure_ascii=False)))]
        )


@pytest_asyncio.fixture
async def runtime(sandbox_dir, monkeypatch):
    EXECUTIONS.clear()
    for key, value in (
        ("MYSQL_READONLY_USER", "fixture_reader"),
        ("MYSQL_READONLY_PASSWORD", "fixture_password"),
        ("MYSQL_DATABASE", "fixture_database"),
    ):
        monkeypatch.setenv(key, value)
    data = load_runtime_policy().model_dump()
    data["sql"]["allowed_tables"] = ["orders"]
    data["scheduler"]["children"] = dict(concurrency=1, queue_capacity=8, requests_per_second=0.0)
    data["scheduler"]["models"]["requests_per_second"] = 0.0
    policy = RuntimePolicy.model_validate(data)
    store = await RunStore.open(sandbox_dir / "runs.db")
    sink = EventSink(store, lambda *args: None)
    context = RunContext(
        "stage4", "thread", sandbox_dir / "work", user_query="核实当前配置的并发槽", event_sink=sink, policy=policy
    )
    context.work_dir.mkdir()
    token = set_run_context(context)
    await sink.create(context.thread_id, context.user_query, context.run_id)
    await sink.start(context)
    try:
        yield context, store, sink
    finally:
        reset_run_context(token)
        await sink.close()
        await store.close()


def graph(registry, worker, review, target):
    parent = ScriptedModel(
        responses=[
            call("task", dict(subagent_type=target, description="核实当前并发槽"), "request"),
            AIMessage(content="流程完成"),
        ]
    )
    children = build_subagents(registry, {"SUB": worker, "REVIEW": review})
    return create_agent(
        model=budget_model(parent, "main_model"),
        middleware=[
            ExpertRegistryMiddleware(registry),
            SubAgentMiddleware(backend=run_backend, subagents=children),
            HookToolMiddleware(allowed_targets=registry.specs, managed_delegation=True),
        ],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("sync", [False, True])
async def test_third_expert_real_task_tool_review_and_durable_result(runtime, sync):
    context, store, sink = runtime
    registry = SubagentRegistry([spec("alpha"), spec("beta"), spec("auditor")])
    worker, review = WorkerModel(responses=[]), ReviewModel(responses=[])
    agent = graph(registry, worker, review, "auditor")
    result = (
        await asyncio.to_thread(agent.invoke, {"messages": [HumanMessage(content=context.user_query)]})
        if sync
        else await agent.ainvoke({"messages": [HumanMessage(content=context.user_query)]})
    )
    await sink.flush()
    rows = await store.list_results(context.run_id, thread_id=context.thread_id)
    assert len(EXECUTIONS) == 1 and len(rows) == 1 and rows[0]["source"] == "auditor"
    assert rows[0]["published"] and rows[0]["judged"]
    tasks = await store.list_subtasks(context.run_id)
    assert tasks[0]["target"] == "auditor" and rows[0]["call_id"] == tasks[0]["call_id"]
    returned = next(m for m in result["messages"] if isinstance(m, ToolMessage))
    assert json.loads(returned.text)["verified_report"]["facts"][0]["value"] == "并发槽为2"
    assert context.call_counts["sub_model"] == 3 and context.call_counts["review"] == 1
    assert child_owner.get() is None
    assert all(v["active"] == v["queued"] == 0 for v in context.scheduler.snapshot().values())


@pytest.mark.asyncio
@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("same_group", [False, True])
async def test_allowed_nested_experts_finish_with_one_service_slot_and_relay_real_evidence(runtime, sync, same_group):
    context, store, sink = runtime
    parent = spec("coordinator", targets=("auditor",), group="parent")
    child = spec("auditor", group="parent" if same_group else "child")
    registry = SubagentRegistry([parent, child])
    # Only the parent delegates. The worker identifies its active expert at call time.
    original = WorkerModel._generate

    class NestedWorker(WorkerModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.nested_target = "auditor" if active_call.get().target == "coordinator" else None
            return original(self, messages, stop, run_manager, **kwargs)

    agent = graph(registry, NestedWorker(responses=[]), ReviewModel(responses=[]), "coordinator")
    invocation = (
        asyncio.to_thread(agent.invoke, {"messages": [HumanMessage(content=context.user_query)]})
        if sync
        else agent.ainvoke({"messages": [HumanMessage(content=context.user_query)]})
    )
    await asyncio.wait_for(invocation, timeout=15)
    await sink.flush()
    rows = await store.list_results(context.run_id, thread_id=context.thread_id)
    assert {r["source"] for r in rows} == {"auditor", "coordinator"}
    assert all(r["published"] for r in rows) and len(EXECUTIONS) == 1
    assert len(context.quote_registry) == 2
    assert len({q["task_id"] for q in context.quote_registry.values()}) == 2
    assert all(v["active"] == v["queued"] == 0 for v in context.scheduler.snapshot().values())


@pytest.mark.parametrize(
    "change",
    [
        dict(agent_id="main"),
        dict(agent_id="general-purpose"),
        dict(agent_id="../escape"),
        dict(review_config=None),
        dict(review_config=ReviewConfig(enabled=True, model_check=False)),
        dict(delegate_targets=("missing",)),
        dict(delegate_targets=("auditor",)),
        dict(tools=(execute_sql_query, execute_sql_query)),
        dict(model_config="SUB"),
        dict(execution="task"),
        dict(summary="automatic"),
    ],
)
def test_bad_expert_definitions_rejected_before_compilation(change):
    with pytest.raises(ValueError):
        SubagentRegistry([replace(spec(), **change)])


def test_registry_name_and_group_conflicts_and_immutable_mapping():
    first = spec("one")
    with pytest.raises(ValueError):
        SubagentRegistry([first, replace(spec("two"), display_name=first.display_name)])
    with pytest.raises(ValueError):
        SubagentRegistry([first, replace(spec("two"), resources=ResourceConfig(concurrency=2))])
    registry = SubagentRegistry([first])
    with pytest.raises(TypeError):
        registry.specs["two"] = spec("two")


def test_permission_escalation_and_cycles_rejected():
    with pytest.raises(ValueError, match="network"):
        SubagentRegistry([spec("parent", targets=("child",)), spec("child", network=True)])
    with pytest.raises(ValueError, match="Cyclic"):
        SubagentRegistry([spec("parent", targets=("child",)), spec("child", targets=("parent",))])


def test_missing_profiles_retries_and_declared_model_capabilities_rejected():
    registry = SubagentRegistry([spec()])
    with pytest.raises(ValueError, match="Missing"):
        build_subagents(registry, {})
    raw = WorkerModel(responses=[], profile={"tool_calling": False})
    with pytest.raises(ValueError, match="tool calling"):
        build_subagents(registry, {"SUB": raw, "REVIEW": ReviewModel(responses=[])})
    raw = WorkerModel(responses=[], profile={"structured_output": False})
    with pytest.raises(ValueError, match="structured"):
        build_subagents(registry, {"SUB": raw, "REVIEW": ReviewModel(responses=[])})
    with pytest.raises(ValueError):
        ModelConfig(max_retries=1)


def test_serial_delegation_rejects_parallel_business_actions():
    from langchain.agents.middleware import ModelResponse

    response = ModelResponse(
        result=[
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "one", "name": "task", "args": {}},
                    {"id": "two", "name": "execute_sql_query", "args": {}},
                ],
            )
        ]
    )
    with pytest.raises(ValueError, match="only tool"):
        SerialDelegationMiddleware.checked(response)


@pytest.mark.asyncio
async def test_unknown_task_target_cannot_execute(runtime):
    context, _, _ = runtime
    middleware = HookToolMiddleware("auditor")
    request = SimpleNamespace(
        tool_call=dict(name="task", id="escape", args=dict(subagent_type="other", description="run"))
    )
    with pytest.raises(HookRejected, match="not allowed"):
        middleware._before(request, middleware._call(request))
    assert not EXECUTIONS and sum(context.call_counts.values()) == 0


@pytest.mark.asyncio
async def test_execution_group_queue_rejects_without_leaking_slot(runtime):
    context, _, _ = runtime
    limits = ResourceConfig(group="bounded", concurrency=1, queue_capacity=0)
    gate = context.scheduler.expert_gate(limits)
    lease = await context.scheduler.aacquire(gate, context, "expert")
    try:
        with pytest.raises(QueueFull):
            await context.scheduler.aacquire(gate, context, "expert")
        assert gate.active == 1 and not gate.waiters
    finally:
        lease.release()
    assert gate.active == 0


@pytest.mark.asyncio
async def test_checkpoint_cannot_reuse_changed_expert_definition(runtime):
    context, _, _ = runtime
    bind_registry(context, SubagentRegistry([spec()]))
    from agent.runtime.context import RunStopped

    with pytest.raises(RunStopped):
        bind_registry(context, SubagentRegistry([replace(spec(), system_prompt="改变后的配置")]))
    assert context.stop_status == "interrupted"


def test_public_summary_constructor_and_private_dependency_scan():
    from deepagents.middleware.summarization import SummarizationMiddleware

    import agent.main_agent as root
    import agent.runtime.backend as backend
    import agent.runtime.skills as skills
    import agent.subagent_factory as factory

    summary = SummarizationMiddleware(model=WorkerModel(responses=[]), backend=run_backend, **SummaryConfig().kwargs())
    from langchain.agents.middleware import AgentMiddleware

    assert isinstance(summary, AgentMiddleware)
    assert SummaryConfig().kwargs()["trigger"] == ("tokens", 12000)
    for module in (root, skills, factory, backend):
        source = inspect.getsource(module)
        assert "import _compute_summarization_defaults" not in source and "import _parse_skill_metadata" not in source
        assert "middleware._utils" not in source and "super()._resolve_path" not in source


@pytest.mark.asyncio
async def test_version_seven_preserves_existing_results_and_current_owner(runtime, sandbox_dir):
    context, store, sink = runtime
    agent = graph(SubagentRegistry([spec()]), WorkerModel(responses=[]), ReviewModel(responses=[]), "auditor")
    await agent.ainvoke({"messages": [HumanMessage(content=context.user_query)]})
    await sink.flush()
    original = await store.list_results(context.run_id, thread_id=context.thread_id)
    async with store.connection.execute("PRAGMA user_version") as cursor:
        assert (await cursor.fetchone())[0] == 7
    await store.connection.execute("PRAGMA user_version=6")
    await store.connection.commit()
    # Reconstruct the previous enum in a separate populated DB, preserving every other column/index.
    backup = sandbox_dir / "v6.db"
    with sqlite3.connect(backup) as destination:
        await store.connection.backup(destination)
    with sqlite3.connect(backup) as db:
        sql = db.execute("SELECT sql FROM sqlite_schema WHERE name='results'").fetchone()[0]
        sql = sql.replace('CREATE TABLE "results"', "CREATE TABLE restored_results").replace(
            "source TEXT NOT NULL", "source TEXT NOT NULL CHECK(source IN ('db','knowledge','search'))", 1
        )
        db.execute("UPDATE subtasks SET target='db'")
        db.execute("UPDATE results SET source='db'")
        indexes = db.execute(
            "SELECT sql FROM sqlite_schema WHERE type='index' AND tbl_name='results' AND sql IS NOT NULL"
        ).fetchall()
        db.execute(sql)
        db.execute("INSERT INTO restored_results SELECT * FROM results")
        db.execute("DROP TABLE results")
        db.execute("ALTER TABLE restored_results RENAME TO results")
        for (statement,) in indexes:
            db.execute(statement)
        db.commit()
    reopened = await RunStore.open(backup)
    try:
        rows = await reopened.list_results(context.run_id, thread_id=context.thread_id)
        assert rows[0]["result_id"] == original[0]["result_id"] and rows[0]["published"]
        task = (await reopened.list_subtasks(context.run_id))[0]
        assert task["current_result_id"] == rows[0]["result_id"]
        async with reopened.connection.execute("PRAGMA foreign_key_check") as cursor:
            assert await cursor.fetchall() == []
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_public_backend_skill_scope_and_metadata_keep_body_private(runtime, sandbox_dir):
    context, _, _ = runtime
    context.skill_dir = Path(__file__).resolve().parents[1] / "skills"
    middleware = ProjectSkillsMiddleware(("research",))
    update = middleware.before_agent({}, None, {})
    assert update["skills_metadata"][0]["name"] == "research"
    backend = SkillBackend(context.skill_dir, ("research",))
    assert "research" in backend.read("/research/SKILL.md")
    from utils.secure_paths import PathDenied

    with pytest.raises(PathDenied):
        backend.read("/report-writing/SKILL.md")
    with pytest.raises(PathDenied):
        GuardedFilesystemBackend(context.work_dir).read("../outside.txt")
    assert backend.write("/research/SKILL.md", "overwrite").error
    with pytest.raises(ValueError):
        parse_metadata("---\\nname: wrong\\ndescription: x\\n---\\nbody", "research")


def test_default_definitions_are_explicit_and_lazy():
    registry = load_default_registry()
    assert set(registry.specs) == {"db", "search"}
    assert registry.get("search").execution.planned and registry.get("search").execution.network_access
    assert registry.get("db").execution.source_kind == "db" and not registry.get("db").execution.network_access
    assert all(s.review_config.enabled and not s.delegate_targets for s in registry.specs.values())


@pytest.mark.asyncio
async def test_registered_skill_body_and_real_resource_read_use_public_middleware(runtime):
    from deepagents.middleware.filesystem import FilesystemMiddleware

    from agent.runtime.skills import skill_scope

    context, store, sink = runtime
    context.skill_dir = Path(__file__).resolve().parents[1] / "skills"
    context.skill_ids = ("research",)
    body = (context.skill_dir / "research/SKILL.md").read_text("utf-8")
    marker = body.split("---", 2)[-1].strip().splitlines()[0]
    model = ScriptedModel(
        responses=[
            call("read_file", {"file_path": "/skills/research/references/source-review.md"}, "read-resource"),
            AIMessage(content="读取完成"),
        ]
    )
    agent = create_agent(
        model=model,
        middleware=[
            FilesystemMiddleware(backend=run_backend),
            ProjectSkillsMiddleware(("research",)),
            HookToolMiddleware("auditor"),
        ],
    )
    token = skill_scope.set(("auditor", ("research",)))
    try:
        result = await agent.ainvoke({"messages": [HumanMessage(content="读取研究引用约束")]})
    finally:
        skill_scope.reset(token)
    await sink.flush()
    assert marker in str(model.seen_messages[0])
    assert any(isinstance(m, ToolMessage) and "来源" in m.text for m in result["messages"])
    events = [e for e in await store.list_events(context.run_id) if e["event"] == "skill_state"]
    assert {e["status"] for e in events} == {"selected", "loaded", "resource_loaded"}
    assert all(e["actor"] == "auditor" for e in events)


@pytest.mark.asyncio
async def test_public_summary_execution_is_metered_as_summarization(runtime):
    from deepagents.middleware.summarization import SummarizationMiddleware

    context, _, _ = runtime
    raw = ScriptedModel(responses=[AIMessage(content="保留的调度上下文"), AIMessage(content="完成")])
    model = budget_model(raw, "main_model")
    agent = create_agent(
        model=model,
        middleware=[
            SummarizationMiddleware(
                model=model,
                backend=run_backend,
                trigger=("tokens", 10),
                keep=("messages", 1),
                trim_tokens_to_summarize=None,
            )
        ],
    )
    await agent.ainvoke(
        {
            "messages": [
                HumanMessage(content="调度说明" * 100),
                AIMessage(content="已有记录" * 100),
                HumanMessage(content="继续核查"),
            ]
        }
    )
    phases = [r["phase"] for r in context.request_ledger.values()]
    assert phases.count("summarization") == 1 and context.call_counts["main_model"] == 2
    assert len(raw.seen_messages) == 2


def test_production_root_builds_without_private_summary_defaults(monkeypatch):
    import deepagents.middleware.summarization as library

    from agent import llm, main_agent

    requested = []

    def create(role):
        requested.append(role)
        return ScriptedModel(responses=[])

    def forbidden(*args, **kwargs):
        raise AssertionError("Private summary defaults were called")

    monkeypatch.setattr(llm, "create_role_model", create)
    monkeypatch.setattr(library, "_compute_summarization_defaults", forbidden)
    compiled = main_agent._build_agent(None)
    assert set(requested) == {"MAIN", "SUB", "REVIEW"}
    assert compiled.get_graph().nodes


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_cancelled_expert_releases_branch_and_group_without_publishing(runtime, nested):
    context, store, sink = runtime
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockedWorker(WorkerModel):
        started: object
        blocker: object

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            if active_call.get().target == "auditor":
                self.started.set()
                await self.blocker.wait()
            self.nested_target = "auditor" if active_call.get().target == "coordinator" else None
            return super()._generate(messages, stop, run_manager, **kwargs)

    definitions = [spec("auditor")]
    if nested:
        definitions.append(spec("coordinator", targets=("auditor",)))
    agent = graph(
        SubagentRegistry(definitions),
        BlockedWorker(responses=[], started=entered, blocker=release),
        ReviewModel(responses=[]),
        "coordinator" if nested else "auditor",
    )
    task = asyncio.create_task(agent.ainvoke({"messages": [HumanMessage(content=context.user_query)]}))
    await asyncio.wait_for(entered.wait(), timeout=5)
    context.stop("cancelled")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await sink.flush()
    assert not EXECUTIONS
    assert await store.list_results(context.run_id, thread_id=context.thread_id) == []
    assert all(t["status"] == "cancelled" for t in await store.list_subtasks(context.run_id))
    assert all(v["active"] == v["queued"] == 0 for v in context.scheduler.snapshot().values())
    assert child_owner.get() is None
