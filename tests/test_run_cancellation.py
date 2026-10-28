"""Step 13: real HTTP cancellation/deadlines, late work and restart settlement."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from test_run_concurrency import complete, server, service_runtime, wait_started

from agent.runtime.context import RunContext, RunStopped, get_run_context, reset_run_context, set_run_context
from agent.runtime.events import EventSink
from agent.runtime.hooks import HookToolMiddleware
from agent.runtime.models import budget_model
from agent.runtime.policies import load_runtime_policy, request_timeout, reserve_calls
from api.run_store import RunStore


async def settled(client, run_id):
    async with asyncio.timeout(5):
        while True:
            record = (await client.get(f"/api/runs/{run_id}")).json()
            if record["status"] not in ("queued", "running"):
                return record
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_cancel_running_idempotent_and_retry_isolated(sandbox_dir, monkeypatch):
    async with service_runtime(sandbox_dir, monkeypatch) as (client, agent, _):
        await server.app.state.run_store.create_thread("same")
        record = (await client.post("/api/task", json={"query": "wait", "thread_id": "same"})).json()
        run_id = await wait_started(agent)
        task = server.app.state.run_tasks[run_id]
        response = await client.post(f"/api/runs/{run_id}/cancel")
        saved = response.json()
        assert response.status_code == 200 and saved["status"] == "cancelled"
        assert saved["answer"] is None and saved["ended_at"]
        await asyncio.gather(task, return_exceptions=True)
        assert (await client.post(f"/api/runs/{run_id}/cancel")).json() == saved
        assert (await client.post("/api/runs/missing/cancel")).status_code == 404
        await server.app.state.run_store.create_thread("same")
        other = (await client.post("/api/task", json={"query": "retry", "thread_id": "same"})).json()
        assert other["run_id"] != record["run_id"]
        await wait_started(agent)
        await complete(agent, other["run_id"])
        assert (await client.get(f"/api/runs/{run_id}")).json() == saved
        events = (await client.get(f"/api/runs/{run_id}/events")).json()["events"]
        assert sum(event["event"] == "run_cancelled" for event in events) == 1
        assert not any(event["event"] == "task_result" for event in events)


@pytest.mark.asyncio
async def test_cancel_completed_does_not_replace_answer(sandbox_dir, monkeypatch):
    async with service_runtime(sandbox_dir, monkeypatch) as (client, agent, _):
        record = (await client.post("/api/task", json={"query": "finish"})).json()
        await wait_started(agent)
        await complete(agent, record["run_id"])
        before = (await client.get(f"/api/runs/{record['run_id']}")).json()
        assert (await client.post(f"/api/runs/{record['run_id']}/cancel")).json() == before
        assert before["answer"] and before["status"] == "completed"


@pytest.mark.asyncio
async def test_cancel_queued_before_executor_starts(sandbox_dir, monkeypatch):
    async with service_runtime(sandbox_dir, monkeypatch) as (client, agent, _):
        await server.app.state.run_store.create_thread("queued")
        await server.app.state.event_sink.create("queued", "q", "queued-id")
        result = await client.post("/api/runs/queued-id/cancel")
        assert result.json()["status"] == "cancelled" and not agent.contexts
        assert not await server.app.state.run_store.start("queued-id")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["main_model", "sub_model", "retry"])
async def test_total_deadline_covers_all_execution_stages(sandbox_dir, monkeypatch, stage):
    reached = asyncio.Event()
    cancelled = asyncio.Event()

    async def body(question, session):
        reserve_calls(stage)
        reached.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(server, "run_deep_agent", body)
    async with service_runtime(sandbox_dir, monkeypatch) as (client, _, _):
        policy = server.app.state.runtime_policy
        server.app.state.runtime_policy = policy.model_copy(
            update={"timeouts": policy.timeouts.model_copy(update={"total_seconds": 1.5})}
        )
        record = (await client.post("/api/task", json={"query": stage})).json()
        await asyncio.wait_for(reached.wait(), 3)
        saved = await settled(client, record["run_id"])
        await asyncio.wait_for(cancelled.wait(), 3)
        assert saved["status"] == "timed_out" and saved["answer"] is None and saved["ended_at"]
        events = (await client.get(f"/api/runs/{record['run_id']}/events")).json()["events"]
        assert events[-1]["event"] == "run_timed_out"


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["cancelled", "timed_out"])
async def test_swallowed_cancellation_cannot_complete_or_start_another_action(sandbox_dir, monkeypatch, termination):
    entered, cancelled, release, returned = (asyncio.Event() for _ in range(4))
    attempts = []

    async def stubborn(question, session):
        context = get_run_context()
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        try:
            with pytest.raises(RunStopped):
                reserve_calls("main_model")
            attempts.append(context.stop_status)
            return "LATE INVENTED ANSWER"
        finally:
            returned.set()

    monkeypatch.setattr(server, "run_deep_agent", stubborn)
    async with service_runtime(sandbox_dir, monkeypatch) as (client, _, _):
        policy = server.app.state.runtime_policy
        server.app.state.runtime_policy = policy.model_copy(
            update={
                "timeouts": policy.timeouts.model_copy(
                    update={"total_seconds": 1.5 if termination == "timed_out" else 30.0}
                )
            }
        )
        record = (await client.post("/api/task", json={"query": "stubborn"})).json()
        await asyncio.wait_for(entered.wait(), 3)
        if termination == "cancelled":
            await client.post(f"/api/runs/{record['run_id']}/cancel")
        saved = await settled(client, record["run_id"])
        await asyncio.wait_for(cancelled.wait(), 3)
        assert saved["status"] == termination and not returned.is_set()
        release.set()
        await asyncio.wait_for(returned.wait(), 3)
        await asyncio.sleep(0)
        assert attempts == [termination]
        assert (await client.get(f"/api/runs/{record['run_id']}")).json() == saved
        assert not await server.app.state.run_store.finish(record["run_id"], "completed", answer="late")


@pytest.mark.asyncio
async def test_sync_tool_late_return_is_rejected(sandbox_dir):
    context = RunContext("r", "t", sandbox_dir)
    entered, release = threading.Event(), threading.Event()
    request = SimpleNamespace(tool_call={"name": "probe", "id": "probe", "args": {}})
    calls = []

    def blocking(request):
        entered.set()
        release.wait(5)
        calls.append(1)
        return ToolMessage(content="late", tool_call_id="probe")

    token = set_run_context(context)
    task = asyncio.create_task(asyncio.to_thread(HookToolMiddleware().wrap_tool_call, request, blocking))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        context.stop("cancelled")
        release.set()
        with pytest.raises(RunStopped):
            await task
        with pytest.raises(RunStopped):
            await asyncio.to_thread(HookToolMiddleware().wrap_tool_call, request, blocking)
        assert calls == [1]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        reset_run_context(token)


@pytest.mark.asyncio
async def test_child_model_request_timeout_uses_remaining_deadline(sandbox_dir):
    captured = []

    async def invoke(messages, **kwargs):
        captured.append(kwargs["timeout"])
        await asyncio.Event().wait()

    context = RunContext("r", "t", sandbox_dir, policy=load_runtime_policy())
    context.policy = context.policy.model_copy(
        update={"timeouts": context.policy.timeouts.model_copy(update={"request_seconds": 0.05})}
    )
    token = set_run_context(context)
    try:
        model = budget_model(
            RunnableLambda(lambda messages, **kwargs: AIMessage(content="ok"), afunc=invoke), "sub_model"
        )
        with pytest.raises(TimeoutError):
            await model.ainvoke("child")
        assert captured == [0.05] and context.call_counts["sub_model"] == 1
        context.deadline = datetime.now(UTC) + timedelta(seconds=0.02)
        assert 0 < request_timeout() <= 0.02
        context.deadline = datetime.now(UTC) - timedelta(seconds=1)
        with pytest.raises(RunStopped):
            reserve_calls("retry")
    finally:
        reset_run_context(token)


@pytest.mark.asyncio
async def test_restart_interrupts_previous_active_records_and_allows_retry(
    sandbox_dir, monkeypatch, migrated_environment
):
    path = sandbox_dir / "runs.db"
    old = await RunStore.open(path)
    try:
        await old.create_thread("same")
        await old.create_thread("done")
        active = await old.create("same", "old")
        await old.start(active["run_id"])
        finished = await old.create("done", "finished")
        await old.finish(finished["run_id"], "completed", answer="keep")
    finally:
        await old.close()
    async with service_runtime(sandbox_dir, monkeypatch) as (client, agent, _):
        saved = (await client.get(f"/api/runs/{active['run_id']}")).json()
        assert saved["status"] == "interrupted" and "restarted" in saved["error"]
        assert (await client.get(f"/api/runs/{finished['run_id']}")).json()["answer"] == "keep"
        events = (await client.get(f"/api/runs/{active['run_id']}/events")).json()["events"]
        assert events[-1]["event"] == "run_interrupted"
        await server.app.state.run_store.create_thread("same")
        retry = (await client.post("/api/task", json={"query": "retry", "thread_id": "same"})).json()
        await wait_started(agent)
        await complete(agent, retry["run_id"])


@pytest.mark.asyncio
async def test_shutdown_marks_running_interrupted_and_drains_executions(sandbox_dir, monkeypatch, migrated_environment):
    async with service_runtime(sandbox_dir, monkeypatch) as (client, agent, _):
        record = (await client.post("/api/task", json={"query": "wait"})).json()
        await wait_started(agent)
    assert not server.app.state.agent_tasks and not server.app.state.execution_tasks
    assert not server.manager.delivery_tasks and server.manager.loop is None
    store = await RunStore.open(sandbox_dir / "runs.db")
    try:
        assert (await store.get(record["run_id"]))["status"] == "interrupted"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_timeout_store_migration_preserves_history_and_terminal_guards(sandbox_dir, migrated_environment):
    path = sandbox_dir / "runs.db"
    store = await RunStore.open(path)
    sink = EventSink(store, lambda *args: None)
    try:
        await store.create_thread("t")
        await sink.create("t", "q", "r")
        context = RunContext("r", "t", sandbox_dir)
        await sink.finish(context, "timed_out", error="deadline")
        before = await store.history("r")
        assert not await store.finish("r", "completed", answer="late")
        assert not await store.finish("r", "cancelled")
    finally:
        await sink.close()
        await store.close()
    store = await RunStore.open(path)
    try:
        assert await store.history("r") == before
        await store.create_thread("t")
        assert (await store.create("t", "retry"))["status"] == "queued"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_total_deadline_interrupts_real_retry_backoff(sandbox_dir, monkeypatch):
    import requests

    monkeypatch.setenv("MYSQL_READONLY_USER", "test_reader")
    monkeypatch.setenv("MYSQL_READONLY_PASSWORD", "test_password")
    monkeypatch.setenv("MYSQL_DATABASE", "test_business")
    calls = []
    reached = asyncio.Event()

    async def body(question, session):
        request = SimpleNamespace(tool_call={"name": "list_sql_tables", "id": "tables", "args": {}})

        async def transient(request):
            calls.append(1)
            reached.set()
            raise requests.ConnectionError("controlled transient outage")

        await HookToolMiddleware().awrap_tool_call(request, transient)

    monkeypatch.setattr(server, "run_deep_agent", body)
    async with service_runtime(sandbox_dir, monkeypatch) as (client, _, _):
        policy = server.app.state.runtime_policy
        server.app.state.runtime_policy = policy.model_copy(
            update={
                "timeouts": policy.timeouts.model_copy(update={"total_seconds": 1.5}),
                "retry": policy.retry.model_copy(update={"delay_seconds": 4.0}),
                "sql": policy.sql.model_copy(update={"allowed_tables": ["orders"]}),
            }
        )
        record = (await client.post("/api/task", json={"query": "retry"})).json()
        await asyncio.wait_for(reached.wait(), 3)
        saved = await settled(client, record["run_id"])
        assert saved["status"] == "failed"
        assert calls == [1]


@pytest.mark.asyncio
async def test_total_deadline_interrupts_real_expert(sandbox_dir, monkeypatch):
    from agent.runtime.context import get_run_context
    from agent.runtime.contracts import bind_contract
    from agent.runtime.delegation import DelegationRunnable
    from agent.subagent_registry import SubagentSpec

    reached, stopped = asyncio.Event(), asyncio.Event()

    async def executing(state):
        reached.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def body(question, session):
        context = get_run_context()
        bind_contract(context)
        executor = DelegationRunnable(SubagentSpec("search", "搜索", "查询", "查询", ()), RunnableLambda(executing))
        await executor.ainvoke({"messages": [HumanMessage(content=question)]})
        return "late"

    monkeypatch.setattr(server, "run_deep_agent", body)
    async with service_runtime(sandbox_dir, monkeypatch) as (client, _, _):
        policy = server.app.state.runtime_policy
        server.app.state.runtime_policy = policy.model_copy(
            update={"timeouts": policy.timeouts.model_copy(update={"total_seconds": 1.5})}
        )
        record = (await client.post("/api/task", json={"query": "expert"})).json()
        await asyncio.wait_for(reached.wait(), 3)
        assert (await settled(client, record["run_id"]))["status"] == "timed_out"
        await asyncio.wait_for(stopped.wait(), 3)


@pytest.mark.parametrize("field,value", [("total_seconds", 0.0), ("request_seconds", -1.0), ("shutdown_seconds", 40.0)])
def test_timeout_settings_reject_invalid_values(field, value):
    from pydantic import ValidationError

    from agent.runtime.policies import TimeoutLimits

    with pytest.raises(ValidationError):
        TimeoutLimits.model_validate({field: value})


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_expired_expert_persists_failed_status_with_timeout_reason(
    sandbox_dir, migrated_environment, asynchronous
):
    import json
    from time import monotonic

    from agent.runtime.contracts import bind_contract
    from agent.runtime.delegation import DelegationRunnable
    from agent.subagent_registry import SubagentSpec

    path = sandbox_dir / "runs.db"
    store = await RunStore.open(path)
    sink = EventSink(store, lambda *args: None)
    work = sandbox_dir / "work"
    work.mkdir()
    context = RunContext("expired-run", "expired-thread", work, policy=load_runtime_policy(), event_sink=sink)

    def expire(state):
        # Expire inside the actual executor, after admission, without a timer race.
        context.deadline_clock = monotonic() - 1
        context.check_active()

    token = set_run_context(context)
    try:
        await store.create_thread(context.thread_id)
        await sink.create(context.thread_id, "Investigate", context.run_id)
        await sink.start(context)
        bind_contract(context)
        child = DelegationRunnable(
            SubagentSpec("probe", "Probe", "Investigate", "Investigate", ()), RunnableLambda(expire)
        )
        inputs = {"messages": [HumanMessage(content="Investigate")]}
        with pytest.raises(RunStopped, match="timed_out"):
            if asynchronous:
                await child.ainvoke(inputs)
            else:
                await asyncio.to_thread(child.invoke, inputs)
        await sink.flush(context.run_id)
        (task,) = await store.list_subtasks(context.run_id)
        (memory,) = context.task_results.values()
        assert task["status"] == memory["status"] == "failed"
        assert task["ended_at"] and task["result"] is None
        assert "timed_out" in task["error"]
        assert memory["error_category"] == "timeout"
        saved = json.loads((work / "results" / (task["call_id"] + ".json")).read_text("utf-8"))
        assert saved["status"] == "failed" and saved["error_category"] == "timeout"
        events = await store.list_events(context.run_id)
        terminals = [event for event in events if event["event"] == "subagent_failed"]
        assert len(terminals) == 1
        assert terminals[0]["status"] == "failed"
        assert terminals[0]["data"]["error_category"] == "timeout"
        assert "timed_out" in terminals[0]["data"]["error"]
        assert not any(event["event"] == "subagent_completed" for event in events)
        assert not (work / "answer.md").exists()
        assert all(row["active"] == row["queued"] == 0 for row in context.scheduler.snapshot().values())
        await sink.finish(context, "timed_out", error="Total run deadline exceeded")
        history = await store.history(context.run_id)
        assert history["run"]["status"] == "timed_out" and history["run"]["answer"] is None
        assert history["events"][-1]["event"] == "run_timed_out"
        assert "recording_status" not in history["events"][-1]["data"]
        assert not await store.finish(context.run_id, "completed", answer="late")
        late = dict(task, status="completed", result="late")
        assert (
            await store.record_subtask(late, dict(terminals[0], event="subagent_completed", status="completed")) is None
        )
        assert await store.history(context.run_id) == history
    finally:
        reset_run_context(token)
        await sink.close()
        await store.close()
    reopened = await RunStore.open(path)
    try:
        assert await reopened.history(context.run_id) == history
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_expired_expert_http_history_keeps_original_timeout(sandbox_dir, monkeypatch):
    from time import monotonic

    from agent.runtime.contracts import bind_contract
    from agent.runtime.delegation import DelegationRunnable
    from agent.subagent_registry import SubagentSpec

    contexts = []

    async def body(question, session):
        context = get_run_context()
        contexts.append(context)
        context.work_dir.mkdir(parents=True)
        bind_contract(context)

        def expire(state):
            context.deadline_clock = monotonic() - 1
            context.check_active()

        child = DelegationRunnable(
            SubagentSpec("probe", "Probe", "Investigate", "Investigate", ()), RunnableLambda(expire)
        )
        await child.ainvoke({"messages": [HumanMessage(content=question)]})
        raise AssertionError("An expired expert must not return success")

    monkeypatch.setattr(server, "run_deep_agent", body)
    async with service_runtime(sandbox_dir, monkeypatch) as (client, _, _):
        response = await client.post("/api/task", json={"query": "Investigate"})
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        run = await settled(client, run_id)
        await asyncio.gather(*list(server.app.state.agent_tasks), return_exceptions=True)
        assert run["status"] == "timed_out" and run["answer"] is None
        assert run["completion"]["status"] == "timed_out"
        assert "Run has ended: timed_out" in run["error"]
        assert "EventPersistenceError" not in run["error"]
        history = (await client.get(f"/api/runs/{run_id}/history")).json()
        (task,) = history["subtasks"]
        assert task["status"] == "failed" and task["result"] is None and task["ended_at"]
        (terminal,) = [event for event in history["events"] if event["event"] == "subagent_failed"]
        assert terminal["data"]["error_category"] == "timeout"
        assert "timed_out" in terminal["data"]["error"]
        assert history["events"][-1]["event"] == "run_timed_out"
        assert "recording_status" not in history["events"][-1]["data"]
        assert (await client.get(f"/api/runs/{run_id}/files/download?path=answer.md")).status_code == 404
        assert all(row["active"] == row["queued"] == 0 for row in contexts[0].scheduler.snapshot().values())
        assert (await client.post(f"/api/runs/{run_id}/cancel")).json()["status"] == "timed_out"
    store = await RunStore.open(sandbox_dir / "runs.db")
    try:
        assert await store.history(run_id) == history
    finally:
        await store.close()
