"""Retained real scheduling, queue, expiry and pacing protections."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from test_run_concurrency import complete, server, service_runtime, wait_started

from agent.runtime.context import RunContext, RunStopped, reset_run_context, set_run_context
from agent.runtime.models import budget_model
from agent.runtime.policies import load_runtime_policy
from agent.runtime.scheduler import ServiceScheduler

QUERY = "Compare tools"


def runtime_policy(**pools):
    policy = load_runtime_policy()
    scheduler = policy.scheduler.model_copy(
        update={name: getattr(policy.scheduler, name).model_copy(update=value) for name, value in pools.items()}
    )
    return policy.model_copy(update={"scheduler": scheduler})


@pytest.fixture
def context(sandbox_dir):
    policy = runtime_policy(models={"requests_per_second": 0.0}, search={"requests_per_second": 0.0})
    context = RunContext("stage-two", "thread", sandbox_dir, policy=policy, user_query=QUERY)
    token = set_run_context(context)
    try:
        yield context
    finally:
        reset_run_context(token)


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
        waiting = asyncio.create_task(budget_model(raw, "sub_model").ainvoke("cancelled"))
    finally:
        reset_run_context(token)
    await asyncio.sleep(0.05)
    assert context.scheduler.snapshot()["models"] == {"active": 1, "queued": 1}
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    release.set()
    await first
    assert calls == ["first"] and other.call_counts["sub_model"] == 0
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
        await server.app.state.run_store.create_thread("one")
        first = (await client.post("/api/task", json={"query": "source research", "thread_id": "one"})).json()
        await wait_started(agent)
        await server.app.state.run_store.create_thread("two")
        second = (await client.post("/api/task", json={"query": "source research", "thread_id": "two"})).json()
        assert (await client.get(f"/api/runs/{second['run_id']}")).json()["status"] == "queued"
        await server.app.state.run_store.create_thread("three")
        rejected = await client.post("/api/task", json={"query": "source research", "thread_id": "three"})
        assert rejected.status_code == 503
        cancelled = await client.post(f"/api/runs/{second['run_id']}/cancel")
        assert cancelled.json()["status"] == "cancelled"
        await complete(agent, first["run_id"])
        await asyncio.sleep(0.05)
        assert second["run_id"] not in agent.contexts


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
