"""Offline HTTP history, stream parsing, disconnected and failed transports."""

import asyncio
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Overwrite
from test_run_concurrency import complete, service_runtime, wait_started

from agent.runtime.context import get_run_context
from api import server
from api.monitor import monitor
from api.run_store import RunStore


@pytest_asyncio.fixture
async def history_service(sandbox_dir, monkeypatch):
    async with service_runtime(sandbox_dir, monkeypatch) as value:
        yield value


@pytest.mark.asyncio
async def test_run_history_available_without_websocket(history_service, monkeypatch):
    from test_run_concurrency import runtime

    client, agent, _ = history_service
    monkeypatch.setattr(runtime, "monitor", monitor)
    await server.app.state.run_store.create_thread("history")
    record = (await client.post("/api/task", json={"query": "question", "thread_id": "history"})).json()
    await wait_started(agent)
    await complete(agent, record["run_id"])
    response = await client.get(f"/api/runs/{record['run_id']}/history")
    assert response.status_code == 200
    history = response.json()
    events = history["events"]
    assert events[0]["event"] == "run_queued" and events[1]["event"] == "run_started"
    assert any(event["event"] == "session_created" for event in events)
    # This fixture has no business tool calls. The runtime, not a model-facing
    # generation/read tool pair, delivers the final artifact.
    assert not [event for event in events if event["event"] == "tool_start"]
    artifact = history["run"]["completion"]["artifact"]
    assert artifact["path"] == "answer.md" and artifact["status"] == "completed"
    downloaded = await client.get(f"/api/runs/{record['run_id']}/files/download?path=answer.md")
    assert downloaded.status_code == 200 and downloaded.content == history["run"]["answer"].encode("utf-8")
    assert events[-1]["data"]["result"] == history["run"]["answer"]
    assert [event["event_seq"] for event in events] == list(range(1, len(events) + 1))
    assert all(event["thread_id"] == record["thread_id"] and event["run_id"] == record["run_id"] for event in events)
    assert (await client.get("/api/runs/missing/history")).status_code == 404


@pytest.mark.asyncio
async def test_two_rounds_two_sessions_history_does_not_mix(history_service, monkeypatch):
    from test_run_concurrency import runtime

    client, agent, _ = history_service
    monkeypatch.setattr(runtime, "monitor", monitor)
    histories = []
    for thread in ("same", "same", "other"):
        await server.app.state.run_store.create_thread(thread)
        record = (await client.post("/api/task", json={"query": f"question {thread}", "thread_id": thread})).json()
        await wait_started(agent)
        await complete(agent, record["run_id"])
        histories.append((await client.get(f"/api/runs/{record['run_id']}/history")).json())
    assert len({history["run"]["run_id"] for history in histories}) == 3
    for history in histories:
        assert all(event["run_id"] == history["run"]["run_id"] for event in history["events"])
        assert history["events"][-1]["data"]["result"] == history["run"]["answer"]

    # Preserved legacy rows are history only, isolated by run and never rewritten on read.
    from hashlib import sha256

    store = server.app.state.run_store
    first = histories[0]["run"]
    await store.connection.execute(
        "INSERT INTO subtasks (call_id,run_id,thread_id,target,display_name,description,description_length,"
        "description_sha256,status,created_at,ended_at,check_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "archived-task",
            first["run_id"],
            first["thread_id"],
            "search",
            "历史搜索",
            "历史任务",
            4,
            sha256("历史任务".encode()).hexdigest(),
            "completed",
            first["created_at"],
            first["ended_at"],
            "passed",
        ),
    )
    await store.connection.execute(
        "INSERT INTO results (result_id,run_id,thread_id,call_id,attempt,source,created_at,query_object,time_range,"
        "statistical_scope,raw_text,raw_sha256,summary,check_status,review,published,judged,facts_status,coverage_status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "archived-result",
            first["run_id"],
            first["thread_id"],
            "archived-task",
            0,
            "search",
            first["created_at"],
            "历史对象",
            "历史范围",
            "{}",
            "历史原文",
            sha256("历史原文".encode()).hexdigest(),
            "历史审核摘要",
            "passed",
            '{"historical":true}',
            1,
            1,
            "all_passed",
            "covered",
        ),
    )
    await store.connection.execute(
        "UPDATE subtasks SET current_result_id='archived-result' WHERE call_id='archived-task'"
    )
    await store.connection.commit()

    async def archived_rows():
        async with store.connection.execute("SELECT * FROM results WHERE result_id='archived-result'") as cursor:
            result = dict(await cursor.fetchone())
        async with store.connection.execute("SELECT * FROM subtasks WHERE call_id='archived-task'") as cursor:
            return result, dict(await cursor.fetchone())

    before = await archived_rows()
    for _ in range(2):
        replay = (await client.get(f"/api/runs/{first['run_id']}/history")).json()
        assert replay["events"] == histories[0]["events"] and replay["run"] == first
        (legacy,) = replay["results"]
        assert legacy["raw_text"] == "历史原文" and legacy["summary"] == "历史审核摘要"
        assert legacy["review"] == {"historical": True} and legacy["published"] is True
        assert await archived_rows() == before
    for later in histories[1:]:
        assert (await client.get(f"/api/runs/{later['run']['run_id']}/history")).json()["results"] == []
    for retired in ("record_result", "get_current_result", "get_result", "list_results"):
        assert not hasattr(store, retired)


@pytest.mark.asyncio
async def test_actual_websocket_send_failure_keeps_successful_record(history_service, monkeypatch, caplog):
    from test_run_concurrency import runtime, server

    class BrokenSocket:
        async def send_json(self, payload):
            raise RuntimeError("disconnected socket")

        async def close(self):
            pass

    client, agent, _ = history_service
    monkeypatch.setattr(runtime, "monitor", monitor)
    server.manager.active_connections["socket"] = BrokenSocket()
    await server.app.state.run_store.create_thread("socket")
    record = (await client.post("/api/task", json={"query": "question", "thread_id": "socket"})).json()
    await wait_started(agent)
    await complete(agent, record["run_id"])
    await asyncio.sleep(0)
    await asyncio.gather(*list(server.manager.delivery_tasks), return_exceptions=True)
    await asyncio.sleep(0)
    history = (await client.get(f"/api/runs/{record['run_id']}/history")).json()
    assert history["run"]["status"] == "completed" and history["events"][-1]["status"] == "completed"
    assert "disconnected socket" in caplog.text
    server.manager.disconnect(server.manager.active_connections["socket"], "socket")
    assert (await client.get(f"/api/runs/{record['run_id']}/history")).json() == history


@pytest.mark.asyncio
async def test_failure_reason_is_saved_once_and_redacted(history_service, monkeypatch):
    from test_run_concurrency import runtime

    client, agent, _ = history_service
    monkeypatch.setattr(runtime, "monitor", monitor)
    agent.failures.add("broken")
    await server.app.state.run_store.create_thread("broken")
    record = (await client.post("/api/task", json={"query": "failure question", "thread_id": "broken"})).json()
    await wait_started(agent)
    await complete(agent, record["run_id"])
    history = (await client.get(f"/api/runs/{record['run_id']}/history")).json()
    assert history["run"]["status"] == "failed"
    failure = [event for event in history["events"] if event["event"] == "error"]
    assert len(failure) == 1 and failure[0]["data"]["error"] == history["run"]["error"]
    classified = [event for event in history["events"] if event["event"] == "on_error"]
    assert len(classified) == 1 and classified[0]["data"]["error_category"] == "execution"


@pytest.mark.asyncio
async def test_proposal_return_and_business_start_have_distinct_meanings(history_service, monkeypatch):
    from test_run_concurrency import runtime, server

    client, _, _ = history_service
    monkeypatch.setattr(runtime, "monitor", monitor)

    class MessageAgent:
        async def astream(self, inputs, config, stream_mode):
            self.context = get_run_context()
            self.call_id = f"request-{self.context.run_id}"
            proposal = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {"subagent_type": "search", "description": "PRIVATE_QUERY"},
                        "id": self.call_id,
                    }
                ],
            )
            yield {"renamed_model": {"messages": [proposal]}}
            await asyncio.to_thread(monitor.report_tool, "search_fetch", {"content": "FULL_UPLOAD_BODY"})
            result = ToolMessage(content="FULL_UPLOAD_BODY", name="task", tool_call_id=self.call_id)
            yield {"renamed_tools": {"messages": result}}
            historical = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "task",
                        "args": {"subagent_type": "old", "description": "OLD_QUERY"},
                        "id": "old-round-call",
                    }
                ],
            )
            yield {
                "overwrite": {
                    "messages": Overwrite(
                        value=[historical, HumanMessage(content=inputs["messages"].value[0].content), proposal, result]
                    )
                }
            }

        async def aget_state(self, config):
            return SimpleNamespace(values={"messages": [AIMessage(content="public final answer")]}, next=())

    agent = MessageAgent()
    # The lifespan owns cleanup of this runtime pointer. A monkeypatch here
    # would restore a closed fake agent after lifespan shutdown.
    runtime.main_agent = agent
    await server.app.state.run_store.create_thread("calls")
    response = (await client.post("/api/task", json={"query": "question", "thread_id": "calls"})).json()
    task = server.app.state.run_tasks[response["run_id"]]
    await asyncio.gather(task, return_exceptions=True)
    history = (await client.get(f"/api/runs/{response['run_id']}/history")).json()
    events = history["events"]
    assert (
        len([event for event in events if event["event"] == "assistant_call" and event["status"] == "requested"]) == 1
    )
    assert len([event for event in events if event["event"] == "tool_start" and event["status"] == "started"]) == 1
    returned = [event for event in events if event["event"] == "tool_returned"]
    assert len(returned) == 1 and returned[0]["call_id"] == agent.call_id
    assert all(event["call_id"] != "old-round-call" for event in events)
    serialized = json.dumps(events)
    assert "FULL_UPLOAD_BODY" not in serialized and "PRIVATE_QUERY" not in serialized and "OLD_QUERY" not in serialized
    reopened = await RunStore.open(server.run_db_path)
    try:
        assert await reopened.list_events(response["run_id"]) == events
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_real_framework_tool_records_proposal_execution_and_return(history_service, monkeypatch):
    from deepagents import create_deep_agent
    from langchain_core.tools import tool
    from test_framework_contract import ScriptedModel
    from test_run_concurrency import runtime, server

    client, _, _ = history_service
    calls = []

    @tool
    def event_probe(value: str) -> str:
        """Return controlled test evidence."""
        calls.append(value)
        monitor.report_tool("event_probe", {"value": value})
        return "private tool evidence"

    model = ScriptedModel(
        responses=[
            AIMessage(
                content="", tool_calls=[{"name": "event_probe", "args": {"value": "private input"}, "id": "probe-id"}]
            ),
            AIMessage(content="public answer"),
        ]
    )
    agent = create_deep_agent(model=model, tools=[event_probe], checkpointer=runtime.checkpointer)
    # The lifespan owns cleanup of this runtime pointer. A monkeypatch here
    # would restore a closed fake agent after lifespan shutdown.
    runtime.main_agent = agent
    monkeypatch.setattr(runtime, "monitor", monitor)
    await server.app.state.run_store.create_thread("framework")
    record = (await client.post("/api/task", json={"query": "question", "thread_id": "framework"})).json()
    await asyncio.gather(server.app.state.run_tasks[record["run_id"]], return_exceptions=True)
    history = (await client.get(f"/api/runs/{record['run_id']}/history")).json()
    assert calls == ["private input"]
    events = history["events"]
    proposed = [event for event in events if event["event"] == "tool_requested"]
    executed = [event for event in events if event["event"] == "tool_start"]
    returned = [event for event in events if event["event"] == "tool_returned"]
    assert len(proposed) == len(executed) == len(returned) == 1
    assert proposed[0]["status"] == "requested" and executed[0]["status"] == "started"
    assert proposed[0]["call_id"] == returned[0]["call_id"] == "probe-id"
    assert history["run"]["status"] == "completed", history["run"]["error"]
    assert history["run"]["answer"] == "public answer"
    assert "private input" not in json.dumps(events) and "private tool evidence" not in json.dumps(events)
