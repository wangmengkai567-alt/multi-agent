"""Durable job protocol: submit, ownership, cancellation, restart, input and expiry."""

import asyncio

import pytest

from agent.runtime.jobs import InputRequired, JobService


async def terminal(service, task_id, thread="thread", statuses=("completed", "failed", "cancelled", "input_required")):
    async with asyncio.timeout(3):
        while True:
            job = await service.get(task_id, thread)
            if job["status"] in statuses:
                return job
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_submission_is_immediate_idempotent_owned_and_not_completion(sandbox_dir):
    gate = asyncio.Event()
    calls = []

    async def handler(job):
        calls.append(job["task_id"])
        await gate.wait()
        return {"answer": "done"}

    service = await JobService.open(sandbox_dir / "jobs.db", {"research": handler}, sandbox_dir)
    try:
        first = await service.submit("thread", "run", "research", {"q": "city"}, "same", 30)
        second = await service.submit("thread", "run", "research", {"q": "city"}, "same", 30)
        assert first["status"] == "working" and first["result"] is None and first["poll_after_seconds"] > 0
        assert first["task_id"] == second["task_id"]
        assert await service.get(first["task_id"], "other") is None
        with pytest.raises(ValueError):
            await service.submit("thread", "run", "research", {"q": "different"}, "same", 30)
        gate.set()
        assert (await terminal(service, first["task_id"]))["result"] == {"answer": "done"}
        assert calls == [first["task_id"]]
        assert (await service.cancel(first["task_id"], "thread"))["status"] == "completed"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_shutdown_preserves_work_and_restart_recovers_it(sandbox_dir):
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def first_handler(job):
        entered.set()
        await gate.wait()

    path = sandbox_dir / "jobs.db"
    first = await JobService.open(path, {"research": first_handler}, sandbox_dir)
    job = await first.submit("thread", "run", "research", {}, "key", 30)
    await entered.wait()
    await first.close()

    async def recovered(job):
        return {"attempt": job["attempts"]}

    second = await JobService.open(path, {"research": recovered}, sandbox_dir)
    try:
        before = await second.get(job["task_id"], "thread")
        assert before["status"] == "working" and before["attempts"] == 1
        await second.recover()
        after = await terminal(second, job["task_id"])
        assert after["status"] == "completed" and after["result"]["attempt"] == 1
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_cancel_wins_against_late_result_and_is_idempotent(sandbox_dir):
    entered = asyncio.Event()

    async def stubborn(job):
        entered.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            return {"answer": "late"}

    service = await JobService.open(sandbox_dir / "jobs.db", {"research": stubborn}, sandbox_dir)
    try:
        job = await service.submit("thread", "run", "research", {}, "key", 30)
        await entered.wait()
        saved = await service.cancel(job["task_id"], "thread")
        assert saved["status"] == "cancelled" and saved["result"] is None
        assert (await service.cancel(job["task_id"], "thread"))["status"] == "cancelled"
        assert await service.cancel(job["task_id"], "other") is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_input_required_pauses_without_self_approval_then_resumes(sandbox_dir):
    async def handler(job):
        if not job.get("resume_input"):
            raise InputRequired("请选择分析范围")
        return {"scope": job["resume_input"]}

    service = await JobService.open(sandbox_dir / "jobs.db", {"research": handler}, sandbox_dir)
    try:
        job = await service.submit("thread", "run", "research", {}, "key", 30)
        paused = await terminal(service, job["task_id"])
        assert paused["status"] == "input_required" and paused["error"] == "请选择分析范围"
        with pytest.raises(ValueError):
            await service.resume(job["task_id"], "other", "all")
        await service.resume(job["task_id"], "thread", "all")
        done = await terminal(service, job["task_id"])
        assert done["status"] == "completed" and done["result"] == {"scope": "all"}
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_expiry_produces_explicit_failure(sandbox_dir):
    async def handler(job):
        await asyncio.sleep(30)

    service = await JobService.open(sandbox_dir / "jobs.db", {"research": handler}, sandbox_dir)
    try:
        job = await service.submit("thread", "run", "research", {}, "key", 0.03)
        done = await terminal(service, job["task_id"])
        assert done["status"] == "failed" and "预算耗尽" in done["error"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_two_workers_cannot_claim_one_job(sandbox_dir):
    gate = asyncio.Event()
    entered = asyncio.Event()
    calls = []

    async def handler(job):
        calls.append(job["task_id"])
        entered.set()
        await gate.wait()
        return {"answer": "one execution"}

    path = sandbox_dir / "shared.db"
    first = await JobService.open(path, {"research": handler}, sandbox_dir)
    second = await JobService.open(path, {"research": handler}, sandbox_dir)
    try:
        job = await first.submit("thread", "run", "research", {}, "same", 30)
        await entered.wait()
        await second.recover()
        await asyncio.sleep(0.03)
        assert calls == [job["task_id"]]
        gate.set()
        assert (await terminal(first, job["task_id"]))["status"] == "completed"
    finally:
        await second.close()
        await first.close()


@pytest.mark.asyncio
async def test_http_background_returns_task_id_and_owned_result(sandbox_dir, monkeypatch):
    from test_run_concurrency import server, service_runtime

    gate = asyncio.Event()

    async def handler(job):
        await gate.wait()
        return {"answer": "晋城与新乡的已验证对比"}

    async with service_runtime(sandbox_dir, monkeypatch) as (client, _, _):
        server.app.state.job_service.handlers["research"] = handler
        response = await client.post("/api/task", json={"query": "比较两个城市", "execution_mode": "background"})
        assert response.status_code == 200
        receipt = response.json()
        assert receipt["status"] == "working" and receipt["task_id"] and receipt["poll_after_seconds"] > 0
        job_id, thread = receipt["task_id"], receipt["thread_id"]
        assert (await client.get(f"/api/jobs/{job_id}", params={"thread_id": "other"})).status_code == 404
        assert (await client.post(f"/api/jobs/{job_id}/cancel", params={"thread_id": "other"})).status_code == 404
        assert (await client.get(f"/api/runs/{receipt['run_id']}/jobs")).json()["jobs"][0]["status"] == "working"
        followup = (await client.post("/api/task", json={"query": "完成了吗？", "thread_id": thread})).json()
        assert followup["task_id"] == job_id
        assert len(await server.app.state.job_service.list_thread(thread)) == 1
        observed = (await client.get(f"/api/runs/{followup['run_id']}/jobs")).json()
        assert observed["jobs"][0]["task_id"] == job_id
        gate.set()
        job = await terminal(server.app.state.job_service, job_id, thread)
        assert job["status"] == "completed" and job["result"]["answer"]
        assert (
            await client.post(f"/api/jobs/{job_id}/resume", json={"thread_id": thread, "user_input": "yes"})
        ).status_code == 409


@pytest.mark.asyncio
async def test_followup_inherits_actual_goal_for_execution_and_review(sandbox_dir, monkeypatch):
    from test_run_concurrency import server, service_runtime

    async with service_runtime(sandbox_dir, monkeypatch) as (client, _, _):
        sink = server.app.state.event_sink
        from agent.runtime.context import RunContext

        await sink.create("thread", "比较晋城与新乡", "original")
        await sink.finish(RunContext("original", "thread", sandbox_dir), "completed", answer="调查计划")
        assert await server.resolve_user_goal("thread", "完成了吗？") == "比较晋城与新乡"
        assert await server.resolve_user_goal("thread", "请分析另一个问题") == "请分析另一个问题"


@pytest.mark.asyncio
async def test_worker_that_lost_lease_cannot_publish_a_late_result(sandbox_dir):
    entered, gate = asyncio.Event(), asyncio.Event()

    async def handler(job):
        entered.set()
        await gate.wait()
        return {"answer": "stale"}

    service = await JobService.open(sandbox_dir / "lease.db", {"research": handler}, sandbox_dir)
    try:
        job = await service.submit("thread", "run", "research", {}, "key", 30)
        await entered.wait()
        async with service.lock:
            await service.connection.execute("UPDATE jobs SET owner='replacement' WHERE task_id=?", (job["task_id"],))
            await service.connection.commit()
        task = service.tasks[job["task_id"]]
        gate.set()
        await task
        saved = await service.get(job["task_id"], "thread")
        assert saved["status"] == "working" and saved["result"] is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_timed_out_job_cannot_report_late_completion(sandbox_dir):
    async def handler(job):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            return {"answer": "late"}

    service = await JobService.open(sandbox_dir / "late.db", {"research": handler}, sandbox_dir)
    try:
        job = await service.submit("thread", "run", "research", {}, "key", 0.03)
        saved = await terminal(service, job["task_id"])
        assert saved["status"] == "failed" and saved["result"] is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_long_pdf_conversion_uses_snapshot_and_publishes_once(sandbox_dir, monkeypatch):
    import json

    from agent.runtime.artifact_jobs import convert_document, submit_conversion
    from agent.runtime.context import RunContext
    from agent.runtime.policies import load_runtime_policy
    from utils import word_converter

    calls = []

    def convert(source, target):
        calls.append(source.read_bytes())
        target.write_bytes(b"%PDF-1.4 verified test")
        return "done"

    monkeypatch.setattr(word_converter, "convert_md_to_pdf_via_word", convert)
    root = sandbox_dir / "output"
    directory = root / "session" / "run"
    directory.mkdir(parents=True)
    source = directory / "report.md"
    source.write_bytes("# 中文报告\r\n".encode())
    target = directory / "report.pdf"
    service = await JobService.open(sandbox_dir / "pdf.db", {}, root / "background")
    service.handlers["pdf_conversion"] = lambda job: convert_document(job, service, root)
    context = RunContext("run", "thread", directory, policy=load_runtime_policy(), job_service=service)
    try:
        first = json.loads(await submit_conversion(context, source, target))
        second = json.loads(await submit_conversion(context, source, target))
        assert first["task_id"] == second["task_id"] and first["status"] == "working"
        saved = await terminal(service, first["task_id"])
        assert saved["status"] == "completed" and target.read_bytes().startswith(b"%PDF")
        assert calls == [source.read_bytes()]
        assert saved["result"]["artifacts"][0]["name"] == "report.pdf"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancelled_pdf_conversion_never_publishes_late_file(sandbox_dir, monkeypatch):
    import base64
    import threading
    from hashlib import sha256

    from agent.runtime.artifact_jobs import convert_document
    from utils import word_converter

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def convert(source, target):
        entered.set()
        release.wait(3)
        target.write_bytes(b"%PDF late stage")
        finished.set()
        return "done"

    monkeypatch.setattr(word_converter, "convert_md_to_pdf_via_word", convert)
    root = sandbox_dir / "output"
    work = root / "run"
    work.mkdir(parents=True)
    service = await JobService.open(sandbox_dir / "pdf.db", {}, root / "background")
    service.handlers["pdf_conversion"] = lambda job: convert_document(job, service, root)
    raw = b"# report"
    payload = dict(
        source_base64=base64.b64encode(raw).decode(),
        source_sha256=sha256(raw).hexdigest(),
        work_dir=str(work),
        output_relative="report.pdf",
    )
    try:
        job = await service.submit("thread", "run", "pdf_conversion", payload, "key", 30)
        assert await asyncio.to_thread(entered.wait, 2)
        await service.cancel(job["task_id"], "thread")
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        assert not (work / "report.pdf").exists()
        assert (await service.get(job["task_id"], "thread"))["status"] == "cancelled"
    finally:
        release.set()
        await service.close()
