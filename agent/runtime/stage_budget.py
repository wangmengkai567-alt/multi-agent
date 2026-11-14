"""One admission calculation and absolute deadlines for serial evidence stages."""

import asyncio
from concurrent.futures import Future
from contextvars import copy_context
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


def investigation_items(context):
    return {
        key: item
        for key, item in context.required_work.items()
        if item["status"] != "completed"
        and any(condition["evidence_kind"] != "delivery" for condition in item["completion_conditions"])
    }


def admit_stage(context, spec=None):
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
        tool_counts = {
            key: 2 if value and any(condition["page_required"] for condition in value["completion_conditions"]) else 1
            for key, value in planned.items()
        }
        minimums = {
            key: limits.coordination_seconds + limits.expert_minimum(context.policy.timeouts.shutdown_seconds, count)
            for key, count in tool_counts.items()
        }
        selected = item_id if item_id in minimums else next(iter(minimums), None)
        minimum = minimums[selected] if minimums else limits.coordination_seconds
        future = sum(value for key, value in minimums.items() if key != selected)
        usable = (
            remaining
            if remaining is not None
            else context.policy.timeouts.total_seconds - context.policy.timeouts.report_reserve_seconds
        )
        allocation = min(
            (usable - limits.coordination_seconds) * minimum / max(minimum, sum(minimums.values())),
            usable - future - limits.coordination_seconds,
        )
        # The configured coordination value is an admission reserve, not a request cap.
        # A coordinator request may use spare collection time while preserving current
        # expert work and every later stage's full minimum (including coordination).
        coordination = max(
            0.0,
            min(
                context.policy.timeouts.request_seconds,
                usable - future - (minimum - limits.coordination_seconds),
            ),
        )
        code, reason = "allowed", "本阶段与后续协调、取证、交接及清理均有预算"
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
        elif usable < minimum + future:
            code, reason = "time_budget", "剩余阶段预算不足以覆盖协调、专家取证、交接、清理及后续事项"
        elif len(context.task_results) >= context.policy.search.max_tasks_per_run:
            code, reason = "task_limit", "本轮委派数量额度已耗尽"
        elif not calls_available_for(
            context,
            *(
                ("main_model",) * (len(planned) + 1)
                + ("sub_model",) * sum(count + 1 for count in tool_counts.values())
                + ("tool",) * sum(count + 2 for count in tool_counts.values())
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
            code == "allowed", code, reason, item_id, remaining, max(0.0, allocation), future, coordination, decided_at
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
    stage = current_stage(context)
    if stage is None:
        return None
    return dict(
        item_id=stage["item_id"],
        tool_seconds=max(0.0, stage_remaining(context, tools=True)),
        handoff_seconds=max(0.0, stage_remaining(context)),
        cleanup_deadline_seconds=max(0.0, stage["deadline"] - monotonic()),
        future_reserve_seconds=stage["future_reserve_seconds"],
    )


def check_stage(context):
    stage = current_stage(context)
    if stage is not None and stage_remaining(context) <= 0:
        raise StageEnded("当前分工阶段预算已到，停止取证并保留截止前有效证据，为后续事项留时")


def tools_admitted(context):
    available = stage_remaining(context, tools=True)
    allowed = not context.report_started and (
        available is None
        or available >= context.policy.stages.tool_min_seconds + context.policy.stages.expert_request_min_seconds
    )
    stage = current_stage(context)
    if not allowed and stage is not None:
        record_decision(
            context,
            Admission(
                False,
                "handoff",
                "停止追加取证，使用已得证据及缺口完成阶段交接",
                stage["item_id"],
                available,
                0.0,
                stage["future_reserve_seconds"],
                0.0,
                monotonic(),
            ),
            "expert_tools",
        )
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
