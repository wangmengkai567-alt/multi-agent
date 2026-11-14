"""Cooperative tool deadlines; blocking providers run in owned, killable processes."""

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, RLock
from time import monotonic

from agent.runtime.context import get_run_context
from agent.runtime.errors import StageEnded
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
        if self.stage_deadline is not None:
            self.deadline = (
                min(self.deadline, self.stage_deadline) if self.deadline is not None else self.stage_deadline
            )
        self.cancelled, self.lock, self.workers = Event(), RLock(), set()

    def check(self):
        context = get_run_context()
        if context is not None:
            context.check_active()
        if self.stage_deadline is not None and monotonic() >= self.stage_deadline:
            raise StageEnded("当前分工取证窗口已到，不再追加工具调用；保留已有证据交接")
        if self.cancelled.is_set() or self.deadline is not None and monotonic() >= self.deadline:
            raise ToolCallTimeout(self.name, self.seconds or 0)

    def attach(self, worker):
        with self.lock:
            self.check()
            self.workers.add(worker)

    def detach(self, worker):
        with self.lock:
            self.workers.discard(worker)

    def close(self):
        self.cancelled.set()
        with self.lock:
            workers = list(self.workers)
        failure = None
        for worker in workers:
            try:
                worker.close()
            except Exception as error:
                failure = error
        if failure is not None:
            raise failure


# 在工具执行前后检查期限，并在退出时关闭该调用持有的子进程。
@contextmanager
def bounded_tool(name, seconds):
    scope = ToolScope(name, seconds)
    token = active_tool_scope.set(scope)
    try:
        scope.check()
        yield scope
        scope.check()
    finally:
        try:
            scope.close()
        finally:
            active_tool_scope.reset(token)
