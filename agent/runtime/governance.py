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
    remaining = context.remaining()
    reserve = min(policy.timeouts.finalize_seconds, policy.timeouts.total_seconds * 0.2)
    counts = context.call_counts
    return (
        remaining is not None
        and remaining <= reserve
        or sum(counts.values()) >= policy.budgets.total * policy.budgets.near_ratio
        or any(
            counts[k] >= getattr(policy.budgets, k) * policy.budgets.near_ratio
            for k in ("tool", "sub_model", "main_model", "review")
            if getattr(policy.budgets, k)
        )
    )


def announce(context, reason):
    if context.collecting:
        context.collecting = False
        if context.event_sink:
            context.event_sink.submit(
                make_event(
                    context.thread_id,
                    context.run_id,
                    "budget_finalizing",
                    "预算接近阈值，优先交付已核实部分",
                    {"reason": reason},
                    status="partial",
                    call_id=context.run_id,
                )
            )


def collection_timeout(context):
    if context.policy is None:
        from agent.runtime.policies import policy_for_run

        context.policy = policy_for_run()
    remaining = context.remaining()
    if remaining is None:
        return None
    reserve = min(context.policy.timeouts.finalize_seconds, context.policy.timeouts.total_seconds * 0.2)
    return max(0.001, remaining - reserve)
