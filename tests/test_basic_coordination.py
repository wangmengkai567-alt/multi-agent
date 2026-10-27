"""Basic multi-agent acceptance through the real authenticated HTTP and graph boundary."""

import asyncio
import json
from hashlib import sha256
from uuid import UUID

import httpx
import pytest
from langchain_core.messages import AIMessage
from test_framework_contract import ScriptedModel, call


async def finished(client, run_id):
    for _ in range(800):
        response = await client.get("/api/runs/" + run_id)
        assert response.status_code == 200, response.text
        row = response.json()
        if row["status"] not in ("queued", "running"):
            return row
        await asyncio.sleep(0.025)
    raise AssertionError("Run did not reach a terminal state")


@pytest.fixture
def providers(monkeypatch):
    from agent.runtime import workers
    from agent.runtime.context import get_run_context

    actual = []
    real_worker = workers.run_operation

    def operation(name, payload, *, seconds, on_event=None):
        assert seconds > 0
        context = get_run_context()
        if name == "tavily":
            assert callable(on_event)
            on_event({"event": "progress", "kind": "transport"})
            actual.append((context.run_id, name, payload["parameters"]))
            return {
                "results": [
                    {
                        "url": "https://example.test/manual",
                        "title": "Official manual",
                        "content": "The service supports sequential task delegation.",
                    }
                ]
            }
        if name == "database":
            actual.append((context.run_id, name, payload))
            assert payload["config"]["user"] == "fixture_reader"
            return "orders,total\n3,120\n"
        return real_worker(name, payload, seconds=seconds, on_event=on_event)

    monkeypatch.setattr(workers, "run_operation", operation)
    monkeypatch.setenv("TAVILY_API_KEY", "fixture-only-key")
    monkeypatch.setenv("MYSQL_READONLY_USER", "fixture_reader")
    monkeypatch.setenv("MYSQL_READONLY_PASSWORD", "fixture-password")
    monkeypatch.setenv("MYSQL_DATABASE", "fixture_database")
    return actual


def models(monkeypatch, main, child):
    from agent import llm

    parent = ScriptedModel(responses=main)
    expert = ScriptedModel(responses=child)
    monkeypatch.setattr(llm, "create_role_model", lambda role: {"MAIN": parent, "SUB": expert}[role])
    return parent, expert


@pytest.mark.asyncio
async def test_search_delegation_sources_and_exact_authenticated_markdown(migrated_environment, monkeypatch, providers):
    parent, expert = models(
        monkeypatch,
        [
            call("task", {"subagent_type": "search", "description": "查服务委派方式"}),
            AIMessage(content="支持顺序委派。\n\n[来源](https://example.test/manual)"),
        ],
        [
            call("internet_search", {"query": "service delegation"}),
            AIMessage(content="支持顺序委派。[来源](https://example.test/manual)"),
        ],
    )
    async with migrated_environment.client() as client:
        submission = await client.post("/api/task", json={"query": "调查委派方式"})
        assert submission.status_code == 200, submission.text
        run = await finished(client, submission.json()["run_id"])
        assert run["status"] == "completed", run
        assert run["completion"]["status"] == "complete"
        history = (await client.get(f"/api/runs/{run['run_id']}/history")).json()
        (task,) = history["subtasks"]
        assert str(UUID(task["call_id"])) == task["call_id"] and task["status"] == "completed"
        assert task["call_id"] != task["request_call_id"]
        assert len(providers) == 1 and providers[0][1] == "tavily"
        assert "https://example.test/manual" in str(parent.seen_messages[-1])
        assert "plan_research" not in parent.tool_names and "research_step" not in expert.tool_names
        assert {"internet_search", "read_web_page"} <= expert.tool_names
        url = f"/api/runs/{run['run_id']}/files/download?path=answer.md"
        downloaded = await client.get(url)
        assert downloaded.status_code == 200
        assert downloaded.content == run["answer"].encode("utf-8")
        assert b"\r\n" in downloaded.content and b"\n" not in downloaded.content.replace(b"\r\n", b"")
        assert sha256(downloaded.content).hexdigest() == run["completion"]["artifact"]["file_digest"]
        async with httpx.AsyncClient(transport=client._transport, base_url="http://127.0.0.1:8000") as anonymous:
            assert (await anonymous.get(url)).status_code == 401
        assert not parent.responses and not expert.responses


@pytest.mark.asyncio
@pytest.mark.parametrize("batched", [False, True])
async def test_three_experts_use_separate_results_and_bound_document(
    migrated_environment, monkeypatch, providers, batched
):
    from api import server

    policy = server.load_runtime_policy()
    policy = policy.model_copy(update={"sql": policy.sql.model_copy(update={"allowed_tables": ["orders"]})})
    monkeypatch.setattr(server, "load_runtime_policy", lambda: policy)
    parent, expert = models(
        monkeypatch,
        [
            call("task", {"subagent_type": "search", "description": "调查服务"}),
            call("task", {"subagent_type": "document", "description": "读取 uploads/note.txt"}),
            call("task", {"subagent_type": "db", "description": "查询订单"}),
            AIMessage(content="外部支持顺序委派；材料记载销售额；订单合计为120。[来源](https://example.test/manual)"),
        ],
        [
            call("internet_search", {"query": "service"}),
            AIMessage(content="支持顺序委派。[来源](https://example.test/manual)"),
            call("read_document", {"filename": "uploads/note.txt"}),
            AIMessage(content="材料第1行：销售额为120。"),
            call("execute_sql_query", {"query": "SELECT SUM(total) FROM orders"}),
            AIMessage(content="数据库订单总额120。"),
        ],
    )
    if batched:
        tasks = [item.tool_calls[0] for item in parent.responses[:3]]
        parent.responses = [AIMessage(content="", tool_calls=tasks), parent.responses[-1]]
    async with migrated_environment.client() as client:
        thread = (await client.post("/api/threads")).json()["thread_id"]
        uploaded = await client.post(
            "/api/upload", data={"thread_id": thread}, files={"files": ("note.txt", "销售额为120。".encode())}
        )
        assert uploaded.status_code == 200, uploaded.text
        result = await client.post(
            "/api/task",
            json={
                "query": "综合资料",
                "thread_id": thread,
                "required_sources": ["db", "search"],
                "required_uploads": ["note.txt"],
            },
        )
        run = await finished(client, result.json()["run_id"])
        assert run["status"] == "completed", run
        assert run["completion"]["status"] == "complete", run
        history = (await client.get(f"/api/runs/{run['run_id']}/history")).json()
        assert {row["target"] for row in history["subtasks"]} == {"db", "document", "search"}
        assert len({row["call_id"] for row in history["subtasks"]}) == 3
        started = sorted(
            history["subtasks"],
            key=lambda row: next(
                e["event_seq"]
                for e in history["events"]
                if e["event"] == "subagent_running" and e["call_id"] == row["call_id"]
            ),
        )
        for left, right in zip(started, started[1:], strict=False):
            end = next(
                e["event_seq"]
                for e in history["events"]
                if e["event"] == "subagent_completed" and e["call_id"] == left["call_id"]
            )
            begin = next(
                e["event_seq"]
                for e in history["events"]
                if e["event"] == "subagent_running" and e["call_id"] == right["call_id"]
            )
            assert end < begin
        directory = migrated_environment.root / "output" / f"session_{thread}" / f"run_{run['run_id']}" / "results"
        snapshots = [json.loads(file.read_text()) for file in directory.glob("*.json")]
        assert len(snapshots) == 3
        for row in snapshots:
            assert row["status"] == "completed" and row["evidence"]
            assert all(
                item["task_id"] == row["call_id"] and item["run_id"] == run["run_id"] for item in row["evidence"]
            )
        doc = next(row for row in snapshots if row["target"] == "document")
        assert doc["sources"][0]["inputs"][0]["path"] == "uploads/note.txt"
        assert doc["sources"][0]["locator"]
        assert [row[1] for row in providers] == ["tavily", "database"]
        assert not parent.responses and not expert.responses


@pytest.mark.asyncio
async def test_failed_expert_cannot_hide_behind_successful_sibling(migrated_environment, monkeypatch, providers):
    models(
        monkeypatch,
        [
            call("task", {"subagent_type": "search", "description": "搜索"}),
            call("task", {"subagent_type": "document", "description": "读取"}),
            AIMessage(content="已有搜索结果。"),
        ],
        [
            call("internet_search", {"query": "service"}),
            AIMessage(content="取得结果。"),
            call("read_document", {"filename": "uploads/missing.txt"}),
            AIMessage(content="无法读取材料。"),
        ],
    )
    async with migrated_environment.client() as client:
        result = await client.post("/api/task", json={"query": "搜索并核对材料"})
        run = await finished(client, result.json()["run_id"])
        assert run["status"] == "completed", run
        assert run["completion"]["status"] == "partial"
        assert "部分完成" in run["answer"] and "上传材料分析助手" in run["answer"]
        rows = (await client.get(f"/api/runs/{run['run_id']}/history")).json()["subtasks"]
        assert {row["status"] for row in rows} == {"completed", "failed"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["fabricated_url", "no_tool", "unknown_expert", "forbidden_network"])
async def test_no_false_success_or_file_after_invalid_investigation(migrated_environment, monkeypatch, providers, kind):
    child = [call("internet_search", {"query": "service"}), AIMessage(content="结果。")]
    parent = [
        call("task", {"subagent_type": "missing" if kind == "unknown_expert" else "search", "description": "调查"}),
        AIMessage(content="没有足够依据。"),
    ]
    if kind == "fabricated_url":
        parent[-1] = AIMessage(content="错误引用 https://example.test/invented")
    if kind == "no_tool":
        child = [AIMessage(content="声称已经查到结果。")]
    models(monkeypatch, parent, child)
    async with migrated_environment.client() as client:
        result = await client.post(
            "/api/task",
            json={"query": "调查", "network_policy": "forbidden" if kind == "forbidden_network" else "allowed"},
        )
        run = await finished(client, result.json()["run_id"])
        assert run["status"] == "failed" and run["answer"] is None
        assert (await client.get(f"/api/runs/{run['run_id']}/files/download?path=answer.md")).status_code == 404
        if kind in ("unknown_expert", "forbidden_network"):
            assert not providers
