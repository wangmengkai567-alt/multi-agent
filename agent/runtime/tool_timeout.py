"""Cooperative tool deadlines; blocking providers run in owned, killable processes."""

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, RLock
from time import monotonic

from agent.runtime.context import get_run_context
from agent.runtime.recovery import ToolCallTimeout

active_tool_scope = ContextVar("active_tool_scope", default=None)


class ToolScope:
    def __init__(self, name, seconds):
        self.name, self.seconds = name, seconds
        self.deadline = monotonic() + seconds if seconds is not None else None
        self.cancelled, self.lock, self.workers = Event(), RLock(), set()

    def check(self):
        context = get_run_context()
        if context is not None:
            context.check_active()
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
