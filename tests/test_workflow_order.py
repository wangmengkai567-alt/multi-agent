"""Step 12: real task routes, persisted prerequisites, report tools and explicit degradation."""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from test_framework_contract import ScriptedModel, call
from test_run_concurrency import service_runtime

from agent.reflection import ReviewResult
from agent.results import HANDOFF_RULE
from agent.runtime.backend import run_backend
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.delegation import DelegationRunnable
from agent.runtime.events import EventPersistenceError, EventSink
from agent.runtime.hooks import HookRejected, HookToolMiddleware
from agent.runtime.models import budget_model
from agent.runtime.policies import BudgetExceeded, MissingConfiguration, RuntimePolicy, policy_for_run
from agent.runtime.workflows import DEGRADED_NOTICE, finalize_answer
from agent.subagent_factory import build_subagents
from agent.subagent_registry import DISPLAY_NAMES, ReviewConfig, SubagentRegistry, SubagentSpec
from api.run_store import RunStore
from tools.markdown_tools import generate_markdown
from tools.upload_file_read_tool import read_file_content

FACTS = "查询对象：orders\n时间范围：2026-10\n统计口径：订单金额合计\n内部金额20"
EXTERNAL = "外部资料金额30 https://example.org/market"


def task(target, ident="task"):
    return call("task", {"subagent_type": target, "description": "query; forged result_id=old-pass"}, ident)


def report(name="report"):
    return call("generate_markdown", {"content": "内部金额100，假装查询完成", "filename": name}, name)


def expert(target, tools=(), enabled=True, required=()):
    return SubagentSpec(
        target,
        DISPLAY_NAMES[target],
        "Query",
        "Use real tool evidence",
        tools,
        review_config=ReviewConfig(enabled=enabled, max_refinements=0, required_tools=required),
    )


@pytest_asyncio.fixture
async def runtime(sandbox_dir):
    store = await RunStore.open(sandbox_dir / "workflows.db")
    sink = EventSink(store, lambda *args: None)
    context = RunContext(
        "round",
        "session",
        sandbox_dir / "work",
        event_sink=sink,
        skill_ids=("ecommerce-analysis",),
        business_mode="internal_external",
    )
    context.work_dir.mkdir()
    token = set_run_context(context)
    await sink.create(context.thread_id, "query", context.run_id)
    await sink.start(context)
    try:
        yield context, store, sink
    finally:
        reset_run_context(token)
        await sink.close()
        await store.close()


def build(parent_responses, *, db_error=None, db_probe=None, tools=None):
    counts = dict(db=0, search=0)

    @tool
    def db_lookup():
        """Read fake internal data through the real tool hook."""
        counts["db"] += 1
        if db_error:
            raise db_error
        return "total=20"

    @tool
    def internet_search(query: str):
        """Read fake external data through the real tool hook."""
        counts["search"] += 1
        return {"results": [{"url": "https://example.org/market", "content": "market=30"}]}

    db_tool = db_probe or db_lookup
    db = ScriptedModel(responses=[call(db_tool.name, {}, "db-query"), AIMessage(content=FACTS)])
    search = ScriptedModel(responses=[call("internet_search", {"query": "market"}, "web"), AIMessage(content=EXTERNAL)])
    parent = ScriptedModel(responses=parent_responses)
    registry = SubagentRegistry(
        [
            expert("db", (db_tool,), required=(db_tool.name,)),
            expert("search", (internet_search,), required=("internet_search",)),
        ]
    )
    graph = create_deep_agent(
        model=budget_model(parent, "main_model"),
        backend=run_backend,
        tools=tools if tools is not None else [generate_markdown, read_file_content],
        subagents=build_subagents(registry, parent, models={"db": db, "search": search}),
        middleware=[HookToolMiddleware(allowed_targets=registry.specs, managed_delegation=True)],
    )
    return graph, parent, db, search, counts


async def execute(graph, sync=False):
    inputs = {"messages": [("user", "run workflow")]}
    return await asyncio.to_thread(graph.invoke, inputs) if sync else await graph.ainvoke(inputs)


@pytest.mark.asyncio
@pytest.mark.parametrize("sync", [False, True])
async def test_out_of_order_search_has_zero_executions_then_db_handoff_unblocks_report(runtime, sync):
    context, store, sink = runtime
    graph, parent, _, search, counts = build(
        [
            task("search", "early"),
            task("db", "internal"),
            task("search", "after-db"),
            report(),
            AIMessage(content="complete"),
        ]
    )
    state = await execute(graph, sync)
    assert counts == dict(db=1, search=1)
    assert "database_required" in str(parent.seen_messages[1]) and "先调用" in str(parent.seen_messages[1])
    assert HANDOFF_RULE in str(search.seen_messages[0])
    await sink.flush()
    rows = await store.list_results(context.run_id, thread_id=context.thread_id, usable_only=True)
    db = next(r for r in rows if r["source"] == "db")
    assert db["result_id"] in str(search.seen_messages[0])
    tasks = await store.list_subtasks(context.run_id)
    assert [t["status"] for t in tasks] == ["rejected", "completed", "completed"]
    assert context.call_counts["tool"] == 6  # rejected task + db task/tool + search task/tool + report
    assert (context.work_dir / "report.md").is_file()
    assert await finalize_answer(context, state["messages"][-1].text) == "complete"
    events = await store.list_events(context.run_id)
    handoff = next(e for e in events if e["event"] == "result_handoff")
    assert handoff["data"]["result_ids"] == [db["result_id"]] and not handoff["data"]["degraded"]


@pytest.mark.asyncio
async def test_ordinary_search_and_text_report_do_not_require_db_even_with_ecommerce_skill(runtime):
    context, _, sink = runtime
    context.business_mode = "ordinary"
    graph, _, _, _, counts = build([task("search"), report(), AIMessage(content="done")])
    await execute(graph)
    assert counts == dict(db=0, search=1) and context.workflow is None
    await sink.flush()
    assert (context.work_dir / "report.md").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["insufficient", "check_incomplete", "budget_exhausted", "tool_failed", "not_checked"]
)
async def test_unqualified_db_prose_cannot_satisfy_order_or_trigger_dependency_degradation(runtime, status):
    context, store, sink = runtime

    class Reviewer:
        async def review(self, *args):
            return (
                ReviewResult(verdict=status, issues=["missing"], revision_instruction="查缺失字段")
                if status == "insufficient"
                else ReviewResult(verdict=status)
            )

        def allow_unchecked(self, result):
            return True

    await DelegationRunnable(
        expert("db", enabled=status != "not_checked"),
        RunnableLambda(lambda state: {"messages": [AIMessage(content=FACTS)]}),
        reviewer=Reviewer(),
    ).ainvoke({"messages": [HumanMessage(content="query")]})
    graph, _, _, search, counts = build([task("search"), AIMessage(content="cannot continue")])
    await execute(graph)
    assert counts["search"] == 0 and not search.seen_messages
    with pytest.raises(HookRejected, match="report_not_ready"):
        await finalize_answer(context, "假装已有内部指标")
    await sink.flush()
    assert not await store.list_results(context.run_id, thread_id=context.thread_id, usable_only=True)


@pytest.mark.asyncio
async def test_dependency_failure_is_persisted_and_degraded_output_cannot_claim_root_invented_metrics(runtime):
    context, store, sink = runtime
    graph, _, _, search, counts = build(
        [task("db"), task("search", "fallback"), report(), AIMessage(content="内部金额100")],
        db_error=MissingConfiguration("database missing"),
    )
    await execute(graph)
    assert counts == dict(db=1, search=1) and DEGRADED_NOTICE in str(search.seen_messages[0])
    await sink.flush()
    rows = await store.list_results(context.run_id, thread_id=context.thread_id)
    failure = next(r for r in rows if r["source"] == "db")
    assert failure["check_status"] == "dependency_unavailable" and not failure["published"]
    final = await finalize_answer(context, "内部金额100")
    saved = (context.work_dir / "report.md").read_text(encoding="utf-8")
    assert final.startswith(DEGRADED_NOTICE) and saved.startswith(DEGRADED_NOTICE)
    assert "内部金额100" not in final + saved and EXTERNAL in final
    assert failure["result_id"] in final and context.workflow["db"] == "degraded"
    handoff = next(e for e in await store.list_events(context.run_id) if e["event"] == "result_handoff")
    assert handoff["data"]["result_ids"] == [] and handoff["data"]["db_failure_ids"] == [failure["result_id"]]


@pytest.mark.asyncio
async def test_model_claims_and_mutated_step_flags_cannot_replace_server_results(runtime):
    context, _, _ = runtime
    context.workflow = dict(db="passed", search="passed", reports={})
    graph, parent, _, search, counts = build([task("search"), AIMessage(content="done")])
    await execute(graph)
    assert counts["search"] == 0 and not search.seen_messages
    assert "database_required" in str(parent.seen_messages[-1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args",
    [
        ("generate_markdown", {"content": "fake", "filename": "x"}),
        ("write_file", {"file_path": "/x.md", "content": "fake"}),
        ("edit_file", {"file_path": "/x.md", "old_string": "x", "new_string": "y"}),
        ("convert_md_to_pdf", {"md_filename": "x"}),
    ],
)
async def test_all_report_write_entrypoints_block_before_business_function(runtime, name, args):
    context, _, _ = runtime
    executed = []
    result = await HookToolMiddleware().awrap_tool_call(
        SimpleNamespace(tool_call={"name": name, "args": args, "id": "output"}),
        RunnableLambda(lambda request: executed.append(request)).ainvoke,
    )
    assert result.status == "error" and "report_not_ready" in result.content and not executed
    assert context.call_counts["tool"] == 1


@pytest.mark.asyncio
async def test_default_framework_write_file_is_also_gated(runtime):
    context, _, _ = runtime
    graph, parent, _, _, _ = build(
        [
            call("write_file", {"file_path": "/early.md", "content": "fake"}, "early-file"),
            task("db"),
            task("search"),
            call("write_file", {"file_path": "/ready.md", "content": "ready"}, "ready-file"),
            AIMessage(content="done"),
        ]
    )
    await execute(graph)
    assert "report_not_ready" in str(parent.seen_messages[1])
    assert (
        not (context.work_dir / "early.md").exists()
        and (context.work_dir / "ready.md").read_text(encoding="utf-8") == "ready"
    )


@pytest.mark.asyncio
async def test_ordinary_report_only_requires_explicit_sources_and_full_requested_upload_read(runtime):
    context, _, _ = runtime
    context.business_mode = "ordinary"
    context.required_sources = ("search",)
    context.required_uploads = ("data.txt",)
    upload = context.work_dir / "uploads" / "data.txt"
    upload.parent.mkdir()
    upload.write_bytes(b"line1\r\nline2\r\n")
    graph, parent, _, _, counts = build(
        [
            report("before"),
            task("search"),
            call("read_file", {"file_path": "/uploads/data.txt", "offset": 0, "limit": 1}, "first-page"),
            report("partial"),
            call("read_file", {"file_path": "/uploads/data.txt", "offset": 1, "limit": 1}, "second-page"),
            report("ready"),
            AIMessage(content="done"),
        ]
    )
    await execute(graph)
    assert counts == dict(db=0, search=1)
    assert not (context.work_dir / "before.md").exists() and not (context.work_dir / "partial.md").exists()
    assert (context.work_dir / "ready.md").is_file() and "data.txt" in str(parent.seen_messages[4])
    assert await finalize_answer(context, "done") == "done"
    upload.write_bytes(b"changed\r\n")
    with pytest.raises(HookRejected, match="unread_uploads"):
        await finalize_answer(context, "old read no longer valid")


@pytest.mark.asyncio
async def test_workflow_requires_existing_uploads_and_business_reader_proof(runtime):
    context, _, _ = runtime
    upload = context.work_dir / "uploads" / "data.txt"
    upload.parent.mkdir()
    upload.write_bytes(b"input\r\n")
    graph, parent, _, _, _ = build(
        [
            task("db"),
            task("search"),
            report("unread"),
            call("read_file_content", {"filename": "uploads/data.txt"}, "read-upload"),
            report("read"),
            AIMessage(content="done"),
        ]
    )
    await execute(graph)
    assert not (context.work_dir / "unread.md").exists() and (context.work_dir / "read.md").is_file()
    assert "unread_uploads" in str(parent.seen_messages[3])


@pytest.mark.asyncio
async def test_parallel_search_waits_for_actual_db_completion(runtime):
    context, _, sink = runtime
    entered, release, blocked = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @tool
    async def slow_db():
        """Wait before returning actual fake internal data."""
        entered.set()
        await release.wait()
        return "total=20"

    sink.deliver = (
        lambda payload, thread: blocked.set()
        if payload["event"] == "workflow_state" and payload["status"] == "blocked"
        else None
    )
    batch = AIMessage(
        content="",
        tool_calls=[
            {"name": "task", "args": {"subagent_type": "db", "description": "query"}, "id": "db"},
            {"name": "task", "args": {"subagent_type": "search", "description": "query"}, "id": "too-early"},
        ],
    )
    graph, _, _, search, counts = build([batch, task("search", "after"), AIMessage(content="done")], db_probe=slow_db)
    running = asyncio.create_task(execute(graph))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        await asyncio.wait_for(blocked.wait(), timeout=10)
        assert counts["search"] == 0 and not search.seen_messages
    finally:
        release.set()
        await asyncio.wait_for(running, timeout=15)
    assert counts["search"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["main_model", "total"])
async def test_repeated_out_of_order_calls_consume_main_and_tool_budgets(runtime, limit):
    context, store, sink = runtime
    data = policy_for_run().model_dump()
    data["budgets"]["main_model" if limit == "main_model" else "total"] = 3 if limit == "main_model" else 6
    context.policy = RuntimePolicy.model_validate(data)
    graph, parent, _, search, counts = build([task("search", f"repeat-{i}") for i in range(6)])
    with pytest.raises(BudgetExceeded, match="main_model|Total"):
        await execute(graph)
    await sink.flush()
    assert len(parent.seen_messages) == context.call_counts["main_model"] == context.call_counts["tool"] == 3
    assert counts["search"] == 0 and not search.seen_messages
    assert len(await store.list_subtasks(context.run_id)) == 3


@pytest.mark.asyncio
async def test_unlinked_search_cannot_retroactively_satisfy_comparison_report(runtime):
    context, _, _ = runtime
    context.business_mode = "ordinary"
    graph, _, _, _, _ = build([task("search"), AIMessage(content="done")])
    await execute(graph)
    context.business_mode = "internal_external"
    graph, parent, _, _, _ = build([task("db"), report("unlinked"), AIMessage(content="done")])
    await execute(graph)
    assert "report_not_ready" in str(parent.seen_messages[2]) and not (context.work_dir / "unlinked.md").exists()


@pytest.mark.asyncio
async def test_storage_unavailable_and_corrupt_raw_source_fail_closed(runtime, monkeypatch):
    context, store, _ = runtime
    graph, _, _, _, _ = build([task("db"), AIMessage(content="done")])
    await execute(graph)
    original = store.get_result

    async def corrupt(*args):
        row = await original(*args)
        return dict(row, raw_text="modified")

    monkeypatch.setattr(store, "get_result", corrupt)
    graph, _, _, search, counts = build([task("search"), AIMessage(content="done")])
    with pytest.raises(EventPersistenceError, match="integrity"):
        await execute(graph)
    assert not search.seen_messages and counts["search"] == 0


@pytest.mark.asyncio
async def test_next_round_and_other_session_cannot_use_prior_db_result(runtime, sandbox_dir):
    context, store, sink = runtime
    graph, _, _, _, _ = build([task("db"), AIMessage(content="done")])
    await execute(graph)
    await sink.finish(context, "completed", answer="done")

    async def run(current):
        token = set_run_context(current)
        try:
            current.work_dir.mkdir()
            await sink.create(current.thread_id, "query", current.run_id)
            await sink.start(current)
            graph, _, _, search, counts = build([task("search"), AIMessage(content="done")])
            await execute(graph)
            assert counts["search"] == 0 and not search.seen_messages
        finally:
            reset_run_context(token)

    await asyncio.gather(
        *(
            run(
                RunContext(
                    name,
                    thread,
                    sandbox_dir / name,
                    event_sink=sink,
                    business_mode="internal_external",
                    skill_ids=("ecommerce-analysis",),
                )
            )
            for name, thread in (("next", context.thread_id), ("other", "other-session"))
        )
    )
    assert len(await store.list_subtasks("next")) == len(await store.list_subtasks("other")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"business_mode": "wrong"},
        {"business_mode": "internal_external"},
        {"business_mode": "internal_external", "skill_ids": ["report-writing"]},
        {"required_sources": ["unknown"]},
        {"required_sources": ["db", "db"]},
        {"required_uploads": ["../secret.txt"]},
        {"required_uploads": ["data.txt", "data.txt"]},
    ],
)
async def test_invalid_request_requirements_are_rejected_before_creating_run(sandbox_dir, monkeypatch, payload):
    async with service_runtime(sandbox_dir, monkeypatch) as (client, agent, _):
        response = await client.post("/api/task", json=dict(query="task", **payload))
        assert response.status_code == 422 and not agent.contexts
        from api import server

        assert await server.app.state.run_store.list_active() == []


@pytest.mark.asyncio
async def test_api_initializes_mode_and_blocks_premature_final_answer(sandbox_dir, monkeypatch):
    from api import server

    async def answer(*args):
        return "内部金额100"

    async with service_runtime(sandbox_dir, monkeypatch) as (client, _, _):
        monkeypatch.setattr(server, "run_deep_agent", answer)
        response = await client.post(
            "/api/task",
            json={"query": "compare", "business_mode": "internal_external", "skill_ids": ["ecommerce-analysis"]},
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        running = server.app.state.run_tasks[run_id]
        await asyncio.gather(running, return_exceptions=True)
        saved = await server.app.state.run_store.history(run_id)
        assert saved["run"]["status"] == "failed" and saved["run"]["answer"] is None
        assert "report_not_ready" in saved["run"]["error"]
        assert any(e["event"] == "workflow_state" and e["status"] == "initialized" for e in saved["events"])


@pytest.mark.asyncio
async def test_degraded_pdf_requires_controlled_markdown_and_edit_cannot_restore_invented_metrics(runtime):
    context, _, _ = runtime
    converted = []
    (context.work_dir / "unapproved.md").write_bytes(b"fake internal 100\r\n")

    @tool
    def convert_md_to_pdf(md_filename: str):
        """Offline conversion probe, never starts Office or an external process."""
        source = context.work_dir / (md_filename + ".md")
        assert source.read_text(encoding="utf-8").startswith(DEGRADED_NOTICE)
        converted.append(md_filename)
        return "converted"

    graph, parent, _, _, _ = build(
        [
            task("db"),
            task("search"),
            call("convert_md_to_pdf", {"md_filename": "unapproved"}, "bad-pdf"),
            report("controlled"),
            call("convert_md_to_pdf", {"md_filename": "controlled"}, "good-pdf"),
            call(
                "edit_file",
                {"file_path": "/controlled.md", "old_string": DEGRADED_NOTICE, "new_string": "内部100"},
                "edit",
            ),
            AIMessage(content="done"),
        ],
        db_error=MissingConfiguration("offline"),
        tools=[generate_markdown, convert_md_to_pdf],
    )
    await execute(graph)
    assert converted == ["controlled"]
    assert "degraded_source" in str(parent.seen_messages[3]) and "degraded_rewrite" in str(parent.seen_messages[-1])
    assert (context.work_dir / "controlled.md").read_text(encoding="utf-8").startswith(DEGRADED_NOTICE)


@pytest.mark.asyncio
async def test_workflow_without_durable_store_does_not_execute_expert(runtime):
    context, _, sink = runtime
    context.event_sink = None
    executed = []
    try:
        with pytest.raises(HookRejected, match="storage_required"):
            await DelegationRunnable(
                expert("search"),
                RunnableLambda(lambda state: executed.append(state) or {"messages": [AIMessage(content="fake")]}),
            ).ainvoke({"messages": [HumanMessage(content="search")]})
        assert not executed
    finally:
        context.event_sink = sink
