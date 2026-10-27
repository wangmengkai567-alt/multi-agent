"""Retained provenance, permissions, bounded resources and real expert failure boundaries."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from test_basic_coordination import providers  # noqa: F401

from agent.reflection import tool_evidence
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.contracts import bind_contract
from agent.runtime.delegation import DelegationRunnable
from agent.runtime.evidence_protocol import EvidenceError, make_record, source_rows, validate_record
from agent.runtime.hooks import HookCall, HookToolMiddleware
from agent.runtime.policies import PolicyError, load_runtime_policy
from agent.runtime.recovery import ToolCallTimeout
from agent.subagent_registry import ExecutionConfig, SubagentRegistry, SubagentSpec, load_default_registry
from tools import tavily_tool
from tools.upload_file_read_tool import read_document


@pytest.fixture
def context(sandbox_dir):
    policy = load_runtime_policy()
    policy = policy.model_copy(
        update={
            "scheduler": policy.scheduler.model_copy(
                update={"models": policy.scheduler.models.model_copy(update={"requests_per_second": 0.0})}
            )
        }
    )
    context = RunContext("boundary", "thread", sandbox_dir, user_query="Investigate", policy=policy)
    bind_contract(context)
    token = set_run_context(context)
    try:
        yield context
    finally:
        reset_run_context(token)


def source(context, task="owned", url="https://example.test/real", status="completed"):
    context.task_results.setdefault(task, {})
    call = HookCall(
        "search:internet_search",
        "real-call",
        task_id=task,
        evidence_adapter="web-search.v1",
        executed=True,
        status=status,
    )
    payload = {"results": [{"url": url, "title": "Result", "content": "Actual result"}]}
    return make_record(call, json.dumps(payload), payload)


def test_failed_sources_are_never_citable_and_source_integrity_is_checked(context):
    failed = source(context, status="failed")
    good = source(context)
    assert len(list(source_rows([failed, good]))) == 1
    good["sources"][0]["url"] = "https://example.test/forged"
    with pytest.raises(EvidenceError):
        validate_record(good, context)


def test_same_thread_other_run_cannot_borrow_sources(context):
    row = source(context)
    other = RunContext("other", "thread", context.work_dir, policy=context.policy)
    token = set_run_context(other)
    try:
        with pytest.raises(EvidenceError, match="run"):
            validate_record(row, other)
    finally:
        reset_run_context(token)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/private",
        "http://localhost/private",
        "file:///private",
        "https://user:pass@example.test/",
        "https://foreign.test/path",
    ],
)
def test_page_reader_denies_unobserved_and_nonpublic_origins(context, url):
    token = tool_evidence.set([source(context)])
    try:
        with pytest.raises(PolicyError):
            tavily_tool.validate_page_request(url, "question")
    finally:
        tool_evidence.reset(token)


def test_discovered_source_id_resolves_only_inside_own_task(context):
    first = [source(context, "first", "https://one.test/path")]
    second = [source(context, "second", "https://two.test/path")]
    from agent.runtime.retrieval import source_key

    token = tool_evidence.set(first)
    try:
        assert (
            tavily_tool.validate_page_request(source_key("https://one.test/path"), "facts") == "https://one.test/path"
        )
    finally:
        tool_evidence.reset(token)
    token = tool_evidence.set(second)
    try:
        with pytest.raises(PolicyError):
            tavily_tool.validate_page_request("https://one.test/path", "facts")
    finally:
        tool_evidence.reset(token)


def test_changed_upload_is_rejected_before_document_worker(context, monkeypatch):
    upload = context.work_dir / "uploads" / "source.txt"
    upload.parent.mkdir()
    upload.write_bytes(b"Original")
    context.task_contract = None
    bind_contract(context)
    upload.write_bytes(b"Changed")
    calls = []
    monkeypatch.setattr("tools.upload_file_read_tool._read", lambda *a, **k: calls.append(1))
    with pytest.raises(ValueError, match="changed"):
        from agent.runtime.contracts import assert_contract

        assert_contract(context)
        read_document.func("uploads/source.txt")
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["cancel", "error", "timeout"])
async def test_expert_failure_releases_all_leases_and_never_publishes_late_result(context, outcome):
    entered = asyncio.Event()

    async def execute(state):
        entered.set()
        if outcome == "error":
            raise OSError("controlled tool failure")
        if outcome == "timeout":
            raise TimeoutError("controlled model timeout")
        await asyncio.Event().wait()
        return {"messages": [AIMessage(content="late")]}

    spec = SubagentSpec("search", "Search", "Investigate", "Use actual results", ())
    task = asyncio.create_task(
        DelegationRunnable(spec, RunnableLambda(execute)).ainvoke({"messages": [HumanMessage(content="task")]})
    )
    await asyncio.wait_for(entered.wait(), 2)
    if outcome == "cancel":
        context.stop("cancelled")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert json.loads(result["messages"][-1].text)["status"] == "failed"
    assert len(context.task_results) == 1
    row = next(iter(context.task_results.values()))
    assert row["status"] in ("failed", "cancelled") and row["result"] is None
    assert all(value["active"] == value["queued"] == 0 for value in context.scheduler.snapshot().values())


@pytest.mark.asyncio
async def test_tool_request_deadline_cancels_handler_and_records_no_success(context):
    context.policy = context.policy.model_copy(
        update={"timeouts": context.policy.timeouts.model_copy(update={"tool_seconds": 0.02})}
    )
    stopped = asyncio.Event()

    async def execute(request):
        try:
            await asyncio.sleep(1)
        finally:
            stopped.set()
        return ToolMessage(content="late", tool_call_id="tool")

    with pytest.raises(ToolCallTimeout):
        await HookToolMiddleware().awrap_tool_call(
            SimpleNamespace(tool_call={"name": "probe", "args": {}, "id": "tool"}), execute
        )
    assert stopped.is_set() and context.stop_status is None


def test_registry_retains_only_registered_tools_and_explicit_network_permissions():
    registry = load_default_registry()
    assert set(registry.specs) == {"db", "document", "search"}
    assert not registry.get("db").execution.network_access and not registry.get("document").execution.network_access
    with pytest.raises(ValueError):
        SubagentRegistry([replace(registry.get("search"), execution=ExecutionConfig())])
    with pytest.raises(ValueError):
        SubagentRegistry([registry.get("db"), registry.get("db")])


def test_search_excerpt_is_bounded_without_discarding_original_payload(context):
    payload = {"results": [{"url": "https://example.test/full", "title": "Full", "content": "actual " * 5000}]}
    compact = json.loads(tavily_tool.compact_search_response(payload, context.policy.search))
    assert len(compact["results"][0]["content"]) <= context.policy.search.result_chars
    assert compact["results"][0]["excerpt_truncated"]
    assert len(payload["results"][0]["content"]) == 35000


def test_provider_progress_is_authorized_once_and_second_send_is_rejected(context, monkeypatch):
    callbacks = []

    def execute(name, payload, *, seconds, on_event):
        callbacks.append(on_event)
        on_event({"event": "progress", "kind": "transport"})
        with pytest.raises(PolicyError, match="implicit retries"):
            on_event({"event": "progress", "kind": "transport"})
        return {"results": []}

    monkeypatch.setattr("agent.runtime.workers.run_operation", execute)
    assert tavily_tool.provider_operation("search", "fake-key", context.policy.search, query="facts") == {"results": []}
    assert len(callbacks) == 1
    assert all(value["active"] == value["queued"] == 0 for value in context.scheduler.snapshot().values())


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows Job Object containment")
def test_actual_windows_worker_close_reaps_descendants(context):
    import sys
    from time import monotonic, sleep

    import win32api
    import win32con
    import win32event

    from agent.runtime.processes import OwnedProcess

    marker = context.work_dir / "worker-ready.json"
    code = "import pathlib,subprocess,sys,time; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)"
    owned = OwnedProcess([sys.executable, "-c", code, str(marker)], cwd=context.work_dir)
    handle = None
    try:
        deadline = monotonic() + 5
        while not marker.is_file() and monotonic() < deadline:
            sleep(0.02)
        assert marker.is_file(), "Worker failed to create its child"
        handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, int(marker.read_text()))
        assert win32event.WaitForSingleObject(handle, 0) == win32con.WAIT_TIMEOUT
        owned.close()
        assert owned.process.poll() is not None
        assert win32event.WaitForSingleObject(handle, 1000) == win32con.WAIT_OBJECT_0
    finally:
        owned.close()
        if handle is not None:
            handle.Close()
        owned.process.stdin.close()
        owned.process.stdout.close()
