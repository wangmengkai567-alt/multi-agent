"""Own the acquisition deadline and a single transition into final reporting."""

import asyncio

from agent.runtime.context import collection_active
from agent.runtime.errors import CollectionEnded, ExecutionTerminated


def _consume(task):
    if not task.cancelled():
        task.exception()


async def run_phases(context, collect, report):
    context.check_active()
    remaining = context.collection_remaining()
    if context.report_started or remaining is not None and remaining <= 0:
        context.start_report("取证阶段时限已到", collection_timed_out=remaining is not None and remaining <= 0)
        return await report()

    async def collecting():
        token = collection_active.set(True)
        try:
            return await collect()
        finally:
            collection_active.reset(token)

    task = asyncio.create_task(collecting(), name=f"collection:{context.run_id}")
    task.add_done_callback(_consume)
    answer = None
    try:
        while True:
            wait = context.remaining() if context.report_started else context.collection_remaining()
            done, _ = await asyncio.wait({task}, timeout=None if wait is None else max(0, wait))
            if done:
                try:
                    answer = task.result()
                except CollectionEnded:
                    pass  # Explicit phase boundary, never a retry of an unexpected model/tool failure.
                break
            context.check_active()
            if context.report_started:
                continue  # A final answer already in flight may use the reserved report interval.
            break
    except asyncio.CancelledError:
        context.stop("cancelled")
        raise
    finally:
        if not task.done():
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=context.policy.timeouts.shutdown_seconds)
            if not done:
                error = ExecutionTerminated("取证执行未在清理时限内停止，不能启动最终报告")
                context.terminate(error)
                raise error
        if not task.cancelled():
            error = task.exception()
            if error is not None and not isinstance(error, CollectionEnded):
                raise error
    context.check_active()
    context.start_report(
        "取证阶段已完成" if answer is not None else "取证阶段时限已到", collection_timed_out=answer is None
    )
    if answer is not None:
        return answer
    return await report()
