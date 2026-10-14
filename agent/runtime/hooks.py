"""Fixed project hook pipeline around actual execution (sync and async)."""

import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial
from time import monotonic
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langchain_core.runnables import Runnable
from langgraph.types import Command

from agent.runtime.context import get_run_context
from agent.runtime.events import EventPersistenceError, make_event, redact_text

logger = logging.getLogger(__name__)
HOOK_NAMES = ("before_run", "before_delegate", "before_tool", "after_tool", "after_subagent", "after_run", "on_error")
CONTROL_HOOKS = frozenset(HOOK_NAMES[:3])
active_call = ContextVar("hook_active_call", default=None)


class HookRejected(RuntimeError):
    """A control hook refused execution before the handler was called."""


@dataclass
class HookCall:
    actor: str
    call_id: str
    parent_call_id: str | None = None
    args: dict = field(default_factory=dict)
    status: str = "running"
    result: object = None
    error: BaseException | None = None
    elapsed_ms: float = 0
    target: str | None = None
    check_status: str = "not_checked"
    executed: bool = False


def error_category(error):
    if isinstance(error, HookRejected):
        return "rejected"
    if isinstance(error, BaseException) and type(error).__name__ == "CancelledError":
        return "cancelled"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, EventPersistenceError):
        return "recording"
    return "execution"


def record_hook(name, call):
    context = get_run_context()
    if context is None or context.event_sink is None:
        return  # Standalone execution makes no persistence claim.
    data = {"hook": name, "elapsed_ms": call.elapsed_ms, "args": call.args}
    if name == "after_tool":
        data["result_length"] = len(str(call.result)) if call.result is not None else 0
        data["result_type"] = type(call.result).__name__
        data["executed"] = call.executed
    if name == "after_subagent":
        data.update(check_status=call.check_status, result_length=len(str(call.result)))
    if call.error is not None:
        data.update(error=redact_text(str(call.error)), error_category=error_category(call.error))
    context.event_sink.submit(
        make_event(
            context.thread_id,
            context.run_id,
            name,
            f"{name}: {call.actor}",
            data,
            actor=call.actor,
            status=call.status,
            call_id=call.call_id,
            parent_call_id=call.parent_call_id or context.run_id,
        )
    )


class HookPipeline:
    """Handlers run in declared insertion order; no priorities or plugin sorting.

    Controls reject by raising HookRejected. Recorder exceptions are exposed in
    logs and the run context, while every remaining recorder still gets called.
    Handlers are synchronous so the identical pipeline works in tool threads.
    """

    def __init__(self, handlers=None):
        handlers = handlers or {}
        if set(handlers) - set(HOOK_NAMES):
            raise ValueError("Unknown project hook")
        self.handlers = {name: tuple(handlers.get(name, ())) for name in HOOK_NAMES}

    def emit(self, name, call):
        for handler in self.handlers[name]:
            try:
                handler(call)
            except Exception as exc:
                if name in CONTROL_HOOKS and not getattr(handler, "record_only", False):
                    raise
                logger.exception("Hook record was NOT saved: %s", name)
                context = get_run_context()
                if context is not None:
                    context.hook_failures.append(f"{name}: {redact_text(str(exc))}")


def prepare_run(call):
    if get_run_context() is None:
        raise HookRejected("Run context is required")


def default_pipeline():
    # Runtime rules will be added here in step 07, before any business handler.
    recorders = {}
    for name in HOOK_NAMES:
        if name not in ("before_run", "after_run"):
            recorder = partial(record_hook, name)
            recorder.record_only = True
            recorders[name] = (recorder,)
    return HookPipeline(
        {
            "before_run": (prepare_run,),
            **recorders,
        }
    )


pipeline = default_pipeline()


async def finish_run(context, call, *, answer=None, error=None):
    """Save the business outcome even when an independent recorder failed."""
    pipeline.emit("after_run", call)
    try:
        if context.event_sink is not None:
            await context.event_sink.finish(context, call.status, answer=answer, error=error)
    finally:
        if context.hook_failures:
            raise EventPersistenceError("Hook recording incomplete: " + "; ".join(context.hook_failures))


class HookToolMiddleware(AgentMiddleware):
    def __init__(self, actor="main", *, allowed_targets=(), hooks=None):
        self.actor = actor
        self.allowed_targets = frozenset(allowed_targets)
        self.hooks = hooks or pipeline

    def _call(self, request):
        parent = active_call.get()
        tool = request.tool_call
        return HookCall(
            actor=f"{self.actor}:{tool['name']}",
            call_id=tool["id"],
            parent_call_id=parent.call_id if parent else None,
            args=tool["args"],
            target=tool["args"].get("subagent_type") if tool["name"] == "task" else None,
        )

    def _before(self, request, call):
        if request.tool_call["name"] == "task":
            if call.target not in self.allowed_targets:
                raise HookRejected(f"Subagent target is not allowed: {call.target}")
            self.hooks.emit("before_delegate", call)
        self.hooks.emit("before_tool", call)

    def _error(self, call, exc):
        call.error = exc
        call.status = error_category(exc) if error_category(exc) in ("rejected", "cancelled") else "failed"
        self.hooks.emit("on_error", call)

    def _returned(self, call):
        result = call.result
        messages = [result] if isinstance(result, ToolMessage) else []
        if isinstance(result, Command) and isinstance(result.update, dict):
            value = result.update.get("messages", [])
            messages = [value] if isinstance(value, ToolMessage) else value if isinstance(value, list) else []
        if any(isinstance(message, ToolMessage) and message.status == "error" for message in messages):
            # Do not copy raw error tool payloads (which may echo uploaded bodies).
            self._error(call, RuntimeError("Tool returned an error response"))
        else:
            call.status = "completed"

    def _after(self, request, call, started):
        call.elapsed_ms = (monotonic() - started) * 1000
        self.hooks.emit("after_tool", call)
        if request.tool_call["name"] == "task" and call.executed:
            self.hooks.emit("after_subagent", call)

    def wrap_tool_call(self, request, handler):
        call = self._call(request)
        token = active_call.set(call)
        started = monotonic()
        try:
            self._before(request, call)
            call.executed = True
            call.result = handler(request)
            self._returned(call)
            return call.result
        except HookRejected as exc:
            self._error(call, exc)
            return ToolMessage(content=redact_text(str(exc)), tool_call_id=call.call_id, status="error")
        except BaseException as exc:
            self._error(call, exc)
            raise
        finally:
            try:
                self._after(request, call, started)
            finally:
                active_call.reset(token)

    async def awrap_tool_call(self, request, handler):
        call = self._call(request)
        token = active_call.set(call)
        started = monotonic()
        try:
            self._before(request, call)
            call.executed = True
            call.result = await handler(request)
            self._returned(call)
            return call.result
        except HookRejected as exc:
            self._error(call, exc)
            return ToolMessage(content=redact_text(str(exc)), tool_call_id=call.call_id, status="error")
        except BaseException as exc:
            self._error(call, exc)
            raise
        finally:
            try:
                self._after(request, call, started)
            finally:
                active_call.reset(token)


class HookSubagentRunnable(Runnable):
    """Explicit adapter for compiled executors; inner tools need middleware too.

    Framework task already owns delegation hooks. Direct calls to this executor
    use the same hooks, without charging or recording delegation twice.
    """

    def __init__(self, target, runnable, hooks=None):
        self.target, self.runnable, self.hooks = target, runnable, hooks or pipeline

    def _begin(self):
        parent = active_call.get()
        if parent and parent.target == self.target:
            return None, None
        call = HookCall(self.target, str(uuid4()), parent.call_id if parent else None, target=self.target)
        token = active_call.set(call)
        return call, token

    def _end(self, call, token, started):
        if call is not None:
            try:
                call.elapsed_ms = (monotonic() - started) * 1000
                if call.executed:
                    self.hooks.emit("after_subagent", call)
            finally:
                active_call.reset(token)

    def invoke(self, input, config=None, **kwargs):
        call, token = self._begin()
        started = monotonic()
        try:
            if call:
                self.hooks.emit("before_delegate", call)
                call.executed = True
            result = self.runnable.invoke(input, config, **kwargs)
            if call:
                call.result, call.status = result, "completed"
            return result
        except BaseException as exc:
            if call:
                call.error = exc
                call.status = error_category(exc) if error_category(exc) in ("rejected", "cancelled") else "failed"
                self.hooks.emit("on_error", call)
            raise
        finally:
            self._end(call, token, started)

    async def ainvoke(self, input, config=None, **kwargs):
        call, token = self._begin()
        started = monotonic()
        try:
            if call:
                self.hooks.emit("before_delegate", call)
                call.executed = True
            result = await self.runnable.ainvoke(input, config, **kwargs)
            if call:
                call.result, call.status = result, "completed"
            return result
        except BaseException as exc:
            if call:
                call.error = exc
                call.status = error_category(exc) if error_category(exc) in ("rejected", "cancelled") else "failed"
                self.hooks.emit("on_error", call)
            raise
        finally:
            self._end(call, token, started)


def hook_subagents(specs):
    """Configure each child explicitly; main middleware is never inherited."""
    result = []
    for spec in specs:
        if "runnable" in spec:
            result.append(dict(spec, runnable=HookSubagentRunnable(spec["name"], spec["runnable"])))
        else:
            result.append(dict(spec, middleware=[*spec.get("middleware", []), HookToolMiddleware(spec["name"])]))
    return result
