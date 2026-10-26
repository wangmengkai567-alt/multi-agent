"""Common admission and delivery boundary for interactive rounds."""

from uuid import uuid4

from agent.runtime.context import get_run_context
from agent.runtime.events import make_event


def register_turn(message):
    context = get_run_context()
    if context is None or not getattr(message, "tool_calls", None):
        return message
    from agent.runtime.hooks import active_call

    parent = active_call.get()
    owner = parent.call_id if parent else context.run_id
    turn = str(uuid4())
    calls = []
    with context.budget_lock:
        for original in message.tool_calls:
            call = dict(original)
            if context.enforce_plan:
                call["id"] = "call_" + uuid4().hex
                context.original_call_ids[call["id"]] = original.get("id")
            context.tool_turns[(owner, call["id"])] = turn
            calls.append(call)
    return message.model_copy(update={"tool_calls": calls})


def near_budget(context):
    policy = context.policy
    if policy is None:
        return False
    from agent.runtime.capacity import pending_seconds
    from agent.runtime.policies import calls_available

    remaining = context.remaining()
    waiting = any(
        key != "delivery"
        and row["owner"] is None
        and any(amount for stage in row["stages"].values() for amount in stage.values())
        for key, row in context.capacity_reservations.items()
    )
    counts = context.call_counts
    pressure = not waiting and (
        sum(counts.values()) >= policy.budgets.total * policy.budgets.near_ratio
        or any(
            counts[k] >= getattr(policy.budgets, k) * policy.budgets.near_ratio
            for k in ("tool", "sub_model", "main_model", "review")
        )
    )
    return (
        pressure
        or not calls_available("main_model", phase="coordination")
        or remaining is not None
        and remaining <= pending_seconds(context) + policy.timeouts.shutdown_seconds
    )


def announce(context, reason):
    if context.collecting:
        from agent.runtime.capacity import begin_delivery

        begin_delivery(context)
        if context.event_sink:
            context.event_sink.submit(
                make_event(
                    context.thread_id,
                    context.run_id,
                    "budget_finalizing",
                    "预算接近阈值，停止资料收集并保留综合与检查步骤",
                    {"reason": reason},
                    status="partial",
                    call_id=context.run_id,
                )
            )
