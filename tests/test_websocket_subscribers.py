"""Multiple tabs subscribe independently to the same thread's durable events."""

import asyncio

import pytest
import pytest_asyncio
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from runtime_fixtures import ORIGIN, OfflineEnvironment, login
from test_event_replay import Socket
from test_run_concurrency import server

from agent.runtime.events import make_event
from api.monitor import ConnectionManager
from api.run_store import RunStore


@pytest.mark.asyncio
async def test_two_tabs_remain_connected_and_receive_each_event():
    manager = ConnectionManager()
    first, second = Socket(), Socket()
    try:
        await manager.connect(first, "thread")
        await manager.connect(second, "thread")
        assert first.closed is None and second.closed is None
        event = {"type": "monitor_event", "run_id": "run", "event_seq": 1}
        await manager.send_to_thread(event, "thread")
        assert first.messages == second.messages == [event]
        manager.disconnect(first, "thread")
        manager.disconnect(first, "thread")
        following = dict(event, event_seq=2)
        await manager.send_to_thread(following, "thread")
        assert first.messages == [event] and second.messages == [event, following]
    finally:
        await manager.shutdown()


@pytest_asyncio.fixture
async def subscriptions(sandbox_dir, migrated_environment):
    store = await RunStore.open(sandbox_dir / "runs.db")
    manager = ConnectionManager()
    manager.store = store
    await store.create_thread("thread")
    await store.create("thread", "first", run_id="first")
    for index in range(3):
        await store.append_event(make_event("thread", "first", "progress", str(index)))
    await store.finish("first", "completed", answer="first answer")
    await store.create("thread", "second", run_id="second")
    await store.append_event(make_event("thread", "second", "progress", "second"))
    try:
        yield manager, store
    finally:
        await manager.shutdown()
        await store.close()


@pytest.mark.asyncio
async def test_each_connection_keeps_its_run_cursor_and_thread_boundary(subscriptions):
    manager, store = subscriptions
    early, late, next_run, other_thread = Socket(), Socket(), Socket(), Socket()
    await manager.connect(early, "thread", run_id="first", after_seq=1)
    await manager.connect(late, "thread", run_id="first", after_seq=2)
    await manager.connect(next_run, "thread", run_id="second")
    await manager.connect(other_thread, "other", run_id="second", after_seq=1)
    assert [event["event_seq"] for event in early.messages] == [2, 3]
    assert [event["event_seq"] for event in late.messages] == [3]
    event = await store.append_event(make_event("thread", "second", "progress", "live"))
    await manager.send_to_thread(event, "thread")
    assert [event["event_seq"] for event in early.messages] == [2, 3]
    assert [event["event_seq"] for event in late.messages] == [3]
    assert [event["event_seq"] for event in next_run.messages] == [1, 2]
    assert other_thread.messages == []
    assert all(socket.closed is None for socket in (early, late, next_run, other_thread))


@pytest.mark.asyncio
@pytest.mark.parametrize("broken_first", [False, True])
async def test_failed_client_does_not_block_healthy_client_or_erase_history(subscriptions, broken_first, caplog):
    manager, store = subscriptions

    class BrokenSocket(Socket):
        armed = False
        attempts = 0

        async def send_json(self, value):
            if self.armed:
                self.attempts += 1
                raise RuntimeError("controlled disconnected client")
            await super().send_json(value)

    broken, healthy = BrokenSocket(), Socket()
    for socket in [broken, healthy] if broken_first else [healthy, broken]:
        await manager.connect(socket, "thread", run_id="second", after_seq=1)
    broken.armed = True
    event = await store.append_event(make_event("thread", "second", "progress", "saved"))
    await manager.send_to_thread(event, "thread")
    assert [message["event_seq"] for message in healthy.messages] == [2]
    assert broken.closed == 1000 and broken not in manager.connections
    manager.disconnect(broken, "thread")
    await manager.send_to_thread(event, "thread")
    assert len(healthy.messages) == 1 and broken.attempts == 1
    assert (await store.list_events("second"))[-1] == event
    assert "controlled disconnected client" in caplog.text


@pytest.mark.asyncio
async def test_slow_client_does_not_delay_another_clients_live_delivery():
    manager = ConnectionManager()
    entered, release, received = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class SlowSocket(Socket):
        async def send_json(self, value):
            entered.set()
            await release.wait()
            await super().send_json(value)

    class HealthySocket(Socket):
        async def send_json(self, value):
            await super().send_json(value)
            received.set()

    slow, healthy = SlowSocket(), HealthySocket()
    sending = None
    try:
        await manager.connect(slow, "thread")
        await manager.connect(healthy, "thread")
        sending = asyncio.create_task(manager.send_to_thread({"run_id": "run"}, "thread"))
        await asyncio.wait_for(received.wait(), 1)
        await asyncio.wait_for(entered.wait(), 1)
        assert not sending.done() and slow.closed is None and healthy.closed is None
        release.set()
        await asyncio.wait_for(sending, 1)
        assert slow.messages == healthy.messages == [{"run_id": "run"}]
    finally:
        release.set()
        if sending is not None:
            await asyncio.gather(sending, return_exceptions=True)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_two_replays_and_out_of_order_live_notifications_have_no_gaps_or_duplicates(subscriptions):
    manager, store = subscriptions
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedSocket(Socket):
        async def send_json(self, value):
            if not self.messages:
                entered.set()
                await release.wait()
            await super().send_json(value)

    slow, healthy = PausedSocket(), Socket()
    connecting = asyncio.create_task(manager.connect(slow, "thread", run_id="second"))
    deliveries = []
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await manager.connect(healthy, "thread", run_id="second", after_seq=1)
        second = await store.append_event(make_event("thread", "second", "progress", "2"))
        third = await store.append_event(make_event("thread", "second", "progress", "3"))
        deliveries = [asyncio.create_task(manager.send_to_thread(event, "thread")) for event in (third, second, third)]
        release.set()
        await asyncio.wait_for(asyncio.gather(connecting, *deliveries), 3)
        assert [event["event_seq"] for event in slow.messages] == [1, 2, 3]
        assert [event["event_seq"] for event in healthy.messages] == [2, 3]
        assert slow.closed is None and healthy.closed is None
    finally:
        release.set()
        await asyncio.gather(connecting, *deliveries, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_old_replay_does_not_remove_later_subscriber(subscriptions):
    manager, _ = subscriptions
    entered, release = asyncio.Event(), asyncio.Event()

    class FailedReplay(Socket):
        async def send_json(self, value):
            entered.set()
            await release.wait()
            raise RuntimeError("controlled replay failure")

    failed, healthy = FailedReplay(), Socket()
    connecting = asyncio.create_task(manager.connect(failed, "thread", run_id="second"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await manager.connect(healthy, "thread", run_id="second", after_seq=1)
        release.set()
        with pytest.raises(RuntimeError, match="replay failure"):
            await connecting
        manager.disconnect(failed, "thread")
        await manager.pong(healthy, {"type": "pong"})
        assert healthy.closed is None and healthy.messages == [{"type": "pong"}]
        assert failed not in manager.connections
    finally:
        release.set()
        await asyncio.gather(connecting, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_closes_all_subscribers_and_leaves_no_delivery_tasks():
    manager = ConnectionManager()
    manager.set_loop(asyncio.get_running_loop())
    sockets = [Socket(), Socket(), Socket()]
    await manager.connect(sockets[0], "one")
    await manager.connect(sockets[1], "one")
    await manager.connect(sockets[2], "two")
    assert all(socket.closed is None for socket in sockets)
    manager.schedule_delivery({"message": "queued"}, "one")
    await manager.shutdown()
    assert all(socket.closed == 1000 for socket in sockets)
    assert manager.active_connections == manager.connections == {}
    assert not manager.delivery_tasks and manager.loop is None and manager.store is None
    manager.schedule_delivery({"message": "after shutdown"}, "one")
    await asyncio.sleep(0)
    assert all(socket.messages == [] for socket in sockets)


def test_authenticated_asgi_tabs_keep_each_other_alive_during_replay_live_and_reconnect(sandbox_dir, monkeypatch):
    async def noop():
        pass

    environment = OfflineEnvironment(sandbox_dir)
    environment.configure(monkeypatch)
    asyncio.run(environment.migrate())
    monkeypatch.setattr(server, "initialize_agent", noop)
    monkeypatch.setattr(server, "shutdown_agent", noop)

    async def prepare():
        store = server.app.state.run_store
        await store.create_thread("thread")
        await store.create("thread", "question", run_id="run")
        await store.append_event(make_event("thread", "run", "progress", "first"))
        await store.append_event(make_event("thread", "run", "progress", "second"))

    async def publish():
        event = await server.app.state.run_store.append_event(make_event("thread", "run", "progress", "live"))
        await server.manager.send_to_thread(dict(event, type="monitor_event"), "thread")
        return event

    url = "ws://127.0.0.1:8000/ws/thread?run_id=run&after_seq="
    with TestClient(server.app, base_url=ORIGIN) as client:
        login(client)
        client.portal.call(prepare)
        with client.websocket_connect(url + "2", headers={"Origin": ORIGIN}) as second:
            with client.websocket_connect(url + "1", headers={"Origin": ORIGIN}) as first:
                assert first.receive_json()["event_seq"] == 2
                second.send_text("ping")
                assert second.receive_json()["type"] == "pong"
                event = client.portal.call(publish)
                assert first.receive_json()["event_seq"] == second.receive_json()["event_seq"] == event["event_seq"]
            second.send_text("still connected")
            assert second.receive_json()["type"] == "pong"
            with pytest.raises(WebSocketDisconnect) as denied_origin:
                with client.websocket_connect(url + "2", headers={"Origin": "https://foreign.example"}):
                    pass
            assert denied_origin.value.code == 4403
            with client.websocket_connect(url + "2", headers={"Origin": ORIGIN}) as reconnected:
                assert reconnected.receive_json()["event_seq"] == event["event_seq"]
                second.send_text("still subscribed")
                assert second.receive_json()["type"] == "pong"
                reconnected.send_text("ping")
                assert reconnected.receive_json()["type"] == "pong"
                assert client.post("/api/auth/logout").status_code == 200
                for socket in (second, reconnected):
                    socket.send_text("after logout")
                    with pytest.raises(WebSocketDisconnect) as expired:
                        socket.receive_json()
                    assert expired.value.code == 4401
        with pytest.raises(WebSocketDisconnect) as anonymous:
            with client.websocket_connect(url + "2", headers={"Origin": ORIGIN}):
                pass
        assert anonymous.value.code == 4401
    assert server.manager.active_connections == server.manager.connections == {}


@pytest.mark.asyncio
async def test_timed_out_socket_is_removed_without_losing_healthy_delivery(caplog):
    manager = ConnectionManager()

    class StalledSocket(Socket):
        async def send_json(self, value):
            await asyncio.Event().wait()

    stalled, healthy = StalledSocket(), Socket()
    try:
        await manager.connect(stalled, "thread")
        await manager.connect(healthy, "thread")
        message = {"run_id": "run", "message": "live"}
        await asyncio.wait_for(manager.send_to_thread(message, "thread"), 7)
        assert healthy.messages == [message] and healthy.closed is None
        assert stalled.closed == 1000 and stalled not in manager.connections
        assert "TimeoutError" in caplog.text
    finally:
        await manager.shutdown()
