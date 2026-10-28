"""Step 13: HTTP cursors, actual WebSocket route and ordered replay/live races."""

import asyncio
import sqlite3

import pytest
from fastapi import WebSocketDisconnect
from runtime_fixtures import ORIGIN, OfflineEnvironment, legacy_snapshot, login
from test_run_concurrency import complete, server, service_runtime, wait_started

from agent.runtime.events import make_event
from api.monitor import ConnectionManager
from api.run_store import RunStore


class Socket:
    def __init__(self):
        self.messages = []
        self.accepted = False
        self.closed = None

    async def accept(self):
        self.accepted = True

    async def send_json(self, value):
        self.messages.append(value)

    async def close(self, code=1000):
        self.closed = code

    async def receive_text(self):
        raise WebSocketDisconnect()


@pytest.mark.asyncio
async def test_http_cursor_pagination_validation_and_final_answer(sandbox_dir, monkeypatch):
    async with service_runtime(sandbox_dir, monkeypatch) as (client, agent, _):
        record = (await client.post("/api/task", json={"query": "answer"})).json()
        await wait_started(agent)
        await complete(agent, record["run_id"])
        run_id = record["run_id"]
        cursor, all_events = 0, []
        while True:
            response = await client.get(f"/api/runs/{run_id}/events", params={"after_seq": cursor, "limit": 2})
            assert response.status_code == 200
            page = response.json()
            all_events.extend(page["events"])
            cursor = page["next_seq"]
            if not page["has_more"]:
                break
        assert [e["event_seq"] for e in all_events] == list(range(1, cursor + 1))
        assert all_events[-1]["event"] == "task_result"
        assert all_events[-1]["data"]["result"] == (await client.get(f"/api/runs/{run_id}")).json()["answer"]
        empty = (await client.get(f"/api/runs/{run_id}/events", params={"after_seq": cursor})).json()
        assert empty["events"] == [] and empty["next_seq"] == cursor and not empty["has_more"]
        assert (await client.get("/api/runs/missing/events")).status_code == 404
        for params in ({"after_seq": -1}, {"after_seq": "bad"}, {"after_seq": 10**30}, {"limit": 0}, {"limit": 1001}):
            assert (await client.get(f"/api/runs/{run_id}/events", params=params)).status_code == 422


@pytest.mark.asyncio
async def test_websocket_route_replays_only_matching_run_after_cursor(sandbox_dir, monkeypatch):
    async with service_runtime(sandbox_dir, monkeypatch) as (client, agent, _):
        records = []
        for thread in ("one", "two"):
            await server.app.state.run_store.create_thread(thread)
            record = (await client.post("/api/task", json={"query": thread, "thread_id": thread})).json()
            await wait_started(agent)
            await complete(agent, record["run_id"])
            records.append(record)
        socket = Socket()
        await server.websocket_endpoint(socket, "one", run_id=records[0]["run_id"], after_seq=1)
        saved = await server.app.state.run_store.list_events(records[0]["run_id"], after_seq=1)
        assert socket.accepted and socket.messages == [dict(e, type="monitor_event") for e in saved]
        assert "one" not in server.manager.active_connections
        for thread, run_id, after in (
            ("one", records[1]["run_id"], 0),
            ("one", "missing", 0),
            ("../bad", None, 0),
            ("one", None, 1),
        ):
            denied = Socket()
            await server.websocket_endpoint(denied, thread, run_id=run_id, after_seq=after)
            assert denied.closed == 1008 and not denied.accepted and denied.messages == []


@pytest.mark.asyncio
async def test_replay_and_concurrent_live_delivery_have_no_gap_or_duplicate(sandbox_dir, migrated_environment):
    store = await RunStore.open(sandbox_dir / "runs.db")
    manager = ConnectionManager()
    manager.store = store
    await store.create_thread("thread")
    await store.create("thread", "question", run_id="run")
    for i in range(4):
        await store.append_event(make_event("thread", "run", "progress", str(i)))
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedSocket(Socket):
        async def send_json(self, event):
            if not self.messages:
                entered.set()
                await release.wait()
            await super().send_json(event)

    socket = PausedSocket()
    connecting = asyncio.create_task(manager.connect(socket, "thread", run_id="run", after_seq=1))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        fifth = await store.append_event(make_event("thread", "run", "progress", "5"))
        sixth = await store.append_event(make_event("thread", "run", "progress", "6"))
        # Higher live notification deliberately arrives first; DB catch-up fills the gap.
        deliveries = [
            asyncio.create_task(manager.send_to_thread(dict(e, type="monitor_event"), "thread"))
            for e in (sixth, fifth, sixth)
        ]
        release.set()
        await asyncio.wait_for(asyncio.gather(connecting, *deliveries), 3)
        assert [e["event_seq"] for e in socket.messages] == [2, 3, 4, 5, 6]
        assert all(e["run_id"] == "run" and e["thread_id"] == "thread" for e in socket.messages)
        # Another round in the same thread must not leak into a run-specific subscription.
        await store.finish("run", "cancelled")
        await store.create_thread("thread")
        await store.create("thread", "next", run_id="next")
        event = await store.append_event(make_event("thread", "next", "progress", "next"))
        await manager.send_to_thread(event, "thread")
        assert len(socket.messages) == 5
    finally:
        release.set()
        await asyncio.gather(connecting, return_exceptions=True)
        await manager.shutdown()
        await store.close()


@pytest.mark.asyncio
async def test_reconnect_reads_saved_events_after_disconnect_and_keeps_new_socket(sandbox_dir, migrated_environment):
    store = await RunStore.open(sandbox_dir / "runs.db")
    manager = ConnectionManager()
    manager.store = store
    try:
        await store.create_thread("thread")
        await store.create("thread", "q", run_id="run")
        first = await store.append_event(make_event("thread", "run", "progress", "first"))
        socket = Socket()
        await manager.connect(socket, "thread", run_id="run")
        manager.disconnect(socket, "thread")
        second = await store.append_event(make_event("thread", "run", "progress", "second"))
        reconnect = Socket()
        await manager.connect(reconnect, "thread", run_id="run", after_seq=first["event_seq"])
        manager.disconnect(socket, "thread")
        assert manager.active_connections["thread"] == {reconnect}
        assert [e["event_seq"] for e in reconnect.messages] == [second["event_seq"]]
        replacement = Socket()
        await manager.connect(replacement, "thread", run_id="run", after_seq=second["event_seq"])
        assert reconnect.closed is None and replacement.closed is None
        assert manager.active_connections["thread"] == {reconnect, replacement}
        manager.disconnect(reconnect, "thread")
        assert manager.active_connections["thread"] == {replacement}
    finally:
        await manager.shutdown()
        await store.close()


@pytest.mark.asyncio
async def test_v4_migration_keeps_existing_run_and_events(sandbox_dir):
    path = sandbox_dir / "runs.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, question TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled','interrupted')),
                answer TEXT, error TEXT, created_at TEXT NOT NULL, ended_at TEXT,
                CHECK((status IN ('queued','running') AND ended_at IS NULL)
                    OR (status NOT IN ('queued','running') AND ended_at IS NOT NULL))
            );
            CREATE UNIQUE INDEX one_active_run_per_thread ON runs(thread_id) WHERE status IN ('queued','running');
            INSERT INTO runs VALUES ('old','thread','中文问题','completed','原答案',NULL,'2026-10-13','2026-10-13');
            PRAGMA user_version=1;
        """)
    environment = OfflineEnvironment(sandbox_dir)
    with pytest.raises(RuntimeError, match="Security database migration required"):
        await RunStore.open(path)
    await environment.migrate()
    store = await RunStore.open(path)
    try:
        original = await store.get("old")
        assert original["answer"] == "原答案"
        event = await store.append_event(make_event("thread", "old", "task_result", "saved"))
    finally:
        await store.close()
    environment = legacy_snapshot(path, sandbox_dir / "legacy", 4)
    await environment.migrate()
    store = await RunStore.open(environment.runs)
    try:
        assert await store.get("old") == original
        assert await store.list_events("old") == [event]
        await store.create_thread("thread")
        await store.create("thread", "retry", run_id="new")
        assert await store.finish("new", "timed_out", error="deadline")
        assert not await store.finish("old", "cancelled")
        async with store.connection.execute("PRAGMA foreign_key_check") as cursor:
            assert await cursor.fetchall() == []
    finally:
        await store.close()


def test_actual_asgi_websocket_cursor_and_wrong_thread_rejection(sandbox_dir, monkeypatch):
    from fastapi.testclient import TestClient

    async def prepare():
        store = await RunStore.open(sandbox_dir / "runs.db")
        try:
            await store.create_thread("thread")
            await store.create("thread", "q", run_id="saved")
            await store.append_event(make_event("thread", "saved", "progress", "first"))
            await store.append_event(make_event("thread", "saved", "progress", "second"))
            await store.finish("saved", "completed", answer="answer")
        finally:
            await store.close()

    async def noop():
        pass

    environment = OfflineEnvironment(sandbox_dir)
    environment.configure(monkeypatch)
    asyncio.run(environment.migrate())
    asyncio.run(prepare())
    monkeypatch.setattr(server, "initialize_agent", noop)
    monkeypatch.setattr(server, "shutdown_agent", noop)
    monkeypatch.setattr(server, "run_db_path", sandbox_dir / "runs.db")
    monkeypatch.setattr(server, "output_dir", sandbox_dir / "output")
    monkeypatch.setattr(server, "updated_dir", sandbox_dir / "updated")
    with TestClient(server.app, base_url=ORIGIN) as client:
        login(client)
        with client.websocket_connect(
            "ws://127.0.0.1:8000/ws/thread?run_id=saved&after_seq=1", headers={"Origin": ORIGIN}
        ) as websocket:
            event = websocket.receive_json()
            assert event["event_seq"] == 2 and event["message"] == "second"
            websocket.send_text("ping")
            assert websocket.receive_json()["type"] == "pong"
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(
                "ws://127.0.0.1:8000/ws/other?run_id=saved&after_seq=0", headers={"Origin": ORIGIN}
            ):
                pass
        assert denied.value.code == 1008
