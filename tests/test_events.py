"""Persistence ordering, event identity, redaction, migration and failure behavior."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio

from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.events import EventPersistenceError, EventSink, make_event
from api.monitor import monitor
from api.run_store import RunStore


@pytest_asyncio.fixture
async def outlet(sandbox_dir):
    store = await RunStore.open(sandbox_dir / "runs.db")
    deliveries = []
    sink = EventSink(store, lambda payload, thread: deliveries.append((payload, thread)))
    try:
        yield store, sink, deliveries
    finally:
        await sink.close()
        await store.close()


@pytest.mark.asyncio
async def test_persisted_before_every_delivery_including_final_answer(sandbox_dir):
    path = sandbox_dir / "runs.db"
    store = await RunStore.open(path)
    observed = []

    def deliver(payload, thread):
        with sqlite3.connect(path) as connection:
            event = connection.execute(
                "SELECT event FROM events WHERE run_id=? AND event_seq=?", (payload["run_id"], payload["event_seq"])
            ).fetchone()
            assert event == (payload["event"],)
            if payload["status"] == "completed":
                assert connection.execute(
                    "SELECT status,answer FROM runs WHERE run_id=?", (payload["run_id"],)
                ).fetchone() == ("completed", "final answer")
        assert payload["thread_id"] == thread
        observed.append(payload)

    sink = EventSink(store, deliver)
    try:
        await sink.create("session", "question", "round")
        context = RunContext("round", "session", sandbox_dir, event_sink=sink)
        await sink.start(context)
        token = set_run_context(context)
        try:
            monitor.report_tool("fetch", {"value": 2})
        finally:
            reset_run_context(token)
        await sink.finish(context, "completed", answer="final answer")
        assert [event["event_seq"] for event in observed] == [1, 2, 3, 4]
    finally:
        await sink.close()
        await store.close()


@pytest.mark.asyncio
async def test_thread_and_async_events_get_unique_increasing_sequences(outlet, sandbox_dir):
    store, sink, _ = outlet
    await sink.create("thread", "query", "run")
    context = RunContext("run", "thread", sandbox_dir, event_sink=sink)
    token = set_run_context(context)
    try:
        await asyncio.gather(*(asyncio.to_thread(monitor.report_tool, f"tool-{i}", {"index": i}) for i in range(20)))
        monitor.report_assistant("search", {"description": "private task"}, call_id="model-request")
    finally:
        reset_run_context(token)
    await sink.flush()
    events = await store.list_events("run")
    assert [event["event_seq"] for event in events] == list(range(1, 23))
    assert len({event["call_id"] for event in events if event["status"] == "started"}) == 20
    request = events[-1]
    assert request["status"] == "requested" and request["call_id"] == "model-request"
    assert request["parent_call_id"] == "run"
    assert all(event["run_id"] == "run" and event["thread_id"] == "thread" and event["timestamp"] for event in events)


@pytest.mark.asyncio
async def test_parameter_strings_document_and_credentials_are_redacted(outlet, sandbox_dir):
    store, sink, deliveries = outlet
    await sink.create("session", "query api_key=secret-key", "run")
    context = RunContext("run", "session", sandbox_dir, event_sink=sink)
    token = set_run_context(context)
    try:
        monitor.report_tool(
            "write",
            {
                "api_key": "secret-key",
                "password": "secret-password",
                "authorization": "Bearer auth-token",
                "写入的文本内容": "FULL_UPLOADED_DOCUMENT",
                "nested": {"anything": "private text", "count": 3},
            },
        )
        monitor._emit(
            "error",
            "request failed password=secret-password https://user:pass@example.test Authorization: Bearer error-token",
        )
    finally:
        reset_run_context(token)
    await sink.flush()
    saved = json.dumps(await store.list_events("run"), ensure_ascii=False)
    payloads = json.dumps(deliveries, ensure_ascii=False)
    record = await store.get("run")
    for secret in (
        "secret-key",
        "secret-password",
        "auth-token",
        "FULL_UPLOADED_DOCUMENT",
        "private text",
        "user:pass",
        "error-token",
    ):
        assert secret not in saved and secret not in payloads and secret not in record["question"]
    assert "[REDACTED]" in saved and '"count": 3' in saved


@pytest.mark.asyncio
async def test_sequence_resets_between_runs_and_persists_on_reopen(outlet, sandbox_dir):
    store, sink, _ = outlet
    await sink.create("one", "q1", "run1")
    await sink.create("two", "q2", "run2")
    await sink.finish(RunContext("run1", "one", sandbox_dir), "completed", answer="one answer")
    await sink.finish(RunContext("run2", "two", sandbox_dir), "failed", error="two error")
    assert [event["event_seq"] for event in await store.list_events("run1")] == [1, 2]
    reopened = await RunStore.open(sandbox_dir / "runs.db")
    try:
        completed = (await reopened.list_events("run1"))[-1]["data"]
        failed = (await reopened.list_events("run2"))[-1]["data"]
        assert completed["result"] == "one answer" and failed["error"] == "two error"
        assert (
            completed["call_counts"]
            == failed["call_counts"]
            == dict.fromkeys(("main_model", "sub_model", "review", "tool", "retry"), 0)
        )
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_version_one_migrates_without_losing_runs(sandbox_dir):
    path = sandbox_dir / "runs.db"
    store = await RunStore.open(path)
    record = await store.create("preserved", "keep this question")
    await store.finish(record["run_id"], "completed", answer="keep this answer")
    before = await store.get(record["run_id"])
    await store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE events")
        connection.execute("PRAGMA user_version = 1")
    store = await RunStore.open(path)
    try:
        assert await store.get(record["run_id"]) == before
        assert await store.list_events(record["run_id"]) == []
        async with store.connection.execute("PRAGMA user_version") as cursor:
            assert (await cursor.fetchone())[0] == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_event_failure_rolls_back_terminal_transition(outlet, sandbox_dir, monkeypatch):
    store, sink, deliveries = outlet
    await sink.create("session", "question", "run")
    context = RunContext("run", "session", sandbox_dir)
    await sink.start(context)

    async def fail(event):
        raise RuntimeError("simulated insert failure")

    monkeypatch.setattr(store, "_append_event", fail)
    with pytest.raises(RuntimeError, match="insert failure"):
        await sink.finish(context, "completed", answer="unsaved answer")
    saved = await store.get("run")
    assert saved["status"] == "running" and saved["answer"] is None and saved["ended_at"] is None
    assert len(deliveries) == 2


@pytest.mark.asyncio
async def test_background_recording_failure_is_explicit_and_business_answer_survives(
    outlet, sandbox_dir, monkeypatch, caplog
):
    store, sink, deliveries = outlet
    await sink.create("session", "question", "run")
    original = store.append_event

    async def fail(event):
        raise RuntimeError("event database failure")

    monkeypatch.setattr(store, "append_event", fail)
    sink.submit(make_event("session", "run", "tool_start", "start", status="started"))
    with pytest.raises(EventPersistenceError, match="not saved"):
        await sink.flush()
    assert "NOT saved" in caplog.text and len(deliveries) == 1
    monkeypatch.setattr(store, "append_event", original)
    await sink.finish(RunContext("run", "session", sandbox_dir), "completed", answer="business answer")
    assert (await store.get("run"))["answer"] == "business answer"
    assert (await store.list_events("run"))[-1]["event"] == "task_result"


@pytest.mark.asyncio
async def test_wrong_session_event_is_rejected(outlet):
    store, _, _ = outlet
    record = await store.create("session", "question")
    with pytest.raises(ValueError, match="session"):
        await store.append_event(make_event("wrong-session", record["run_id"], "bad", "bad"))
    assert await store.list_events(record["run_id"]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_notification_failure_does_not_change_business_state(outlet, sandbox_dir, caplog, status):
    store, sink, _ = outlet
    await sink.create("session", "question", "run")

    def fail(*args):
        raise RuntimeError("notification transport unavailable")

    sink.deliver = fail
    await sink.finish(RunContext("run", "session", sandbox_dir), status, answer="answer", error="business error")
    record = await store.get("run")
    assert record["status"] == status
    assert record["answer"] == ("answer" if status == "completed" else None)
    assert record["error"] == "business error"
    assert (await store.list_events("run"))[-1]["status"] == status
    assert "Saved event delivery failed" in caplog.text


@pytest.mark.asyncio
async def test_full_final_answer_is_not_truncated_and_late_terminal_event_is_rejected(outlet, sandbox_dir):
    store, sink, _ = outlet
    await sink.create("session", "question", "run")
    context = SimpleNamespace(run_id="run", thread_id="session")
    answer = "final answer " * 200
    await sink.finish(context, "completed", answer=answer)
    assert not await sink.finish(context, "failed", error="late failure")
    events = await store.list_events("run")
    assert len(events) == 2 and events[-1]["data"]["result"] == answer
    assert (await store.get("run"))["answer"] == answer


@pytest.mark.asyncio
async def test_recording_failures_are_scoped_to_run_and_do_not_undo_final_answer(outlet, sandbox_dir, monkeypatch):
    store, sink, _ = outlet
    await sink.create("one", "q1", "run1")
    await sink.create("two", "q2", "run2")
    original = store.append_event

    async def fail_first(event):
        if event["run_id"] == "run1":
            raise RuntimeError("first run event failure")
        return await original(event)

    monkeypatch.setattr(store, "append_event", fail_first)
    sink.submit(make_event("one", "run1", "tool_start", "start"))
    # Completion of run2 must not consume or report run1's recording error.
    assert await sink.finish(RunContext("run2", "two", sandbox_dir), "completed", answer="second answer")
    with pytest.raises(EventPersistenceError, match="run1"):
        await sink.finish(RunContext("run1", "one", sandbox_dir), "completed", answer="first answer")
    assert (await store.get("run1"))["status"] == "completed"
    assert (await store.get("run2"))["answer"] == "second answer"
    assert not await sink.finish(RunContext("run1", "one", sandbox_dir), "failed", error="recording error")
    assert (await store.list_events("run1"))[-1]["data"]["result"] == "first answer"
