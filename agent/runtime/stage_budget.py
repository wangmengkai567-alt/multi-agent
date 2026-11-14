"""One admission calculation and absolute deadlines for serial evidence stages."""

import asyncio
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import asdict, dataclass
from threading import Thread
from time import monotonic

from agent.runtime.errors import StageCleanupFailed, StageEnded


@dataclass(frozen=True)
class Admission:
    allowed: bool
    code: str
    reason: str
    item_id: str | None
    remaining_seconds: float | None
    stage_seconds: float
    future_reserve_seconds: float
    coordination_window_seconds: float
    decided_at: float
    execution_unit: dict | None = None


def investigation_items(context):
    return {
        key: item
        for key, item in context.required_work.items()
        if item["status"] != "completed"
        and any(condition["evidence_kind"] != "delivery" for condition in item["completion_conditions"])
    }


def admit_stage(context, spec=None):
    from agent.runtime.document_work import execution_unit
    from agent.runtime.policies import calls_available_for
    from agent.runtime.required_work import active_work_item

    with context.budget_lock:
        decided_at = monotonic()
        limits = context.policy.stages
        remaining = context.collection_remaining()
        items = investigation_items(context)
        item_id = active_work_item(context)
        item = items.get(item_id)
        scheduled = {
            key: value
            for key, value in items.items()
            if not value["permission_conflict"]
            and (value["ready_for_delegation"] or value["status"] == "pending" or not value["task_ids"])
        }
        planned = scheduled if scheduled else ({None: None} if not context.required_work else {})
        units = {
            key: execution_unit(context, value, spec if key == item_id else None) for key, value in planned.items()
        }
        minimums = {key: limits.coordination_seconds + sum(unit["seconds"].values()) for key, unit in units.items()}
        selected = item_id if item_id in minimums else next(iter(minimums), None)
        minimum = minimums[selected] if minimums else limits.coordination_seconds
        future = sum(value for key, value in minimums.items() if key != selected)
        usable = (
            remaining
            if remaining is not None
            else context.policy.timeouts.total_seconds - context.policy.timeouts.report_reserve_seconds
        )
        expert_minimum = minimum - limits.coordination_seconds if planned else 0.0
        request_limit = context.policy.timeouts.request_seconds
        # Future minima guide allocation; they cannot veto an affordable current stage.
        # Share spare time between one coordination request and the later stages, while
        # retaining the current expert's tools, handoff and cleanup minimum.
        coordination = max(
            0.0,
            min(
                request_limit,
                usable - expert_minimum,
                max(
                    limits.coordination_seconds,
                    (usable - expert_minimum) * request_limit / (request_limit + future),
                ),
            ),
        )
        allocation = (
            max(0.0, min(usable - coordination, max(expert_minimum, usable - coordination - future)))
            if planned
            else 0.0
        )
        future_reserve = max(0.0, usable - coordination - allocation) if planned else 0.0
        code, reason = "allowed", "当前协调、取证、交接及清理有预算；后续事项逐阶段重新准入"
        if context.stop_status:
            code, reason = (
                "cancelled" if context.stop_status == "cancelled" else "run_stopped",
                "本轮已停止：" + context.stop_status,
            )
        elif context.report_started:
            code, reason = "report_started", "已进入最终汇总，不再启动取证"
        elif (
            spec is not None
            and spec.execution.network_access
            or item
            and any(c["evidence_kind"] == "web" for c in item["completion_conditions"])
        ) and context.network_policy == "forbidden":
            code, reason = "network_forbidden", "本轮禁止联网，外部核验保留未完成"
        elif items and not scheduled and all(value["permission_conflict"] for value in items.values()):
            code, reason = "network_forbidden", "剩余外部核验事项与本轮联网权限冲突"
        elif planned and units[selected]["kind"] == "document_no_unread_range":
            code, reason = "document_gap", "材料没有可接续的未读页；已读证据和识别缺口保留，不重复失败操作"
        elif usable < minimum:
            code, reason = "time_budget", "剩余阶段预算不足以覆盖当前协调、专家取证、交接及清理"
        elif len(context.task_results) >= context.policy.search.max_tasks_per_run:
            code, reason = "task_limit", "本轮委派数量额度已耗尽"
        elif not calls_available_for(
            context,
            *(
                ("main_model", "main_model")
                + ("sub_model",)
                * (units[selected]["thinking_model_calls"] + units[selected]["internal_model_calls"] + 1)
                + ("tool",) * (units[selected]["tool_calls"] + 2)
                if planned
                else ("main_model", "main_model", "tool")
            ),
        ):
            code, reason = "call_budget", "剩余调用额度不足以覆盖专家取证及最终答复"
        elif context.required_work and item is None:
            code, reason = "work_not_selected", "先确认前置事项并选择当前执行事项"
        elif item and not item["ready_for_delegation"]:
            code, reason = "handoff_pending", "当前分工已签发，先按实际交接确认事项或明确申请新的分工"
        elif item and any(context.required_work[key]["status"] != "completed" for key in item["dependencies"]):
            code, reason = "dependency_incomplete", "前置事项尚未满足，不能启动后续核验"
        return Admission(
            code == "allowed",
            code,
            reason,
            item_id,
            remaining,
            max(0.0, allocation),
            future_reserve,
            coordination,
            decided_at,
            units[selected] if planned else None,
        )


def record_decision(context, decision, entry, target=None):
    from agent.runtime.events import make_event

    key = (target, decision.item_id, decision.allowed, decision.code)
    with context.budget_lock:
        slot = (entry, target)
        if context.budget_decisions.get(slot) == key:
            return
        context.budget_decisions[slot] = key
        if context.event_sink:
            context.event_sink.submit(
                make_event(
                    context.thread_id,
                    context.run_id,
                    "stage_admission",
                    "阶段预算准入：" + decision.code,
                    dict(entry=entry, target=target, **asdict(decision)),
                    actor="main",
                    status="allowed" if decision.allowed else "blocked",
                    call_id=context.run_id,
                )
            )


def begin_stage(context, task_id, decision):
    now = decision.decided_at
    limits = context.policy.stages
    stage = dict(
        item_id=decision.item_id,
        deadline=now + decision.stage_seconds,
        handoff_deadline=now + decision.stage_seconds - context.policy.timeouts.shutdown_seconds,
        tool_deadline=now + decision.stage_seconds - context.policy.timeouts.shutdown_seconds - limits.handoff_seconds,
        future_reserve_seconds=decision.future_reserve_seconds,
        execution_unit=decision.execution_unit,
    )
    context.task_results[task_id]["stage_budget"] = stage
    record_decision(context, decision, "allocation:" + task_id, context.task_results[task_id]["target"])
    return stage


def current_stage(context):
    from agent.runtime.hooks import active_call

    call = active_call.get()
    row = context.task_results.get(call.task_id) if call and call.task_id else None
    return row.get("stage_budget") if row else None


def stage_remaining(context, *, tools=False):
    stage = current_stage(context)
    if stage is None:
        return context.collection_remaining()
    return min(
        value
        for value in (
            stage["tool_deadline" if tools else "handoff_deadline"] - monotonic(),
            context.collection_remaining(),
        )
        if value is not None
    )


def stage_snapshot(context):
    from agent.runtime.document_work import next_document_step

    stage = current_stage(context)
    if stage is None:
        return None
    return dict(
        item_id=stage["item_id"],
        tool_seconds=max(0.0, stage_remaining(context, tools=True)),
        handoff_seconds=max(0.0, stage_remaining(context)),
        cleanup_deadline_seconds=max(0.0, stage["deadline"] - monotonic()),
        future_reserve_seconds=stage["future_reserve_seconds"],
        execution_unit=stage.get("execution_unit"),
        next_document_step=next_document_step(context),
    )


def check_stage(context):
    stage = current_stage(context)
    if stage is not None and stage_remaining(context) <= 0:
        raise StageEnded("当前分工阶段预算已到，停止取证并保留截止前有效证据，为后续事项留时")


@dataclass(frozen=True)
class OperationCost:
    seconds: float
    internal_model_calls: int


@dataclass(frozen=True)
class ThinkingWindow:
    task_id: str
    deadline: float
    operation_seconds: float


active_thinking_window = ContextVar("expert_thinking_window", default=None)


def operation_cost(context, name=None, parameters=None):
    from pathlib import Path

    from agent.runtime.document_work import next_document_step
    from tools.document_vision import VISUAL_SUFFIXES

    nested = False
    if name == "read_document" and parameters is not None:
        nested = (parameters.get("selection") or {}).get("mode") != "catalog" and (
            Path(parameters["filename"]).suffix.lower() in VISUAL_SUFFIXES
        )
    elif name in (None, "read_document"):
        step = next_document_step(context)
        # The disclosed schema can select content even before a catalog is known.
        nested = step is not None and step["visual"]
    limits = context.policy.stages
    return OperationCost(limits.tool_min_seconds + nested * limits.expert_request_min_seconds, int(nested))


@contextmanager
def expert_tool_turn(context, names):
    from agent.runtime.hooks import active_call
    from agent.runtime.tool_timeout import active_tool_scope

    stage = current_stage(context) if context else None
    window = None
    if stage is not None:
        now = monotonic()
        reserve = max((operation_cost(context, name).seconds for name in names), default=0.0)
        hard_deadline = stage["tool_deadline"]
        remaining = context.collection_remaining()
        if remaining is not None:
            hard_deadline = min(hard_deadline, now + remaining)
        scope = active_tool_scope.get()
        if scope is not None and scope.deadline is not None:
            hard_deadline = min(hard_deadline, scope.deadline)
        task_id = active_call.get().task_id
        deadline = min(hard_deadline - reserve, now + context.policy.timeouts.request_seconds)
        parent = active_thinking_window.get()
        if parent is not None and parent.task_id == task_id:
            deadline = min(deadline, parent.deadline)
        window = ThinkingWindow(task_id, deadline, reserve)
    token = active_thinking_window.set(window)
    try:
        yield window
    finally:
        active_thinking_window.reset(token)


def expert_request_deadline(context):
    from agent.runtime.hooks import active_call
    from agent.runtime.tool_timeout import active_tool_scope

    scope = active_tool_scope.get()
    if scope is not None and scope.deadline is not None:
        return None  # A nested model shares the parent tool rather than reserving another tool.
    stage = current_stage(context)
    if stage is None:
        return None
    window = active_thinking_window.get()
    call = active_call.get()
    if window is not None and window.task_id == call.task_id:
        return window.deadline
    return stage["tool_deadline"] - operation_cost(context).seconds


@dataclass(frozen=True)
class ModelTurn:
    run_id: str
    task_id: str | None
    deadline: float


active_model_turn = ContextVar("summary_and_model_turn", default=None)


@contextmanager
def model_turn(context, names):
    from agent.runtime.hooks import active_call
    from agent.runtime.tool_timeout import active_tool_scope

    context.check_active()
    with expert_tool_turn(context, names):
        call = active_call.get()
        task_id = call.task_id if call else None
        turn = None
        if not context.report_started:
            now = monotonic()
            deadline = (
                active_thinking_window.get().deadline
                if current_stage(context) is not None
                else now + coordination_window(context)
            )
            remaining = context.collection_remaining()
            if remaining is not None:
                deadline = min(deadline, now + remaining)
            scope = active_tool_scope.get()
            if scope is not None and scope.deadline is not None:
                deadline = min(deadline, scope.deadline)
            parent = active_model_turn.get()
            if parent is not None and (parent.run_id, parent.task_id) == (context.run_id, task_id):
                deadline = min(deadline, parent.deadline)
            turn = ModelTurn(context.run_id, task_id, deadline)
        token = active_model_turn.set(turn)
        try:
            yield turn
        finally:
            active_model_turn.reset(token)


def model_turn_deadline(context, *, summary=False):
    from agent.runtime.finalization import finalization_active
    from agent.runtime.hooks import active_call

    if context.report_started or finalization_active.get():
        return None
    turn = active_model_turn.get()
    call = active_call.get()
    task_id = call.task_id if call else None
    if turn is None or (turn.run_id, turn.task_id) != (context.run_id, task_id):
        return None
    return turn.deadline - (context.policy.stages.expert_request_min_seconds if summary else 0.0)


def check_model_turn(context, *, summary=False, minimum=0.0):
    from agent.runtime.errors import CollectionEnded

    context.check_active()
    deadline = model_turn_deadline(context, summary=summary)
    if deadline is not None and (deadline <= monotonic() or deadline - monotonic() < minimum):
        error = StageEnded if current_stage(context) is not None else CollectionEnded
        raise error("摘要与后续模型共用的回合窗口不足；保留已有证据和未完成要求进入所属阶段收尾")


def tools_admitted(context, name=None, *, thinking=False, require=False):
    from agent.runtime.document_work import next_document_step
    from agent.runtime.hooks import active_call

    call = active_call.get()
    parameters = call.args if call and name == call.actor.rsplit(":", 1)[-1] and not thinking else None
    cost = operation_cost(context, name, parameters)
    available = stage_remaining(context, tools=True)
    window = active_thinking_window.get()
    reserve = max(cost.seconds, window.operation_seconds) if thinking and window is not None else cost.seconds
    minimum = reserve + (context.policy.stages.expert_request_min_seconds if thinking else 0.0)
    allowed = not context.report_started and (available is None or available >= minimum)
    thinking_left = window.deadline - monotonic() if thinking and window is not None else None
    if thinking_left is not None:
        allowed &= thinking_left >= context.policy.stages.expert_request_min_seconds
    stage = current_stage(context)
    if not allowed and stage is not None:
        reason = (
            f"当前分工预算不足：{'思考及下一操作' if thinking else '当前操作'}至少需要{minimum:g}秒，"
            f"取证窗口剩余{max(0.0, available):.3f}秒"
            + (f"，本次思考窗口剩余{max(0.0, thinking_left):.3f}秒" if thinking_left is not None else "")
            + "；保留已得证据和未读范围交接"
        )
        record_decision(
            context,
            Admission(
                False,
                "handoff",
                reason,
                stage["item_id"],
                available,
                0.0,
                stage["future_reserve_seconds"],
                0.0,
                monotonic(),
                dict(
                    next_document_step=next_document_step(context),
                    minimum_seconds=minimum,
                    operation_seconds=cost.seconds,
                    thinking_deadline=window.deadline if window else None,
                    thinking_seconds=thinking_left,
                ),
            ),
            "expert_thinking" if thinking else "expert_tools",
            name,
        )
        row = context.task_results[call.task_id]
        if row["budget_handoff"] is None:
            from agent.runtime.hooks import failure_details

            row["budget_handoff"] = failure_details(call, error=StageEnded(reason))
            row["budget_handoff"]["stage"] = "disclosure" if thinking else "admission"
        if require:
            raise StageEnded(reason)
    return allowed


async def run_stage(context, task_id, operation):
    """A non-cooperative executor cannot keep the coordinator waiting past cleanup."""
    stage = context.task_results[task_id]["stage_budget"]
    task = asyncio.create_task(operation(), name="stage:" + task_id)

    def finished(completed):
        if not completed.cancelled():
            completed.exception()
        if context.stop_status:
            context.scheduler.forget_run(context.run_id)

    try:
        done, _ = await asyncio.wait({task}, timeout=max(0.0, stage["handoff_deadline"] - monotonic()))
        if done:
            return task.result()
        raise StageEnded("阶段交接时限已到，已停止当前分工并保留有效证据")
    finally:
        if not task.done():
            task.cancel()
            done, _ = await asyncio.wait(
                {task}, timeout=max(0.0, min(context.policy.timeouts.shutdown_seconds, stage["deadline"] - monotonic()))
            )
            if not done:
                task.add_done_callback(finished)
                error = StageCleanupFailed("当前阶段未在清理边界内停止；禁止后续取证或重复最终汇总")
                context.terminate(error)
                raise error
        if not task.cancelled():
            task.exception()


def run_stage_sync(context, task_id, operation):
    """Bound the sync coordinator's wait; an unresponsive branch stops the whole run."""
    stage = context.task_results[task_id]["stage_budget"]
    result = Future()
    execution_context = copy_context()

    def execute():
        try:
            result.set_result(execution_context.run(operation))
        except BaseException as error:
            result.set_exception(error)
        finally:
            if context.stop_status:
                context.scheduler.forget_run(context.run_id)

    Thread(target=execute, name="stage:" + task_id, daemon=True).start()
    try:
        while True:
            context.check_active()
            wait = stage["handoff_deadline"] - monotonic()
            if wait <= 0:
                raise StageEnded("同步分工阶段交接时限已到，拒绝迟到结果")
            try:
                return result.result(timeout=min(wait, 0.05))
            except TimeoutError:
                if result.done():
                    raise
    finally:
        if not result.done():
            wait = max(0.0, min(context.policy.timeouts.shutdown_seconds, stage["deadline"] - monotonic()))
            try:
                result.result(timeout=wait)
            except TimeoutError:
                if not result.done():
                    error = StageCleanupFailed("同步阶段未在清理边界内停止；禁止后续取证或重复最终汇总")
                    context.terminate(error)
                    raise error from None
            except BaseException:
                pass  # The original stage/cancellation boundary remains the owning outcome.


def close_limited_work(context, decision, item_ids=None):
    from agent.runtime.required_work import limit_required_work

    if decision.code in ("time_budget", "call_budget", "task_limit", "network_forbidden"):
        limit_required_work(
            context, item_ids if item_ids is not None else investigation_items(context), decision.code, decision.reason
        )


def coordination_window(context):
    """Reserve an actual next-stage window before starting a tool-enabled root request."""
    decision = admit_stage(context)
    record_decision(context, decision, "coordination")
    if decision.code in ("time_budget", "call_budget", "task_limit", "network_forbidden"):
        return 0.0
    return decision.coordination_window_seconds
