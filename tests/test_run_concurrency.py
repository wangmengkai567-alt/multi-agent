"""Exercise the real HTTP admission, runner context, files and persistent records."""

import asyncio
import importlib
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage
from runtime_fixtures import OfflineEnvironment

from agent.runtime.context import get_run_context
from api.context import get_session_context, get_thread_context
from api.run_store import RunStore

runtime = importlib.import_module("agent.main_agent")
server = importlib.import_module("api.server")


class MonitorRecorder:
    def __init__(self):
        self.events = []

    def _record(self, kind, value):
        context = get_run_context()
        self.events.append((kind, context.thread_id, context.run_id, value))

    def report_session_dir(self, path):
        self._record("directory", path)

    def report_task_result(self, answer):
        self._record("answer", answer)

    def report_assistant(self, name, args):
        self._record("assistant", name)

    def _emit(self, kind, message):
        self._record(kind, message)


class GatedAgent:
    def __init__(self):
        self.started = asyncio.Queue()
        self.gates = {}
        self.contexts = {}
        self.failures = set()
        self.answers = {}
        self.inputs = {}

    async def astream(self, inputs, config, stream_mode):
        context = get_run_context()
        assert get_thread_context() == context.thread_id
        assert get_session_context() == str(context.work_dir)
        assert config["configurable"]["thread_id"] == context.run_id
        self.contexts[context.run_id] = context
        self.inputs[context.run_id] = inputs
        gate = self.gates[context.run_id] = asyncio.Event()
        await self.started.put(context.run_id)
        await gate.wait()
        if context.thread_id in self.failures:
            raise RuntimeError(f"controlled failure in {context.thread_id}")
        answer = f"answer:{context.thread_id}:{context.run_id}"
        assert get_run_context() is context
        self.answers[context.run_id] = answer
        yield {"any_node": {"messages": [AIMessage(content=answer)]}}

    async def aget_state(self, config):
        return SimpleNamespace(
            values={"messages": [AIMessage(content=self.answers[config["configurable"]["thread_id"]])]}, next=()
        )


@asynccontextmanager
async def service_runtime(sandbox_dir, monkeypatch):
    agent = GatedAgent()
    recorder = MonitorRecorder()
    environment = OfflineEnvironment(sandbox_dir)
    environment.configure(monkeypatch)
    await environment.migrate()
    monkeypatch.setattr(runtime, "_build_agent", lambda saver: agent)
    monkeypatch.setattr(runtime, "monitor", recorder)
    async with environment.client() as client:
        yield client, agent, recorder


@pytest_asyncio.fixture
async def service(sandbox_dir, monkeypatch):
    async with service_runtime(sandbox_dir, monkeypatch) as value:
        yield value


async def wait_started(agent):
    return await asyncio.wait_for(agent.started.get(), timeout=3)


async def complete(agent, run_id):
    task = server.app.state.run_tasks[run_id]
    agent.gates[run_id].set()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    return task


@pytest.mark.asyncio
async def test_same_session_concurrent_requests_return_409(service):
    client, agent, _ = service
    await server.app.state.run_store.create_thread("same")
    responses = await asyncio.gather(
        *(client.post("/api/task", json={"query": f"q{i}", "thread_id": "same"}) for i in range(8))
    )
    accepted = [response for response in responses if response.status_code == 200]
    rejected = [response for response in responses if response.status_code == 409]
    assert len(accepted) == 1 and len(rejected) == 7
    record = accepted[0].json()
    assert all(response.json()["run_id"] == record["run_id"] for response in rejected)
    assert await wait_started(agent) == record["run_id"]
    assert len(agent.contexts) == 1 and len(server.app.state.run_tasks) == 1
    saved = (await client.get(f"/api/runs/{record['run_id']}")).json()
    assert saved["question"] in {f"q{i}" for i in range(8)} and saved["status"] == "running"
    await complete(agent, record["run_id"])


@pytest.mark.asyncio
async def test_two_sessions_run_concurrently_without_mixed_outputs(service):
    client, agent, recorder = service
    for name in ("alpha", "beta"):
        await server.app.state.run_store.create_thread(name)
    responses = await asyncio.gather(
        *(client.post("/api/task", json={"query": name, "thread_id": name}) for name in ("alpha", "beta"))
    )
    records = [response.json() for response in responses]
    assert all(response.status_code == 200 for response in responses)
    started = {await wait_started(agent), await wait_started(agent)}
    assert started == {record["run_id"] for record in records}
    assert len(server.app.state.run_tasks) == 2
    for record in records:
        context = agent.contexts[record["run_id"]]
        assert context.call_counts["tool"] == 0 and context.deadline is not None
        assert context.work_dir == server.output_dir / f"session_{record['thread_id']}" / f"run_{record['run_id']}"
        assert (await client.get(f"/api/runs/{record['run_id']}")).json()["status"] == "running"
    await asyncio.gather(*(complete(agent, record["run_id"]) for record in records))
    for record in records:
        context = agent.contexts[record["run_id"]]
        expected = f"answer:{record['thread_id']}:{record['run_id']}"
        saved = (await client.get(f"/api/runs/{record['run_id']}")).json()
        assert saved["status"] == "completed" and saved["answer"] == expected
        assert (context.work_dir / "answer.md").read_text(encoding="utf-8") == expected
        events = await server.app.state.run_store.list_events(record["run_id"])
        assert events[-1]["data"]["result"] == expected
    assert get_run_context() is None and get_thread_context() is None and get_session_context() is None
    assert not server.app.state.run_tasks and not server.app.state.agent_tasks


@pytest.mark.asyncio
async def test_next_round_keeps_previous_answer_and_files(service):
    client, agent, _ = service
    upload = server.updated_dir / "session_repeat"
    upload.mkdir(parents=True)
    (upload / "uploaded.txt").write_text("upload version 1", encoding="utf-8")
    await server.app.state.run_store.create_thread("repeat")
    first = (await client.post("/api/task", json={"query": "first question", "thread_id": "repeat"})).json()
    await wait_started(agent)
    old_task = await complete(agent, first["run_id"])
    before = (await client.get(f"/api/runs/{first['run_id']}")).json()
    (upload / "uploaded.txt").write_text("upload version 2", encoding="utf-8")
    await server.app.state.run_store.create_thread("repeat")
    second = (await client.post("/api/task", json={"query": "second question", "thread_id": "repeat"})).json()
    assert first["run_id"] != second["run_id"]
    await wait_started(agent)
    new_task = server.app.state.run_tasks[second["run_id"]]
    server._task_finished(old_task, first["run_id"])
    assert server.app.state.run_tasks[second["run_id"]] is new_task
    assert not await server.app.state.run_store.finish(first["run_id"], "failed", error="late error")
    await complete(agent, second["run_id"])
    assert (await client.get(f"/api/runs/{first['run_id']}")).json() == before
    for version, record in enumerate((first, second), start=1):
        directory = agent.contexts[record["run_id"]].work_dir
        assert (directory / "uploads" / "uploaded.txt").read_text(encoding="utf-8") == f"upload version {version}"
        assert record["run_id"] in (directory / "answer.md").read_text(encoding="utf-8")
        prompt = agent.inputs[record["run_id"]]["messages"].value[0].content
        assert directory.name == f"run_{record['run_id']}"
        assert "虚拟文件系统根目录为 /" in prompt and "uploads/uploaded.txt" in prompt
        assert str(directory) not in prompt


@pytest.mark.asyncio
async def test_failure_is_saved_and_session_can_retry(service, caplog):
    client, agent, _ = service
    agent.failures.add("failure")
    await server.app.state.run_store.create_thread("failure")
    first = (await client.post("/api/task", json={"query": "question", "thread_id": "failure"})).json()
    await wait_started(agent)
    await complete(agent, first["run_id"])
    saved = (await client.get(f"/api/runs/{first['run_id']}")).json()
    assert saved["status"] == "failed" and saved["answer"] is None
    assert saved["ended_at"] and saved["error"] == "controlled failure in failure"
    assert first["run_id"] in caplog.text
    agent.failures.clear()
    await server.app.state.run_store.create_thread("failure")
    next_run = await client.post("/api/task", json={"query": "retry", "thread_id": "failure"})
    assert next_run.status_code == 200 and next_run.json()["run_id"] != first["run_id"]
    await wait_started(agent)
    await complete(agent, next_run.json()["run_id"])


@pytest.mark.asyncio
async def test_generated_ids_invalid_session_and_unknown_run(service):
    client, agent, _ = service
    response = await client.post("/api/task", json={"query": "new session"})
    assert response.status_code == 200
    record = response.json()
    assert record["thread_id"] and record["run_id"] and record["thread_id"] != record["run_id"]
    await wait_started(agent)
    await complete(agent, record["run_id"])
    assert (await client.post("/api/task", json={"query": "bad", "thread_id": "../escape"})).status_code == 422
    assert (await client.get("/api/runs/unknown")).status_code == 404


@pytest.mark.asyncio
async def test_shutdown_saves_cancellation_and_closes_run_connection(service):
    client, agent, _ = service
    await server.app.state.run_store.create_thread("shutdown")
    record = (await client.post("/api/task", json={"query": "wait", "thread_id": "shutdown"})).json()
    await wait_started(agent)
    task = server.app.state.run_tasks[record["run_id"]]
    # Same cancellation path as the lifespan, inspect while the store is open.
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    saved = (await client.get(f"/api/runs/{record['run_id']}")).json()
    assert saved["status"] == "cancelled" and saved["ended_at"] and saved["error"]
    assert not server.app.state.run_tasks


@pytest.mark.asyncio
async def test_actual_lifespan_shutdown_persists_unstarted_run(sandbox_dir, monkeypatch, migrated_environment):
    async def initialize():
        pass

    monkeypatch.setattr(server, "initialize_agent", initialize)
    monkeypatch.setattr(server, "run_db_path", sandbox_dir / "runs.db")
    monkeypatch.setattr(server, "output_dir", sandbox_dir / "output")
    monkeypatch.setattr(server, "updated_dir", sandbox_dir / "updated")
    async with server.lifespan(server.app):
        # Keep a queued record to cover cancellation before a coroutine's first turn.
        await server.app.state.run_store.create_thread("queued-at-exit")
        record = await server.app.state.run_store.create("queued-at-exit", "question")
        connection = server.app.state.run_store.connection
    with pytest.raises(ValueError, match="closed|active"):
        await connection.execute("SELECT 1")
    reopened = await RunStore.open(server.run_db_path)
    try:
        saved = await reopened.get(record["run_id"])
        assert saved["status"] == "interrupted" and saved["ended_at"] and saved["error"]
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_cancelled_submission_does_not_leave_session_busy(service, monkeypatch):
    client, agent, _ = service
    store = server.app.state.run_store
    original_create = store.create
    inserted = asyncio.Event()
    records = []

    async def paused_create(*args, **kwargs):
        record = await original_create(*args, **kwargs)
        records.append(record)
        inserted.set()
        await asyncio.Event().wait()
        return record

    monkeypatch.setattr(store, "create", paused_create)
    await store.create_thread("admission")
    submission = asyncio.create_task(
        server.run_task(server.TaskRequest(query="cancelled request", thread_id="admission"))
    )
    await asyncio.wait_for(inserted.wait(), timeout=3)
    submission.cancel()
    await asyncio.gather(submission, return_exceptions=True)
    saved = await store.get(records[0]["run_id"])
    assert saved["status"] == "cancelled" and saved["error"] == "Task submission cancelled"
    assert not server.app.state.run_tasks
    monkeypatch.setattr(store, "create", original_create)
    await server.app.state.run_store.create_thread("admission")
    response = await client.post("/api/task", json={"query": "retry", "thread_id": "admission"})
    assert response.status_code == 200
    await wait_started(agent)
    await complete(agent, response.json()["run_id"])
