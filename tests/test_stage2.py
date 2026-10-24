"""Stage-two contracts: real lifecycle/transport boundaries, not model knowledge."""

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from test_run_concurrency import complete, server, service_runtime, wait_started

from agent.runtime.checkpoints import restore_job_checkpoint, save_job_checkpoint
from agent.runtime.context import RunContext, RunStopped, reset_run_context, set_run_context
from agent.runtime.hooks import HookRejected
from agent.runtime.models import budget_model
from agent.runtime.policies import load_runtime_policy
from agent.runtime.reservations import resource_available
from agent.runtime.review_state import ReviewState
from agent.runtime.scheduler import ServiceScheduler
from agent.runtime.search import SearchAllowance, SearchTaskBudget
from agent.runtime.source_policy import PublisherIdentity, SourcePolicy, page_tier, source_assessment
from agent.runtime.task_plan import PlanRequirement, ResearchScope, plan_research, resolve_task

QUERY = "比较 Deep Agents、Open Deep Research、smolagents 和 GPT Researcher 的工具调用，提出建议"


def runtime_policy(**pools):
    policy = load_runtime_policy()
    scheduler = policy.scheduler.model_copy(
        update={name: getattr(policy.scheduler, name).model_copy(update=value) for name, value in pools.items()}
    )
    return policy.model_copy(update={"scheduler": scheduler})


@pytest.fixture
def context(sandbox_dir):
    policy = runtime_policy(models={"requests_per_second": 0.0}, search={"requests_per_second": 0.0})
    context = RunContext("stage-two", "thread", sandbox_dir, policy=policy, user_query=QUERY, enforce_plan=True)
    token = set_run_context(context)
    try:
        yield context
    finally:
        reset_run_context(token)


def scope(name, **extra):
    return ResearchScope(
        object_name=name,
        label=name,
        description=name + " 工具调用源码",
        requirements=[PlanRequirement(anchor="工具调用", label="工具调用", kind="fact")],
        investigations=[],
        **extra,
    )


def initialize(context, names=("Deep Agents", "Open Deep Research", "smolagents", "GPT Researcher")):
    value = plan_research.func(
        tasks=[scope(name) for name in names],
        main_requirements=[PlanRequirement(anchor="提出建议", label="提出建议", kind="recommendation")],
    )
    return json.loads(value)


def passed():
    return ReviewState(judged=True, facts_status="all_passed", coverage_status="covered", check_status="passed")


def test_four_objects_and_append_preserve_requirement_ids_and_budget(context):
    plan = initialize(context)
    assert len(plan["tasks"]) == 4 and context.policy.search.max_concurrent_tasks == 2
    original = copy.deepcopy(context.task_scopes)
    required = {key: row for key, row in context.requirements.items() if row["required"]}
    task = plan["tasks"][0]
    old = SearchTaskBudget(closed_reason="finished", state=passed(), calls={"attempt-0"})
    context.search_tasks[task["task_id"]] = old
    context.call_counts["tool"] = 1
    addition = scope("Deep Agents", object_id=task["object_id"], depends_on=[task["task_id"]])
    addition.requirements = []
    addition.description = "核实真实源码中发现的取消路径"
    updated = json.loads(
        plan_research.func(tasks=[addition], main_requirements=[], operation="append", expected_version=plan["version"])
    )
    new = updated["tasks"][-1]
    assert updated["version"] == 2 and len(updated["tasks"]) == 5
    assert all(context.task_scopes[key] == row for key, row in original.items())
    assert required == {key: row for key, row in context.requirements.items() if row["required"]}
    assert context.search_tasks[task["task_id"]] is old and context.call_counts["tool"] == 1
    allowance = SearchAllowance(context, new["task_id"])
    allowance.task.calls.add("attempt-1")
    assert allowance.used("search") == 2
    assert not resource_available(context, allowance, "search")
    with pytest.raises(HookRejected, match="already registered"):
        plan_research.func(tasks=[addition], main_requirements=[], operation="append", expected_version=2)


def test_append_version_conflicts_and_scope_expansion_are_atomic(context):
    plan = initialize(context, ("Deep Agents",))
    before = copy.deepcopy((context.objects, context.requirements, context.task_scopes))
    addition = scope("Deep Agents", object_id=plan["tasks"][0]["object_id"])
    addition.requirements = []
    addition.description = "额外源码调查"
    with pytest.raises(HookRejected, match="version"):
        plan_research.func(tasks=[addition], main_requirements=[], operation="append", expected_version=0)
    bad = scope("Fabricated Entity")
    bad.requirements = []
    with pytest.raises(HookRejected, match="approved fact"):
        plan_research.func(tasks=[addition, bad], main_requirements=[], operation="append", expected_version=1)
    assert (context.objects, context.requirements, context.task_scopes) == before
    assert context.plan_version == 1 and context.plan_expansions == 0


def test_discovered_objects_require_verified_quotes_and_inherit_budget(context, monkeypatch):
    plan = initialize(context, ("Deep Agents",))
    obj = plan["tasks"][0]["object_id"]
    proof = {
        "id": "verified-fact",
        "object_id": obj,
        "evidence": [{"quote": "Deep Agents delegates to Specialized Worker"}],
        "identity_evidence": [],
    }
    monkeypatch.setattr("agent.runtime.delivery.fact_pool", lambda ctx: {"facts": {"verified-fact": proof}})
    addition = scope("Specialized Worker", discovery_fact_id="verified-fact")
    addition.requirements = []
    updated = json.loads(
        plan_research.func(tasks=[addition], main_requirements=[], operation="append", expected_version=1)
    )
    discovered = updated["tasks"][-1]
    assert context.objects[discovered["object_id"]]["budget_owner_id"] == obj
    assert not any(r["required"] for r in context.requirements.values() if r["task_id"] == discovered["task_id"])


def test_dependencies_consume_no_slots_and_checkpoint_retains_pending_plan(context):
    plan = initialize(context, ("Deep Agents",))
    parent = plan["tasks"][0]
    addition = scope("Deep Agents", object_id=parent["object_id"], depends_on=[parent["task_id"]])
    addition.requirements = []
    addition.description = "依据父任务的事实核查异常路径"
    updated = json.loads(
        plan_research.func(tasks=[addition], main_requirements=[], operation="append", expected_version=1)
    )
    child = updated["tasks"][-1]
    assert child["status"] == "waiting"
    with pytest.raises(HookRejected, match="dependencies"):
        resolve_task(context, json.dumps({"task_id": child["task_id"]}))
    assert context.scheduler.snapshot()["children"] == {"active": 0, "queued": 0}
    context.job_checkpoint_path = context.work_dir / "checkpoint.json"
    save_job_checkpoint(context)
    restored = RunContext(
        "new-execution",
        context.thread_id,
        context.work_dir,
        policy=context.policy,
        user_query=QUERY,
        research_scope_id=context.research_scope_id,
        job_checkpoint_path=context.job_checkpoint_path,
    )
    restore_job_checkpoint(restored)
    assert restored.plan_version == 2 and restored.task_scopes == context.task_scopes
    context.search_tasks[parent["task_id"]] = SearchTaskBudget(closed_reason="finished", state=passed())
    assert resolve_task(context, json.dumps({"task_id": child["task_id"]}))[0] == child["task_id"]


def test_plan_seal_and_expansion_limit_are_explicit(context):
    plan = initialize(context, ("Deep Agents",))
    with pytest.raises(HookRejected, match="pending"):
        plan_research.func(tasks=[], main_requirements=[], operation="seal", expected_version=1)
    context.search_tasks[plan["tasks"][0]["task_id"]] = SearchTaskBudget(closed_reason="finished", state=passed())
    closed = json.loads(plan_research.func(tasks=[], main_requirements=[], operation="seal", expected_version=1))
    assert closed["closed"] and context.plan_version == 2
    with pytest.raises(HookRejected, match="closed"):
        plan_research.func(tasks=[], main_requirements=[], operation="append", expected_version=2)


@pytest.mark.parametrize(
    "url",
    [
        "https://docs.vendor.example/manual",
        "https://vendor.example/",
    ],
)
def test_publisher_mapping_is_configurable_and_auditable(context, url):
    rule = PublisherIdentity(
        publisher="Vendor",
        host="vendor.example",
        include_subdomains=True,
        tier="official",
        verification="Operator-confirmed publisher site",
    )
    context.policy = context.policy.model_copy(update={"sources": SourcePolicy(identities=[rule])})
    value = page_tier({"url": url, "content": "source"})
    assert value["tier"] == "official" and value["publisher"] == "Vendor"
    assert value["identity_basis"] == rule.verification


@pytest.mark.parametrize(
    "url",
    [
        "https://python.org.evil.example/",
        "https://evilpython.org/",
        "https://github.com/attacker/deepagents",
        "https://github.com/langchain-ai/deepagents-malicious",
    ],
)
def test_lookalike_hosts_and_repository_paths_are_not_official(context, url):
    assert page_tier({"url": url, "publisher": "official", "content": "source"})["tier"] == "unknown"


def test_unknown_identity_is_distinct_from_a_confirmed_disallowed_source(context):
    context.policy = context.policy.model_copy(
        update={
            "sources": SourcePolicy(
                identities=[
                    PublisherIdentity(
                        publisher="News", host="news.example", tier="media", verification="confirmed newsroom"
                    )
                ]
            )
        }
    )
    req = {"task_id": "task", "required_tier": ["official"]}
    assert (
        source_assessment(req, {"url": "https://unknown.example/"}, context)["constraint_status"] == "unknown_identity"
    )
    assert source_assessment(req, {"url": "https://news.example/"}, context)["constraint_status"] == "disallowed_tier"


@pytest.mark.asyncio
async def test_shared_account_models_queue_and_cancel_without_budget_or_late_execution(context):
    policy = runtime_policy(models={"concurrency": 1, "requests_per_second": 0.0})
    context.scheduler = ServiceScheduler(policy.scheduler)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def request(messages, **kwargs):
        calls.append(messages[0].text)
        entered.set()
        await release.wait()
        return AIMessage(content="controlled delivery")

    raw = RunnableLambda(request)
    first = asyncio.create_task(budget_model(raw, "main_model").ainvoke("first"))
    await asyncio.wait_for(entered.wait(), 2)
    other = RunContext("other", "other-thread", context.work_dir, policy=policy, scheduler=context.scheduler)
    token = set_run_context(other)
    try:
        waiting = asyncio.create_task(budget_model(raw, "review").ainvoke("cancelled"))
    finally:
        reset_run_context(token)
    await asyncio.sleep(0.05)
    assert context.scheduler.snapshot()["models"] == {"active": 1, "queued": 1}
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    release.set()
    await first
    assert calls == ["first"] and other.call_counts["review"] == 0
    assert context.scheduler.snapshot()["models"] == {"active": 0, "queued": 0}


@pytest.mark.asyncio
async def test_queued_expiry_does_not_execute_or_leak_a_slot(context):
    policy = runtime_policy(models={"concurrency": 1, "requests_per_second": 0.0})
    scheduler = ServiceScheduler(policy.scheduler)
    gate = scheduler.gate("models", "account")
    lease = await scheduler.aacquire(gate, context, "models")
    expired = RunContext(
        "expiry",
        "t",
        context.work_dir,
        policy=policy,
        deadline=datetime.now(UTC) + timedelta(seconds=0.05),
        scheduler=scheduler,
    )
    with pytest.raises(RunStopped):
        await scheduler.aacquire(gate, expired, "models")
    assert not gate.waiters and gate.active == 1
    lease.release()
    assert gate.active == 0


@pytest.mark.asyncio
async def test_http_queue_is_observable_bounded_and_cancellation_prevents_execution(sandbox_dir, monkeypatch):
    policy = runtime_policy(runs={"concurrency": 1, "queue_capacity": 1})
    monkeypatch.setattr(server, "load_runtime_policy", lambda: policy)
    async with service_runtime(sandbox_dir, monkeypatch) as (client, agent, _):
        first = (await client.post("/api/task", json={"query": "source research", "thread_id": "one"})).json()
        await wait_started(agent)
        second = (await client.post("/api/task", json={"query": "source research", "thread_id": "two"})).json()
        assert (await client.get(f"/api/runs/{second['run_id']}")).json()["status"] == "queued"
        rejected = await client.post("/api/task", json={"query": "source research", "thread_id": "three"})
        assert rejected.status_code == 503
        cancelled = await client.post(f"/api/runs/{second['run_id']}/cancel")
        assert cancelled.json()["status"] == "cancelled"
        await complete(agent, first["run_id"])
        await asyncio.sleep(0.05)
        assert second["run_id"] not in agent.contexts


@pytest.mark.asyncio
async def test_foreground_and_durable_job_share_the_same_service_slot(sandbox_dir, monkeypatch):
    policy = runtime_policy(runs={"concurrency": 1, "queue_capacity": 4})
    monkeypatch.setattr(server, "load_runtime_policy", lambda: policy)
    async with service_runtime(sandbox_dir, monkeypatch) as (client, agent, _):
        first = (await client.post("/api/task", json={"query": "source research", "thread_id": "front"})).json()
        await wait_started(agent)
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(job):
            entered.set()
            async with server.app.state.scheduler.run_slot(
                RunContext("nested", "job", sandbox_dir, policy=policy, scheduler=server.app.state.scheduler)
            ):
                assert server.app.state.scheduler.snapshot()["runs"]["active"] == 1
                await release.wait()
            return {"reviewed": True}

        service = server.app.state.job_service
        service.handlers["controlled"] = handler
        job = await service.submit("job", "job-run", "controlled", {}, "job-request", 20)
        await asyncio.sleep(0.05)
        assert not entered.is_set()
        await complete(agent, first["run_id"])
        await asyncio.wait_for(entered.wait(), 2)
        assert server.app.state.scheduler.snapshot()["runs"]["active"] == 1
        release.set()
        await asyncio.wait_for(service.tasks[job["task_id"]], 2)
        assert (await service.get(job["task_id"], "job"))["status"] == "completed"


@pytest.mark.asyncio
async def test_four_real_delegations_queue_without_dropping_results(context):
    from langchain_core.messages import HumanMessage

    from agent.runtime.delegation import DelegationRunnable
    from agent.runtime.hooks import active_call
    from agent.subagent_registry import SubagentSpec

    plan = initialize(context)
    active = peak = 0
    started = []
    first_batch = asyncio.Event()
    release = asyncio.Event()

    async def execute(inputs):
        nonlocal active, peak
        key = active_call.get().search_task_key
        active += 1
        peak = max(peak, active)
        started.append(key)
        if len(started) == 2:
            first_batch.set()
        try:
            await release.wait()
            return {"messages": [AIMessage(content="controlled child result")]}
        finally:
            active -= 1

    spec = SubagentSpec("search", DEFAULT_NAMES["search"], "Controlled executor", "Return execution marker", ())
    worker = DelegationRunnable(spec, RunnableLambda(execute))
    tasks = [
        asyncio.create_task(
            worker.ainvoke({"messages": [HumanMessage(content=json.dumps({"task_id": task["task_id"]}))]})
        )
        for task in plan["tasks"]
    ]
    await asyncio.wait_for(first_batch.wait(), 2)
    assert len(started) == 2 and len(tasks) == 4
    release.set()
    results = await asyncio.gather(*tasks)
    assert len(results) == 4 and len(set(started)) == 4 and peak == 2
    assert context.scheduler.snapshot()["children"] == {"active": 0, "queued": 0}


def test_search_scope_reaches_provider_and_rejects_outside_results_without_losing_raw_evidence(context, monkeypatch):
    import requests
    from requests.adapters import HTTPAdapter

    from agent.runtime.hooks import HookCall, active_call
    from agent.runtime.research import _perform, audit_transport
    from agent.runtime.search import search_allowance

    context.user_query += "；仅搜索 vendor.example"
    item = scope("Deep Agents", search_domains=["vendor.example"])
    plan = json.loads(
        plan_research.func(
            tasks=[item], main_requirements=[PlanRequirement(anchor="提出建议", label="建议", kind="recommendation")]
        )
    )
    task_id = plan["tasks"][0]["task_id"]
    allowance = SearchAllowance(context, task_id)
    gap_id = next(gid for gid, g in context.gaps.items() if g["task_id"] == task_id)
    captured = []

    def transport(adapter, request, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response._content = b"{}"
        return response

    def provider(**params):
        captured.append(params)
        with requests.Session() as session:
            audit_transport(SimpleNamespace(session=session))
            session.get("https://api.tavily.com/search")
        return "", {
            "results": [
                {"url": "https://docs.vendor.example/source", "content": "actual approved scope"},
                {"url": "https://vendor.example.evil.test/", "content": "outside source text"},
                {"url": "https://[malformed", "content": "invalid source"},
            ]
        }

    monkeypatch.setattr(HTTPAdapter, "send", transport)
    monkeypatch.setattr("tools.tavily_tool.internet_search.func", provider)
    call_token = active_call.set(HookCall("search:research_step", "query", args={}, search_task_key=task_id))
    search_token = search_allowance.set(allowance)
    try:
        response = json.loads(_perform("search", gap_id, "tools", "", "required fact"))
    finally:
        active_call.reset(call_token)
        search_allowance.reset(search_token)
    assert captured[0]["include_domains"] == ["vendor.example"]
    assert [row["url"] for row in response["results"]] == ["https://docs.vendor.example/source"]
    raw = allowance.task.evidence[0]["content"]
    assert raw["scope_rejected_results"][0]["content"] == "outside source text"
    assert len(context.outbound) == 1 and context.call_counts["tool"] == 1


@pytest.mark.asyncio
async def test_job_submitted_from_running_context_does_not_inherit_its_execution_slot(sandbox_dir):
    from agent.runtime.jobs import JobService

    policy = runtime_policy(runs={"concurrency": 1, "queue_capacity": 2})
    scheduler = ServiceScheduler(policy.scheduler)
    parent = RunContext("parent", "t", sandbox_dir, policy=policy, scheduler=scheduler)
    entered = asyncio.Event()

    async def handler(job):
        entered.set()
        return {"done": True}

    service = await JobService.open(sandbox_dir / "jobs.db", {"probe": handler}, sandbox_dir, scheduler=scheduler)
    try:
        async with scheduler.run_slot(parent):
            job = await service.submit("t", "r", "probe", {}, "request", 10)
            await asyncio.sleep(0.05)
            assert not entered.is_set()
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(service.tasks[job["task_id"]], 2)
    finally:
        await service.close()


def test_changed_source_identity_policy_cannot_reuse_a_checkpoint(context):
    initialize(context, ("Deep Agents",))
    context.job_checkpoint_path = context.work_dir / "checkpoint.json"
    save_job_checkpoint(context)
    raw = context.job_checkpoint_path.read_bytes()
    changed = context.policy.model_copy(update={"sources": SourcePolicy(identities=[])})
    restored = RunContext(
        "resume",
        "thread",
        context.work_dir,
        policy=changed,
        user_query=QUERY,
        research_scope_id=context.research_scope_id,
        job_checkpoint_path=context.job_checkpoint_path,
    )
    with pytest.raises(RunStopped, match="Source policy changed"):
        restore_job_checkpoint(restored)
    assert context.job_checkpoint_path.read_bytes() == raw


def test_pacing_is_shared_by_concurrent_slots_and_does_not_allow_a_burst(monkeypatch):
    from agent.runtime.policies import PoolLimits
    from agent.runtime.scheduler import Gate

    clock = [1.0]
    monkeypatch.setattr("agent.runtime.scheduler.monotonic", lambda: clock[0])
    gate = Gate(PoolLimits(concurrency=3, queue_capacity=4, requests_per_second=2.0))
    first, second, third = gate.enqueue(), gate.enqueue(), gate.enqueue()
    assert gate.ready(first) and not gate.ready(second)
    clock[0] = 1.5
    assert gate.ready(second) and not gate.ready(third)
    clock[0] = 2.0
    assert gate.ready(third) and gate.active == 3
    for _ in range(3):
        gate.release()


def test_failed_dependency_is_sealed_as_unexecuted_instead_of_restarted(context):
    plan = initialize(context, ("Deep Agents",))
    parent = plan["tasks"][0]
    addition = scope("Deep Agents", object_id=parent["object_id"], depends_on=[parent["task_id"]])
    addition.requirements = []
    addition.description = "基于已有结果继续核查异常"
    updated = json.loads(
        plan_research.func(tasks=[addition], main_requirements=[], operation="append", expected_version=1)
    )
    context.search_tasks[parent["task_id"]] = SearchTaskBudget(
        closed_reason="provider failed", state=ReviewState(check_status="tool_failed")
    )
    closed = json.loads(plan_research.func(tasks=[], main_requirements=[], operation="seal", expected_version=2))
    child_id = updated["tasks"][-1]["task_id"]
    assert closed["closed"] and closed["tasks"][-1]["status"] == "blocked"
    assert context.search_tasks[child_id].evidence == []
    assert context.search_tasks[child_id].state.check_status == "dependency_unavailable"


def test_rate_limited_transport_attempts_are_charged_once_each(context, monkeypatch):
    import requests
    from requests.adapters import HTTPAdapter

    from agent.runtime.hooks import HookCall, active_call
    from agent.runtime.research import _perform, audit_transport
    from agent.runtime.search import search_allowance

    plan = initialize(context, ("Deep Agents",))
    task_id = plan["tasks"][0]["task_id"]
    allowance = SearchAllowance(context, task_id)
    gap = next(g for g in context.gaps.values() if g["task_id"] == task_id)
    attempts = []

    def transport(adapter, request, **kwargs):
        attempts.append(request.url)
        response = requests.Response()
        response.url = request.url
        response.status_code = 429 if len(attempts) == 1 else 200
        response._content = b"{}"
        return response

    def provider(**params):
        with requests.Session() as session:
            audit_transport(SimpleNamespace(session=session))
            session.get("https://api.tavily.com/search").raise_for_status()
        return "", {"results": [{"url": "https://github.com/langchain-ai/deepagents", "content": "source"}]}

    monkeypatch.setattr(HTTPAdapter, "send", transport)
    monkeypatch.setattr("tools.tavily_tool.internet_search.func", provider)
    token = active_call.set(HookCall("search:research_step", "retry", args={}, search_task_key=task_id))
    allowance_token = search_allowance.set(allowance)
    try:
        _perform("search", gap["gid"], "delegation source", "", "")
    finally:
        active_call.reset(token)
        search_allowance.reset(allowance_token)
    assert len(attempts) == len(context.outbound) == 2
    assert context.call_counts["tool"] == 2 and context.call_counts["retry"] == 1
    assert {row["status"] for row in context.attempt_ledger.values()} == {"spent"}
    assert [row["attempt"] for row in context.outbound] == [0, 1]


def test_source_tiers_come_from_original_scope_not_from_plan_output(context):
    from pydantic import ValidationError

    from agent.runtime.source_policy import requested_tiers

    assert requested_tiers("从官方源码核实") == ("official",)
    assert requested_tiers("核实源码，不要使用官方材料") == ()
    assert requested_tiers("核实源码，优先参考官方文档") == ()
    with pytest.raises(ValidationError):
        PlanRequirement(anchor="工具调用", label="工具调用", kind="fact", required_tier=["official"])
    context.user_query = "从官方源码比较 Deep Agents 的工具调用，提出建议"
    plan = initialize(context, ("Deep Agents",))
    rows = [r for r in context.requirements.values() if r["task_id"] == plan["tasks"][0]["task_id"]]
    assert rows and all(r["required_tier"] == ["official"] for r in rows)


DEFAULT_NAMES = {"db": "数据库查询助手", "search": "联网搜索助手"}
