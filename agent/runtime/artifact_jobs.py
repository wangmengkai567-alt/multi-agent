"""Durable local document conversion: immutable input, staged output, owned publication."""

import asyncio
import base64
import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4


async def submit_conversion(context, source, target):
    from tools.job_tools import public_job

    raw = source.read_bytes()
    if len(raw) > context.policy.files.max_upload_bytes:
        raise ValueError("文档超过转换输入大小上限")
    relative = target.relative_to(context.work_dir.resolve()).as_posix()
    digest = sha256(raw).hexdigest()
    payload = dict(
        source_base64=base64.b64encode(raw).decode("ascii"),
        source_sha256=digest,
        conversion_version=1,
        output_relative=relative,
    )
    key = context.run_id + ":pdf:" + sha256((digest + relative).encode()).hexdigest()
    job = await context.job_service.submit(
        context.thread_id, context.run_id, "pdf_conversion", payload, key, context.policy.timeouts.job_seconds
    )
    context.pending_jobs[job["task_id"]] = public_job(job)
    return json.dumps(public_job(job), ensure_ascii=False)


def submit_conversion_sync(context, source, target):
    try:
        same_loop = asyncio.get_running_loop() is context.event_sink.loop
    except RuntimeError:
        same_loop = False
    if same_loop:
        raise RuntimeError("同步转换入口必须在工具线程调用")
    future = asyncio.run_coroutine_threadsafe(submit_conversion(context, source, target), context.event_sink.loop)
    return future.result(timeout=min(25, context.remaining() or 25))


async def convert_document(job, service, output_root):
    from agent.runtime.context import RunContext, reset_run_context, set_run_context
    from agent.runtime.policies import load_runtime_policy

    context = RunContext(
        job["task_id"],
        job["thread_id"],
        service.directory / job["task_id"],
        owner_id=job["resource_owner_id"],
        deadline=datetime.fromisoformat(job["expires_at"]),
        policy=load_runtime_policy(),
        background_job=True,
    )
    service.contexts[job["task_id"]] = context
    token = set_run_context(context)
    outcome = "failed"
    try:
        result = await _convert_document(job, service, output_root)
        outcome = "completed"
        return result
    except asyncio.CancelledError:
        outcome = "interrupted" if service.closing else "cancelled"
        raise
    finally:
        context.stop(outcome)
        reset_run_context(token)
        service.contexts.pop(job["task_id"], None)


async def _convert_document(job, service, output_root):
    from utils.secure_paths import resource_path
    from utils.word_converter import convert_md_to_pdf_via_word

    if job["payload"].get("conversion_version") != 1:
        raise ValueError("Conversion task requires the current resource ownership protocol")
    run = await service.run_store.get(job["run_id"])
    if not run or run["thread_id"] != job["thread_id"] or job["resource_owner_id"] != service.resource_owner_id:
        raise ValueError("Conversion parent run ownership mismatch")
    directory = service.directory / job["task_id"]
    directory.mkdir(parents=True, exist_ok=True)
    raw = base64.b64decode(job["payload"]["source_base64"], validate=True)
    if sha256(raw).hexdigest() != job["payload"]["source_sha256"]:
        raise ValueError("持久化转换输入校验失败")
    source = directory / "input.md"
    if source.exists() and source.read_bytes() != raw:
        raise ValueError("持久化转换输入已被更改")
    if not source.exists():
        source.write_bytes(raw)
    stage = directory / ("stage-" + uuid4().hex[:12] + ".pdf")
    work_dir = resource_path(run["workspace"], output_root)
    target = resource_path(job["payload"]["output_relative"], work_dir)
    if (
        target.suffix.lower() != ".pdf"
        or target.is_relative_to(work_dir / "uploads")
        or target.is_relative_to(work_dir / "results")
    ):
        raise ValueError("转换输出必须是本轮可写 PDF 路径")
    # Cancelling the await never publishes a late conversion: only this coroutine commits output.
    await asyncio.to_thread(convert_md_to_pdf_via_word, source, stage)
    if not stage.is_file() or stage.stat().st_size == 0:
        raise RuntimeError("文档转换未产生有效 PDF")
    if datetime.fromisoformat(job["expires_at"]) <= datetime.now(UTC):
        raise TimeoutError("转换总时长预算耗尽")
    result = {
        "answer": "PDF 转换已完成，可下载文件。",
        "artifacts": [{"name": target.name, "relative_path": target.relative_to(work_dir).as_posix()}],
    }
    async with service.lock:
        try:
            await service.connection.execute("BEGIN IMMEDIATE")
            async with service.connection.execute(
                "SELECT owner,status FROM jobs WHERE task_id=? AND resource_owner_id=?",
                (job["task_id"], service.resource_owner_id),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None or row["owner"] != service.owner or row["status"] != "working":
                raise RuntimeError("转换任务已取消或执行权已转移，未发布迟到文件")
            target.parent.mkdir(parents=True, exist_ok=True)
            stage.replace(target)
            await service.connection.execute(
                "UPDATE jobs SET status='completed',result=?,updated_at=?,owner=NULL,lease_until=NULL WHERE task_id=? AND owner=?",
                (json.dumps(result, ensure_ascii=False), datetime.now(UTC).isoformat(), job["task_id"], service.owner),
            )
            await service.connection.commit()
        except BaseException:
            await service.connection.rollback()
            raise
    return result
