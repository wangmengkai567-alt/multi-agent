"""Fixed project hook pipeline around actual execution (sync and async)."""

import asyncio
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial
from time import monotonic, sleep
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langchain_core.runnables import Runnable
from langgraph.types import Command
from pydantic import ValidationError

from agent.runtime.context import get_run_context
from agent.runtime.events import EventPersistenceError, make_event, redact_text
from agent.runtime.review_state import ReviewState

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
    source_kind: str | None = None
    evidence_adapter: str | None = None
    check_status: str = "not_checked"
    state: ReviewState = field(default_factory=ReviewState)
    current_result_id: str | None = None
    report_attempt: int = 0
    executed: bool = False
    attempt: int = 0
    workflow_data: dict = field(default_factory=dict, repr=False)
    search_task_key: str | None = field(default=None, repr=False)
    search_budget: dict | None = field(default=None, repr=False)


def error_category(error):
    from agent.runtime.policies import classify_tool_error

    if isinstance(error, HookRejected):
        return classify_tool_error(error.__cause__) if error.__cause__ else "rejected"
    if isinstance(error, BaseException) and type(error).__name__ == "CancelledError":
        return "cancelled"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, EventPersistenceError):
        return "recording"
    return classify_tool_error(error)


def error_message(error):
    if type(error).__name__ == "CancelledError":
        context = get_run_context()
        if context and context.stop_status == "timed_out":
            return "任务达到总时限，已取消当前操作"
        if context and context.stop_status == "interrupted":
            return "服务关闭，已取消当前操作"
        return "当前操作已取消"
    return redact_text(str(error) or type(error).__name__)


def record_hook(name, call):
    context = get_run_context()
    if context is None or context.event_sink is None:
        return  # Standalone execution makes no persistence claim.
    data = {"hook": name, "elapsed_ms": call.elapsed_ms, "args": call.args, "attempt": call.attempt}
    if call.search_budget is not None:
        data["search_budget"] = call.search_budget
    if name == "after_tool":
        data["result_length"] = len(str(call.result)) if call.result is not None else 0
        data["result_type"] = type(call.result).__name__
        data["executed"] = call.executed
    if name == "after_subagent":
        data.update(check_status=call.check_status, result_length=len(str(call.result)))
    if call.error is not None:
        data.update(error=error_message(call.error), error_category=error_category(call.error))
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
            parent_call_id=call.parent_call_id or (None if call.call_id == context.run_id else context.run_id),
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
    get_run_context().check_active()


def default_pipeline():
    from agent.runtime.policies import before_run_policy, before_tool_policy
    from agent.runtime.workflows import after_tool_workflow, before_delegate_workflow, before_run_workflow

    after_tool_workflow.record_only = True

    recorders = {}
    for name in HOOK_NAMES:
        if name not in ("before_run", "after_run"):
            recorder = partial(record_hook, name)
            recorder.record_only = True
            recorders[name] = (recorder,)
    return HookPipeline(
        {
            "before_run": (prepare_run, before_run_policy, before_run_workflow),
            **recorders,
            "before_delegate": (prepare_run, before_delegate_workflow, *recorders["before_delegate"]),
            "after_tool": (after_tool_workflow, *recorders["after_tool"]),
            "before_tool": (before_tool_policy, *recorders["before_tool"]),
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
    def __init__(self, actor="main", *, allowed_targets=(), hooks=None, managed_delegation=False):
        self.actor = actor
        self.allowed_targets = frozenset(allowed_targets)
        self.target_specs = (
            dict(allowed_targets) if isinstance(allowed_targets, dict) or hasattr(allowed_targets, "items") else {}
        )
        self.hooks = hooks or pipeline
        self.managed_delegation = managed_delegation

    def _call(self, request):
        parent = active_call.get()
        tool = request.tool_call
        return HookCall(
            actor=f"{self.actor}:{tool['name']}",
            call_id=tool["id"],
            parent_call_id=parent.call_id if parent else None,
            args=tool["args"],
            search_task_key=parent.search_task_key if parent else None,
            target=tool["args"].get("subagent_type") if tool["name"] == "task" else None,
            source_kind=(
                self.target_specs[tool["args"]["subagent_type"]].execution.source_kind
                if tool["name"] == "task" and tool["args"].get("subagent_type") in self.target_specs
                else None
            ),
        )

    def _before(self, request, call):
        if context := get_run_context():
            context.check_active()
        if context and context.task_contract is not None:
            from agent.runtime.contracts import assert_contract

            contract = assert_contract(context)
            from agent.runtime.delivery_actions import action_authorized

            if (
                contract.delivery_kind == "clarification"
                or contract.delivery_kind == "direct"
                and not action_authorized(call)
            ):
                raise HookRejected("当前契约禁止模型业务工具调用")
            if request.tool_call["name"] in (
                "generate_markdown",
                "convert_md_to_pdf",
                "write_file",
                "edit_file",
            ) and not action_authorized(call):
                raise HookRejected("文件仅由程序在内容终审通过后生成")
            if not contract.network_allowed and (
                (call.target in self.target_specs and self.target_specs[call.target].execution.network_access)
                or request.tool_call["name"]
                in ("research_step", "internet_search", "read_web_page", "submit_research_job")
            ):
                raise HookRejected("原始任务禁止联网，本轮不能调用搜索或提交研究任务")
        tool = getattr(request, "tool", None)
        context = get_run_context()
        if context and context.enforce_plan and hasattr(request, "tool") and tool is None:
            error = ValueError("模型请求了未注册的工具，请选择实际提供的工具")
            raise HookRejected(str(error)) from error
        if tool is not None:
            from agent.runtime.evidence_protocol import EvidenceError, adapter_for_tool

            try:
                adapter = adapter_for_tool(tool)
            except EvidenceError as exc:
                raise HookRejected(str(exc)) from exc
            call.evidence_adapter = adapter.adapter_id
            if (
                context
                and adapter.network
                and (
                    context.network_policy == "forbidden"
                    or context.task_contract is not None
                    and not context.task_contract.network_allowed
                )
            ):
                raise HookRejected("本轮联网权限不允许执行该工具")
            try:
                parsed_args = tool.tool_call_schema.model_validate(call.args).model_dump(exclude_unset=True)
                # Validate policy against the same values the tool will actually use
                # (e.g. compatible transports encode integers/arrays as strings).
                # Preserve unknown fields so policy can still reject extra task args.
                call.args = {**call.args, **parsed_args}
                request.tool_call["args"] = call.args
            except ValidationError as exc:
                fields = "; ".join(
                    f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
                    for error in exc.errors(include_input=False, include_url=False)[:5]
                )
                raise HookRejected("Invalid tool arguments: " + fields) from exc
        if request.tool_call["name"] == "task":
            if call.target not in self.allowed_targets:
                raise HookRejected(f"Subagent target is not allowed: {call.target}")
            if not isinstance(call.args.get("description"), str) or not call.args["description"].strip():
                raise HookRejected("Task description must not be empty")
            if not self.managed_delegation:
                self.hooks.emit("before_delegate", call)
        self.hooks.emit("before_tool", call)

    def _retry_delay(self, request, call, exc):
        from agent.runtime.recovery import retry_delay

        return retry_delay(request, call, exc)

    def _timeout(self, call):
        from agent.runtime.policies import policy_for_run, request_timeout

        name = call.actor.rsplit(":", 1)[-1]
        if name in ("task", "research_step"):
            return None  # Delegations have their own collection and run deadlines.
        return request_timeout(policy_for_run().timeouts.tool_seconds) if get_run_context() else None

    def _error(self, call, exc):
        call.error = exc
        call.status = (
            "rejected"
            if isinstance(exc, HookRejected)
            else "cancelled"
            if error_category(exc) == "cancelled"
            else "failed"
        )
        self.hooks.emit("on_error", call)

    def _returned(self, call):
        if context := get_run_context():
            context.check_active()
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
        from agent.reflection import capture_tool

        try:
            capture_tool(call)
        except Exception as exc:
            self._error(call, exc)
            raise
        finally:
            self.hooks.emit("after_tool", call)
        if request.tool_call["name"] == "task" and call.executed and not self.managed_delegation:
            self.hooks.emit("after_subagent", call)

    def wrap_tool_call(self, request, handler):
        call = self._call(request)
        token = active_call.set(call)
        try:
            while True:
                started = monotonic()
                try:
                    from agent.runtime.workflows import prepare_tool_sync

                    prepare_tool_sync(call)
                    self._before(request, call)
                    call.executed = True
                    seconds = self._timeout(call)
                    if seconds is None:
                        call.result = handler(request)
                    else:
                        from agent.runtime.tool_timeout import invoke_bounded

                        call.result = invoke_bounded(handler, request, seconds)
                    self._returned(call)
                    return call.result
                except HookRejected as exc:
                    self._error(call, exc)
                    return ToolMessage(content=redact_text(str(exc)), tool_call_id=call.call_id, status="error")
                except Exception as exc:
                    self._error(call, exc)
                    delay = self._retry_delay(request, call, exc)
                    if delay is None:
                        if call.workflow_data.get("retry_blocked"):
                            return ToolMessage(
                                content=call.workflow_data["retry_blocked"], tool_call_id=call.call_id, status="error"
                            )
                        raise
                except BaseException as exc:
                    self._error(call, exc)
                    raise
                finally:
                    self._after(request, call, started)
                context = get_run_context()
                if context and context.event_sink:
                    context.event_sink.submit(
                        make_event(
                            context.thread_id,
                            context.run_id,
                            "tool_retry",
                            "运行时安排有界重试",
                            {"delay_seconds": delay, "attempt": call.attempt + 1},
                            actor=call.actor,
                            status="waiting",
                            call_id=call.call_id,
                            parent_call_id=call.parent_call_id or context.run_id,
                        )
                    )
                call.attempt += 1
                call.error, call.status, call.result, call.executed = None, "running", None, False
                sleep(delay)
        finally:
            active_call.reset(token)

    async def awrap_tool_call(self, request, handler):
        call = self._call(request)
        token = active_call.set(call)
        try:
            while True:
                started = monotonic()
                try:
                    from agent.runtime.workflows import prepare_tool

                    await prepare_tool(call)
                    self._before(request, call)
                    call.executed = True
                    seconds = self._timeout(call)
                    if seconds is None:
                        call.result = await handler(request)
                    else:
                        from agent.runtime.recovery import ToolCallTimeout

                        try:
                            async with asyncio.timeout(seconds):
                                call.result = await handler(request)
                        except TimeoutError as error:
                            if isinstance(error, ToolCallTimeout):
                                raise
                            raise ToolCallTimeout(request.tool_call["name"], seconds) from error
                    self._returned(call)
                    return call.result
                except HookRejected as exc:
                    self._error(call, exc)
                    return ToolMessage(content=redact_text(str(exc)), tool_call_id=call.call_id, status="error")
                except Exception as exc:
                    self._error(call, exc)
                    delay = self._retry_delay(request, call, exc)
                    if delay is None:
                        if call.workflow_data.get("retry_blocked"):
                            return ToolMessage(
                                content=call.workflow_data["retry_blocked"], tool_call_id=call.call_id, status="error"
                            )
                        raise
                except BaseException as exc:
                    self._error(call, exc)
                    raise
                finally:
                    self._after(request, call, started)
                context = get_run_context()
                if context and context.event_sink:
                    context.event_sink.submit(
                        make_event(
                            context.thread_id,
                            context.run_id,
                            "tool_retry",
                            "运行时安排有界重试",
                            {"delay_seconds": delay, "attempt": call.attempt + 1},
                            actor=call.actor,
                            status="waiting",
                            call_id=call.call_id,
                            parent_call_id=call.parent_call_id or context.run_id,
                        )
                    )
                call.attempt += 1
                call.error, call.status, call.result, call.executed = None, "running", None, False
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError as exc:
                    self._error(call, exc)
                    raise
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
