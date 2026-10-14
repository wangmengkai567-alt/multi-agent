"""Run storage, terminal guards, and per-round context/file integration."""

import asyncio
from datetime import UTC, datetime

import aiosqlite
import pytest
import pytest_asyncio

from agent.runtime.context import RunContext, get_run_context, reset_run_context, set_run_context
from api.context import get_session_context, get_thread_context
from api.run_store import RunConflict, RunStore
from utils.path_utils import resolve_path


@pytest_asyncio.fixture
async def store(sandbox_dir):
    result = await RunStore.open(sandbox_dir / "runs.db")
    try:
        yield result
    finally:
        await result.close()


@pytest.mark.asyncio
async def test_run_record_persists_across_reopen(sandbox_dir):
    path = sandbox_dir / "runs.db"
    store = await RunStore.open(path)
    try:
        run = await store.create("conversation", "question with 'quotes' and 中文")
        assert run["status"] == "queued" and run["ended_at"] is None
        assert run["answer"] is None and run["error"] is None
        assert datetime.fromisoformat(run["created_at"]).tzinfo is not None
        assert await store.start(run["run_id"])
        assert not await store.start(run["run_id"])
        assert await store.finish(run["run_id"], "completed", answer="saved answer")
    finally:
        await store.close()
    store = await RunStore.open(path)
    try:
        saved = await store.get(run["run_id"])
        assert saved["question"] == "question with 'quotes' and 中文"
        assert saved["thread_id"] == "conversation" and saved["answer"] == "saved answer"
        assert saved["status"] == "completed" and saved["ended_at"] is not None
        assert await store.get("missing") is None
        async with store.connection.execute("PRAGMA user_version") as cursor:
            assert (await cursor.fetchone())[0] == 2
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "interrupted"])
async def test_terminal_state_rejects_late_writes(store, status):
    run = await store.create("same-session", "first question")
    assert await store.finish(run["run_id"], status, answer="first answer", error="first error")
    saved = await store.get(run["run_id"])
    second = await store.create("same-session", "second question")
    assert second["run_id"] != run["run_id"]
    for late_status in ("completed", "failed", "cancelled", "interrupted"):
        assert not await store.finish(run["run_id"], late_status, answer="late answer", error="late error")
    assert not await store.start(run["run_id"])
    assert await store.get(run["run_id"]) == saved
    assert (await store.get(second["run_id"]))["status"] == "queued"


@pytest.mark.asyncio
async def test_atomic_store_admission(store):
    results = await asyncio.gather(*(store.create("same", str(i)) for i in range(10)), return_exceptions=True)
    admitted = [result for result in results if isinstance(result, dict)]
    rejected = [result for result in results if isinstance(result, RunConflict)]
    assert len(admitted) == 1 and len(rejected) == 9
    assert all(error.run_id == admitted[0]["run_id"] for error in rejected)


@pytest.mark.asyncio
async def test_finish_validation_and_unknown_id(store):
    with pytest.raises(ValueError, match="terminal"):
        await store.finish("id", "running")
    with pytest.raises(ValueError, match="answer"):
        await store.finish("id", "completed")
    assert not await store.finish("missing", "failed", error="unknown")


@pytest.mark.asyncio
async def test_future_schema_is_rejected_without_deleting_data(sandbox_dir):
    path = sandbox_dir / "future.db"
    async with aiosqlite.connect(path) as connection:
        await connection.execute("CREATE TABLE preserved (value TEXT)")
        await connection.execute("INSERT INTO preserved VALUES ('keep me')")
        await connection.execute("PRAGMA user_version = 9")
        await connection.commit()
    with pytest.raises(RuntimeError, match="schema version: 9"):
        await RunStore.open(path)
    async with aiosqlite.connect(path) as connection:
        async with connection.execute("SELECT value FROM preserved") as cursor:
            assert (await cursor.fetchone())[0] == "keep me"


@pytest.mark.asyncio
async def test_context_propagates_to_tool_thread_and_resets(sandbox_dir):
    context = RunContext("round", "session", sandbox_dir / "run_round", deadline=datetime.now(UTC))
    token = set_run_context(context)
    try:
        assert get_session_context() == str(context.work_dir)
        assert get_thread_context() == "session"
        assert await asyncio.to_thread(get_run_context) is context
        context.call_counts["tool"] += 1
        other = RunContext("round2", "session2", sandbox_dir / "run_round2")
        assert other.call_counts["tool"] == 0
        assert context.deadline is not None
    finally:
        reset_run_context(token)
    assert get_run_context() is None
    assert get_session_context() is None and get_thread_context() is None


@pytest.mark.asyncio
async def test_real_file_tools_use_this_run_directory(sandbox_dir):
    from tools.markdown_tools import generate_markdown
    from tools.upload_file_read_tool import read_file_content

    run_dir = sandbox_dir / "output" / "session_s" / "run_r"
    token = set_run_context(RunContext("r", "s", run_dir))
    try:
        result = await asyncio.to_thread(
            generate_markdown.invoke,
            {
                "content": "# 本轮文档",
                "filename": "report",
                "path": "output/session_s/run_r/nested",
            },
        )
        assert "已成功生成" in result
        assert (run_dir / "nested" / "report.md").read_text(encoding="utf-8") == "# 本轮文档"
        assert read_file_content.invoke({"filename": "nested/report.md"}) == "# 本轮文档"
        assert resolve_path("/workspace/nested/report.md") == str(run_dir / "nested" / "report.md")
        from utils.path_utils import PathDenied

        with pytest.raises(PathDenied, match="traversal"):
            resolve_path("../run_other/report.md")
        with pytest.raises(PathDenied, match="allowed directory"):
            resolve_path(str(sandbox_dir / "outside.md"))
    finally:
        reset_run_context(token)


@pytest.mark.asyncio
async def test_shutdown_interrupts_only_active_records(store):
    finished = await store.create("done", "done question")
    await store.finish(finished["run_id"], "completed", answer="done")
    first = await store.create("queued", "queued question")
    second = await store.create("running", "running question")
    await store.start(second["run_id"])
    await store.interrupt_active("service exit")
    for record in (first, second):
        result = await store.get(record["run_id"])
        assert result["status"] == "interrupted" and result["error"] == "service exit"
        assert result["ended_at"]
    assert (await store.get(finished["run_id"]))["answer"] == "done"
