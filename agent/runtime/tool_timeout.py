"""Cooperative tool deadlines; blocking providers run in owned, killable processes."""

import asyncio
from concurrent.futures import Future, wait
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from threading import Event, Lock
from time import monotonic

from agent.runtime.context import get_run_context
from agent.runtime.errors import StageCleanupFailed, StageEnded
from agent.runtime.recovery import ToolCallTimeout

active_tool_scope = ContextVar("active_tool_scope", default=None)


# 管理单次工具调用的截止时间和子进程；seconds 为秒，None 表示无单次截止时间。
class ToolScope:
    def __init__(self, name, seconds):
        self.name, self.seconds = name, seconds
        self.deadline = monotonic() + seconds if seconds is not None else None
        from agent.runtime.hooks import active_call
        from agent.runtime.stage_budget import current_stage

        context = get_run_context()
        parent = active_tool_scope.get()
        if parent is not None:
            parent.check()
            if parent.deadline is not None:
                self.deadline = min(self.deadline, parent.deadline) if self.deadline is not None else parent.deadline
        call = active_call.get()
        admitted = call.workflow_data.get("admitted_deadline") if call and call.actor == name else None
        if admitted is not None:
            self.deadline = min(self.deadline, admitted) if self.deadline is not None else admitted
        stage = current_stage(context) if context else None
        self.stage_deadline = stage["tool_deadline"] if stage else None
        self.stage_cleanup_deadline = stage["deadline"] if stage else None
        if self.stage_deadline is not None:
            self.deadline = (
                min(self.deadline, self.stage_deadline) if self.deadline is not None else self.stage_deadline
            )
        self.cancelled, self.lock, self.workers = Event(), Lock(), []

    def check(self):
        context = get_run_context()
        if context is not None:
            context.check_active()
        if self.stage_deadline is not None and monotonic() >= self.stage_deadline:
            raise StageEnded("当前分工取证窗口已到，不再追加工具调用；保留已有证据交接")
        if self.cancelled.is_set() or self.deadline is not None and monotonic() >= self.deadline:
            raise ToolCallTimeout(self.name, self.seconds or 0)

    def register_worker(self):
        with self.lock:
            self.check()
            completion = Future()
            self.workers.append(completion)
            return completion

    def _stop_workers(self):
        with self.lock:
            self.cancelled.set()
            return tuple(self.workers)

    def _cleanup_deadline(self):
        from agent.runtime.policies import policy_for_run

        context = get_run_context()
        now = monotonic()
        remaining = context.remaining()
        return min(
            limit
            for limit in (
                now + policy_for_run().timeouts.shutdown_seconds,
                self.stage_cleanup_deadline,
                now + remaining if remaining is not None else None,
            )
            if limit is not None
        )

    @staticmethod
    def _confirm_cleanup(workers):
        for completion in workers:
            if not completion.done():
                raise StageCleanupFailed("工具工作进程未在清理时限内退出")
            if error := completion.result():
                raise error

    def close(self):
        workers = self._stop_workers()
        pending = [worker for worker in workers if not worker.done()]
        if pending:
            wait(pending, timeout=max(0.0, self._cleanup_deadline() - monotonic()))
        self._confirm_cleanup(workers)

    async def aclose(self):
        workers = self._stop_workers()
        pending = [asyncio.wrap_future(worker) for worker in workers if not worker.done()]
        try:
            if pending:
                deadline = self._cleanup_deadline()
                try:
                    await asyncio.wait(pending, timeout=max(0.0, deadline - monotonic()))
                except asyncio.CancelledError:
                    # 阶段取消时，同一截止时间内仍需确认进程已退出。
                    await asyncio.wait(pending, timeout=max(0.0, deadline - monotonic()))
                    raise
        finally:
            self._confirm_cleanup(workers)


@contextmanager
def _active_scope(name, seconds):
    scope = ToolScope(name, seconds)
    token = active_tool_scope.set(scope)
    try:
        yield scope
    finally:
        active_tool_scope.reset(token)


@contextmanager
def bounded_tool(name, seconds):
    with _active_scope(name, seconds) as scope:
        try:
            scope.check()
            yield scope
            scope.check()
        finally:
            scope.close()


@asynccontextmanager
async def bounded_tool_async(name, seconds):
    with _active_scope(name, seconds) as scope:
        try:
            scope.check()
            yield scope
            scope.check()
        finally:
            await scope.aclose()
