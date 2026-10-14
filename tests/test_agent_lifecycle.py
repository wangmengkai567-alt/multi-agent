"""Step 03: real async checkpoint plus offline service lifecycle paths."""

import asyncio
import importlib
import subprocess
import sys
from types import SimpleNamespace

import pytest
import pytest_asyncio
from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from test_framework_contract import ScriptedModel

runtime = importlib.import_module("agent.main_agent")
server = importlib.import_module("api.server")


class RecordingMonitor:
    def __init__(self):
        self.results = []
        self.errors = []
        self.assistants = []

    def report_session_dir(self, path):
        pass

    def report_assistant(self, name, args):
        self.assistants.append((name, args))

    def report_task_result(self, result):
        self.results.append(result)

    def _emit(self, event, message):
        self.errors.append((event, message))


@pytest_asyncio.fixture
async def isolated_runtime(sandbox_dir, monkeypatch):
    monkeypatch.setattr(runtime, "checkpoint_db_path", sandbox_dir / "data" / "checkpoints.db")
    monkeypatch.setattr(runtime, "project_root_path", sandbox_dir)
    recorder = RecordingMonitor()
    monkeypatch.setattr(runtime, "monitor", recorder)
    monkeypatch.setattr(server, "output_dir", sandbox_dir / "output")
    monkeypatch.setattr(server, "updated_dir", sandbox_dir / "updated")
    yield recorder
    await server.manager.shutdown()
    await runtime.shutdown_agent()


def test_import_has_no_runtime_side_effects(sandbox_dir):
    code = """
import pathlib, sqlite3, sys

def forbidden(*args, **kwargs):
    raise AssertionError('import attempted runtime I/O')
pathlib.Path.mkdir = forbidden
sqlite3.connect = forbidden
import agent.main_agent as runtime
import api.server
assert runtime.main_agent is None and runtime.checkpointer is None
assert 'agent.llm' not in sys.modules
assert 'tools.db_tools' not in sys.modules
assert 'tools.ragflow_tools' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(runtime.project_root_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_concurrent_initialization_and_reopen(isolated_runtime, monkeypatch):
    calls = []

    def build(saver):
        calls.append(saver)
        return create_deep_agent(model=ScriptedModel(responses=[AIMessage(content="FIRST")]), checkpointer=saver)

    monkeypatch.setattr(runtime, "_build_agent", build)
    agents = await asyncio.gather(*(runtime.initialize_agent() for _ in range(8)))
    assert all(agent is agents[0] for agent in agents)
    assert len(calls) == 1
    config = {"configurable": {"thread_id": "persisted"}}
    assert await runtime.run_deep_agent("question", "persisted") == "FIRST"
    saver = runtime.checkpointer
    await runtime.shutdown_agent()
    assert runtime.main_agent is None and runtime.checkpointer is None
    with pytest.raises(ValueError, match="closed|active"):
        await saver.conn.execute("SELECT 1")
    reopened = await runtime.initialize_agent()
    state = await reopened.aget_state(config)
    assert state.values["messages"][-1].content == "FIRST"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_initialization_failure_closes_connection_and_can_retry(isolated_runtime, monkeypatch):
    savers = []

    def fail(saver):
        savers.append(saver)
        raise RuntimeError("construction failed")

    monkeypatch.setattr(runtime, "_build_agent", fail)
    with pytest.raises(RuntimeError, match="construction failed"):
        await runtime.initialize_agent()
    assert runtime.main_agent is None and runtime._resources is None
    with pytest.raises(ValueError, match="closed|active"):
        await savers[0].conn.execute("SELECT 1")
    monkeypatch.setattr(runtime, "_build_agent", lambda saver: object())
    assert await runtime.initialize_agent() is runtime.main_agent


@pytest.mark.asyncio
async def test_stream_uses_messages_and_completed_state(isolated_runtime, monkeypatch):
    def build(saver):
        graph = StateGraph(MessagesState)
        graph.add_node("renamed_writer", lambda state: {"messages": [AIMessage(content="DRAFT")]})
        graph.add_node(
            "renamed_finalizer",
            lambda state: {"messages": [AIMessage(content=[{"type": "text", "text": "FINAL"}])]},
        )
        graph.add_edge(START, "renamed_writer")
        graph.add_edge("renamed_writer", "renamed_finalizer")
        graph.add_edge("renamed_finalizer", END)
        return graph.compile(checkpointer=saver)

    monkeypatch.setattr(runtime, "_build_agent", build)
    assert await runtime.run_deep_agent("question", "stream") == "FINAL"
    assert isolated_runtime.results == ["FINAL"]
    from api.context import get_session_context, get_thread_context

    assert get_session_context() is None and get_thread_context() is None


class ControlledAgent:
    def __init__(self, fail=False, hang=False, invalid=False):
        self.fail, self.hang, self.invalid = fail, hang, invalid
        self.started = asyncio.Event()
        self.cancelled = False

    async def astream(self, inputs, config, stream_mode):
        assert stream_mode == "updates"
        self.started.set()
        yield {
            "new_node": {
                "messages": [
                    HumanMessage(content="input"),
                    ToolMessage(content="tool output", tool_call_id="id"),
                    AIMessage(
                        content="intermediate",
                        tool_calls=[
                            {
                                "name": "task",
                                "args": {"subagent_type": "search", "description": "lookup"},
                                "id": "id",
                            }
                        ],
                    ),
                ]
            }
        }
        if self.fail:
            raise RuntimeError("controlled execution failure")
        if self.hang:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def aget_state(self, config):
        message = (
            ToolMessage(content="not an answer", tool_call_id="id") if self.invalid else AIMessage(content="ANSWER")
        )
        return SimpleNamespace(values={"messages": [message]}, next=())


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_background_completion_and_exception(isolated_runtime, monkeypatch, caplog, fail):
    agent = ControlledAgent(fail=fail)
    monkeypatch.setattr(runtime, "_build_agent", lambda saver: agent)
    async with server.lifespan(server.app):
        response = await server.run_task(server.TaskRequest(query="question", thread_id="background"))
        assert response == {"status": "started", "thread_id": "background"}
        task = next(iter(server.app.state.agent_tasks))
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        assert not server.app.state.agent_tasks
        assert isolated_runtime.assistants == [("search", {"description": "lookup"})]
        if fail:
            assert "controlled execution failure" in caplog.text
            assert isolated_runtime.errors and not isolated_runtime.results
        else:
            assert isolated_runtime.results == ["ANSWER"]


@pytest.mark.asyncio
async def test_shutdown_cancels_before_closing_database(isolated_runtime, monkeypatch):
    agent = ControlledAgent(hang=True)
    monkeypatch.setattr(runtime, "_build_agent", lambda saver: agent)
    async with server.lifespan(server.app):
        saver = runtime.checkpointer
        await server.run_task(server.TaskRequest(query="question", thread_id="cancelled"))
        task = next(iter(server.app.state.agent_tasks))
        await agent.started.wait()
        delivery = asyncio.create_task(asyncio.Event().wait())
        server.manager.track_delivery(delivery)
        await asyncio.sleep(0)
    assert agent.cancelled and task.cancelled() and delivery.cancelled()
    assert not server.app.state.agent_tasks and not server.manager.delivery_tasks
    assert server.manager.loop is None
    assert runtime.main_agent is None
    assert not isolated_runtime.results and not isolated_runtime.errors
    with pytest.raises(ValueError, match="closed|active"):
        await saver.conn.execute("SELECT 1")


@pytest.mark.asyncio
async def test_no_final_ai_answer_is_failure(isolated_runtime, monkeypatch):
    monkeypatch.setattr(runtime, "_build_agent", lambda saver: ControlledAgent(invalid=True))
    with pytest.raises(RuntimeError, match="without a final"):
        await runtime.run_deep_agent("question", "invalid")
    assert not isolated_runtime.results and isolated_runtime.errors


@pytest.mark.asyncio
async def test_monitor_delivery_exception_is_observed(isolated_runtime, caplog):
    async def fail():
        raise RuntimeError("delivery failed")

    server.manager.set_loop(asyncio.get_running_loop())
    delivery = asyncio.create_task(fail())
    server.manager.track_delivery(delivery)
    await asyncio.gather(delivery, return_exceptions=True)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not server.manager.delivery_tasks
    assert "delivery failed" in caplog.text


@pytest.mark.asyncio
async def test_stream_is_closed_when_parser_raises(isolated_runtime, monkeypatch):
    closed = []

    class BrokenStream:
        async def astream(self, *args, **kwargs):
            try:
                yield {
                    "changed_node": {
                        "messages": AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "task",
                                    "args": {},
                                    "id": "bad-call",
                                }
                            ],
                        )
                    }
                }
                raise AssertionError("parser should have stopped iteration")
            finally:
                closed.append(True)

    monkeypatch.setattr(runtime, "_build_agent", lambda saver: BrokenStream())
    with pytest.raises(KeyError):
        await runtime.run_deep_agent("question", "parser-error")
    assert closed == [True]
    assert isolated_runtime.errors and not isolated_runtime.results
    from api.context import get_session_context, get_thread_context

    assert get_session_context() is None and get_thread_context() is None


@pytest.mark.asyncio
async def test_startup_failure_does_not_serve_and_releases_resources(isolated_runtime, monkeypatch):
    savers = []

    def fail(saver):
        savers.append(saver)
        raise RuntimeError("startup construction failed")

    monkeypatch.setattr(runtime, "_build_agent", fail)
    with pytest.raises(RuntimeError, match="startup construction failed"):
        async with server.lifespan(server.app):
            pytest.fail("failed initialization must not start serving requests")
    assert runtime.main_agent is None and server.manager.loop is None
    assert server.app.state.stopping
    with pytest.raises(ValueError, match="closed|active"):
        await savers[0].conn.execute("SELECT 1")


@pytest.mark.asyncio
async def test_tool_thread_delivery_is_awaited_at_shutdown(isolated_runtime, monkeypatch):
    started = asyncio.Event()
    stopped = []

    async def send(payload, thread_id):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(thread_id)

    server.manager.set_loop(asyncio.get_running_loop())
    monkeypatch.setattr(server.manager, "send_to_thread", send)
    await asyncio.to_thread(server.manager.schedule_delivery, {"event": "test"}, "thread-delivery")
    await started.wait()
    await server.manager.shutdown()
    assert stopped == ["thread-delivery"]
    assert not server.manager.delivery_tasks
