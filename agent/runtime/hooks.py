# 工具执行边界：执行前校验权限和预算，返回后校验证据，并在各阶段记录事件。
"""Fixed project hook pipeline around actual execution (sync and async)."""

import asyncio
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial
from time import monotonic, sleep

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from pydantic import ValidationError

from agent.runtime.context import get_run_context
from agent.runtime.errors import DependencyUnavailable, ExecutionTerminated
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
    source_kind: str | None = None
    evidence_adapter: str | None = None
    executed: bool = False
    # 执行完成与结果可采信分别记录：工具返回后，结果仍可能因证据校验失败而被拒绝。
    execution_status: str = "not_started"
    result_status: str = "unavailable"
    stage: str = "admission"
    terminal_recorded: bool = False
    attempt: int = 0
    workflow_data: dict = field(default_factory=dict, repr=False)
    task_id: str | None = field(default=None, repr=False)


def error_category(error):
    from agent.runtime.policies import classify_tool_error
    from agent.runtime.recovery import is_transport_timeout

    if isinstance(error, HookRejected):
        return classify_tool_error(error.__cause__) if error.__cause__ else "rejected"
    if isinstance(error, BaseException) and type(error).__name__ == "CancelledError":
        return "cancelled"
    if is_transport_timeout(error):
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


def error_details(call):
    return dict(
        error_category=error_category(call.error),
        stage=call.stage,
        tool_name=call.actor.rsplit(":", 1)[-1],
        call_id=call.call_id,
    )


def record_hook(name, call):
    context = get_run_context()
    if context is None or context.event_sink is None:
        return  # Standalone execution makes no persistence claim.
    data = {"hook": name, "elapsed_ms": call.elapsed_ms, "args": call.args, "attempt": call.attempt}
    if name == "after_tool":
        data["result_length"] = len(str(call.result)) if call.result is not None else 0
        data["result_type"] = type(call.result).__name__
        data.update(
            executed=call.executed,
            execution_status=call.execution_status,
            result_status=call.result_status,
            stage=call.stage,
            tool_name=call.actor.rsplit(":", 1)[-1],
        )
    if name == "after_subagent":
        data.update(result_length=len(str(call.result)))
    if call.error is not None:
        data.update(error=error_message(call.error), **error_details(call))
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
                # 控制钩子失败立即阻止执行；记录钩子失败则保留错误，并继续调用其余记录器。
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
    from agent.runtime.workflows import after_tool_workflow

    after_tool_workflow.record_only = True

    recorders = {}
    for name in HOOK_NAMES:
        if name not in ("before_run", "after_run"):
            recorder = partial(record_hook, name)
            recorder.record_only = True
            recorders[name] = (recorder,)
    return HookPipeline(
        {
            "before_run": (prepare_run, before_run_policy),
            **recorders,
            "before_delegate": (prepare_run, *recorders["before_delegate"]),
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
            task_id=parent.task_id if parent else None,
            target=tool["args"].get("subagent_type") if tool["name"] == "task" else None,
        )

    # 工具真正执行前统一检查：当前轮次仍有效、工具已注册、参数合法、目标专家获准。
    def _before(self, request, call):
        if context := get_run_context():
            context.check_active()
        from agent.runtime.capabilities import expert_available, tool_available

        if context and context.task_contract is not None:
            from agent.runtime.contracts import assert_contract

            assert_contract(context)
        if context and not tool_available(context, request.tool_call["name"]):
            raise HookRejected("工具不在当前契约、权限或执行阶段允许的范围内")

        tool = getattr(request, "tool", None)
        context = get_run_context()
        if context and hasattr(request, "tool") and tool is None:
            error = ValueError("模型请求了未注册的工具，请选择实际提供的工具")
            raise HookRejected(str(error)) from error
        if tool is not None:
            from agent.runtime.evidence_protocol import EvidenceError, adapter_for_tool

            try:
                adapter = adapter_for_tool(tool)
            except EvidenceError as exc:
                raise HookRejected(str(exc)) from exc
            call.evidence_adapter = adapter.adapter_id
            if context and adapter.network and not tool_available(context, request.tool_call["name"], network=True):
                raise HookRejected("本轮联网权限不允许执行该工具")
            try:
                parsed_args = tool.tool_call_schema.model_validate(call.args).model_dump(exclude_unset=True)
                # 按工具实际使用的类型解析参数，再进行策略检查，例如把字符串形式的整数转回整数。
                # 保留未知字段，让策略层仍能识别并拒绝多余的 task 参数。
                call.args = {**call.args, **parsed_args}
                request.tool_call["args"] = call.args
            except ValidationError as exc:
                fields = "; ".join(
                    f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
                    for error in exc.errors(include_input=False, include_url=False)[:5]
                )
                raise HookRejected("Invalid tool arguments: " + fields) from exc
        if request.tool_call["name"] == "task":
            call.target = call.args.get("subagent_type")
            if not isinstance(call.target, str):
                raise HookRejected("Task subagent_type must be a string")
            if call.target not in self.allowed_targets:
                raise HookRejected(f"Subagent target is not allowed: {call.target}")
            if not isinstance(call.args.get("description"), str) or not call.args["description"].strip():
                raise HookRejected("Task description must not be empty")
            spec = self.target_specs.get(call.target)
            if spec is not None:
                call.source_kind = spec.execution.source_kind
                if context and not expert_available(context, spec):
                    raise HookRejected("专家不在当前契约、权限或执行阶段允许的范围内")
            if not self.managed_delegation:
                self.hooks.emit("before_delegate", call)
        self.hooks.emit("before_tool", call)

    def _retry_delay(self, request, call, exc):
        from agent.runtime.recovery import retry_delay

        return retry_delay(request, call, exc)

    def _timeout(self, call):
        from agent.runtime.policies import policy_for_run, request_timeout

        name = call.actor.rsplit(":", 1)[-1]
        if name == "task":
            return None  # 专家串行执行由整轮截止时间约束，不套用普通工具的单次时限。
        if "admitted_timeout" in call.workflow_data:
            return call.workflow_data["admitted_timeout"]
        return request_timeout(policy_for_run().timeouts.tool_seconds) if get_run_context() else None

    def _error(self, call, exc):
        if call.actor.rsplit(":", 1)[-1] == "task" and isinstance(exc, HookRejected):
            if context := get_run_context():
                context.task_rejections.append(error_message(exc))
        call.error = exc
        call.result_status = "rejected"
        if call.execution_status == "running":
            from agent.runtime.recovery import ToolCallTimeout

            call.execution_status = "unknown" if isinstance(exc, ToolCallTimeout | asyncio.CancelledError) else "failed"
        call.status = (
            "rejected"
            if isinstance(exc, HookRejected)
            else "cancelled"
            if error_category(exc) == "cancelled"
            else "failed"
        )
        self.hooks.emit("on_error", call)

    @staticmethod
    def _error_response(call, message):
        return ToolMessage(
            content=message,
            tool_call_id=call.call_id,
            status="error",
            additional_kwargs={"runtime_error": dict(error_details(call), message=error_message(call.error))},
        )

    # 返回消息必须对应本次调用；显式错误响应即使没有抛异常，也不能记为成功。
    def _returned(self, call):
        if context := get_run_context():
            context.check_active()
        result = call.result
        messages = [result] if isinstance(result, ToolMessage) else []
        if isinstance(result, Command) and isinstance(result.update, dict):
            value = result.update.get("messages", [])
            messages = [value] if isinstance(value, ToolMessage) else value if isinstance(value, list) else []
        if any(isinstance(message, ToolMessage) and message.tool_call_id != call.call_id for message in messages):
            raise ValueError("Tool returned a message for a different invocation")
        if any(isinstance(message, ToolMessage) and message.status == "error" for message in messages):
            # Do not copy raw error tool payloads (which may echo uploaded bodies).
            self._error(call, RuntimeError("Tool returned an error response"))
        else:
            call.status = "completed"

    # 每次尝试只收尾一次：采集并验证工具证据，再发出 after_tool 事件。
    def _after(self, request, call, started):
        if call.terminal_recorded:
            return
        call.terminal_recorded = True
        call.stage = "result_validation" if call.error is None else call.stage
        call.elapsed_ms = (monotonic() - started) * 1000
        from agent.reflection import capture_tool

        try:
            capture_tool(call)
            if call.status == "completed":
                call.result_status = "accepted"
        except Exception as exc:
            self._error(call, exc)
            raise
        finally:
            self.hooks.emit("after_tool", call)
        if request.tool_call["name"] == "task" and call.executed and not self.managed_delegation:
            self.hooks.emit("after_subagent", call)

    @staticmethod
    def _execute(request, call, handler):
        context = get_run_context()
        if context:
            context.check_active()
        if call.terminal_recorded:
            raise RuntimeError("Tool attempt already reached its terminal outcome")
        call.executed, call.execution_status = True, "running"
        return handler(request)

    def wrap_tool_call(self, request, handler):
        call = self._call(request)
        token = active_call.set(call)
        try:
            while True:
                started = monotonic()
                try:
                    self._before(request, call)
                    seconds = self._timeout(call)
                    call.stage = "execution"
                    from agent.runtime.tool_timeout import bounded_tool

                    with bounded_tool(call.actor, seconds):
                        call.result = self._execute(request, call, handler)
                    call.execution_status = "completed"
                    call.result_status = "pending"
                    call.stage = "result_validation"
                    self._returned(call)
                    return call.result
                except ExecutionTerminated as exc:
                    if context := get_run_context():
                        context.terminate(exc)
                    self._error(call, exc)
                    raise
                except DependencyUnavailable as exc:
                    self._error(call, exc)
                    if call.task_id:
                        raise  # The owning delegation records a failed dependency, without report/review calls.
                    return self._error_response(call, error_message(exc))
                except HookRejected as exc:
                    self._error(call, exc)
                    return self._error_response(call, error_message(exc))
                except Exception as exc:
                    self._error(call, exc)
                    delay = self._retry_delay(request, call, exc)
                    if delay is None:
                        if call.workflow_data.get("retry_blocked"):
                            return self._error_response(
                                call, error_message(exc) + "; " + call.workflow_data["retry_blocked"]
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
                call.execution_status, call.result_status, call.stage = "not_started", "unavailable", "admission"
                call.terminal_recorded = False
                sleep(delay)
        finally:
            active_call.reset(token)

    # 异步执行也走相同的校验和记录链；仅按恢复策略允许的错误安排有界重试。
    async def awrap_tool_call(self, request, handler):
        call = self._call(request)
        token = active_call.set(call)
        try:
            while True:
                started = monotonic()
                try:
                    self._before(request, call)
                    seconds = self._timeout(call)
                    call.executed = True
                    call.execution_status = "running"
                    call.stage = "execution"
                    from agent.runtime.tool_timeout import bounded_tool

                    with bounded_tool(call.actor, seconds):
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
                    call.execution_status = "completed"
                    call.result_status = "pending"
                    call.stage = "result_validation"
                    self._returned(call)
                    return call.result
                except ExecutionTerminated as exc:
                    if context := get_run_context():
                        context.terminate(exc)
                    self._error(call, exc)
                    raise
                except DependencyUnavailable as exc:
                    self._error(call, exc)
                    if call.task_id:
                        raise  # The owning delegation records a failed dependency, without report/review calls.
                    return self._error_response(call, error_message(exc))
                except HookRejected as exc:
                    self._error(call, exc)
                    return self._error_response(call, error_message(exc))
                except Exception as exc:
                    self._error(call, exc)
                    delay = self._retry_delay(request, call, exc)
                    if delay is None:
                        if call.workflow_data.get("retry_blocked"):
                            return self._error_response(
                                call, error_message(exc) + "; " + call.workflow_data["retry_blocked"]
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
                call.execution_status, call.result_status, call.stage = "not_started", "unavailable", "admission"
                call.terminal_recorded = False
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError as exc:
                    self._error(call, exc)
                    raise
        finally:
            active_call.reset(token)
