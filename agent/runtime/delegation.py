"""The one lifecycle owner at the actual expert execution boundary."""

import logging
from datetime import UTC, datetime
from hashlib import sha256
from time import monotonic
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import Runnable

from agent.runtime.context import get_run_context
from agent.runtime.events import make_event, redact_text
from agent.runtime.hooks import HookCall, HookRejected, active_call, pipeline


class DelegationRunnable(Runnable):
    def __init__(self, spec, executor, *, hooks=None):
        from agent.subagent_registry import DISPLAY_NAMES

        if spec.agent_id not in DISPLAY_NAMES:
            raise ValueError("Invalid expert executor target")
        self.spec, self.executor, self.hooks = spec, executor, hooks or pipeline

    def _prepare(self, inputs):
        context = get_run_context()
        if context is None:
            raise HookRejected("Expert execution requires a run context")
        messages = inputs.get("messages", [])
        if not messages or not isinstance(messages[-1], HumanMessage) or not messages[-1].text.strip():
            raise HookRejected("Expert execution requires a non-empty task description")
        description = messages[-1].text
        parent = active_call.get()
        request_id = parent.call_id if parent and parent.target == self.spec.agent_id else None
        call = HookCall(
            self.spec.agent_id,
            str(uuid4()),
            request_id or context.run_id,
            args={"description": description},
            target=self.spec.agent_id,
        )
        record = dict(
            call_id=call.call_id,
            run_id=context.run_id,
            thread_id=context.thread_id,
            request_call_id=request_id,
            target=self.spec.agent_id,
            display_name=self.spec.display_name,
            description="[REDACTED]",
            description_length=len(description),
            description_sha256=sha256(description.encode("utf-8")).hexdigest(),
            status="queued",
            deadline=context.deadline.isoformat() if context.deadline else None,
            created_at=datetime.now(UTC).isoformat(),
            ended_at=None,
            result=None,
            error=None,
            check_status="not_checked",
        )
        if request_id:
            with context.budget_lock:
                context.delegation_calls.setdefault(request_id, []).append(record)
        return context, call, record

    def _save(self, context, call, record, status):
        record["status"] = status
        if status not in ("queued", "running"):
            record["ended_at"] = datetime.now(UTC).isoformat()
        if context.event_sink is None:
            return  # Standalone has no persistence claim, but still uses controls.
        event = make_event(
            context.thread_id,
            context.run_id,
            f"subagent_{status}",
            f"{self.spec.display_name}："
            + {
                "queued": "任务已接收",
                "running": "开始执行",
                "completed": "返回结果",
                "failed": "执行失败",
                "rejected": "执行被拒绝",
                "cancelled": "执行已取消",
            }[status],
            {
                "assistant_name": self.spec.display_name,
                "target": self.spec.agent_id,
                "request_call_id": record["request_call_id"],
                "check_status": record["check_status"],
            },
            actor=self.spec.agent_id,
            status=status,
            call_id=call.call_id,
            parent_call_id=call.parent_call_id,
        )
        try:
            context.event_sink.submit_subtask(dict(record), event)
        except Exception as exc:
            logging.getLogger(__name__).exception("Subtask record was NOT saved")
            context.hook_failures.append(f"subtask {call.call_id}: {redact_text(str(exc))}")

    def _start(self, context, call, record):
        self._save(context, call, record, "queued")
        self.hooks.emit("before_delegate", call)
        if context.deadline is not None and context.deadline <= datetime.now(UTC):
            raise HookRejected("Run deadline has expired; expert execution was not started")
        self._save(context, call, record, "running")
        call.executed = True

    def _returned(self, call, record, result):
        messages = result.get("messages", []) if isinstance(result, dict) else []
        final = messages[-1] if messages else None
        if not isinstance(final, AIMessage) or final.tool_calls:
            raise RuntimeError("Expert executor did not return a final assistant message")
        call.result, call.status = result, "completed"
        record["result"] = redact_text(final.text)

    def _error(self, call, record, error):
        call.error = error
        call.status = (
            "rejected"
            if isinstance(error, HookRejected)
            else "cancelled"
            if type(error).__name__ == "CancelledError"
            else "failed"
        )
        record["error"] = redact_text(str(error))
        self.hooks.emit("on_error", call)

    def _finish(self, context, call, record, started):
        call.elapsed_ms = (monotonic() - started) * 1000
        self._save(context, call, record, call.status)
        if call.executed:
            self.hooks.emit("after_subagent", call)

    def invoke(self, input, config=None, **kwargs):
        context, call, record = self._prepare(input)
        token = active_call.set(call)
        started = monotonic()
        try:
            self._start(context, call, record)
            result = self.executor.invoke(input, config, **kwargs)
            self._returned(call, record, result)
            return result
        except BaseException as exc:
            self._error(call, record, exc)
            raise
        finally:
            try:
                self._finish(context, call, record, started)
            finally:
                active_call.reset(token)

    async def ainvoke(self, input, config=None, **kwargs):
        context, call, record = self._prepare(input)
        token = active_call.set(call)
        started = monotonic()
        try:
            self._start(context, call, record)
            result = await self.executor.ainvoke(input, config, **kwargs)
            self._returned(call, record, result)
            return result
        except BaseException as exc:
            self._error(call, record, exc)
            raise
        finally:
            try:
                self._finish(context, call, record, started)
            finally:
                active_call.reset(token)
