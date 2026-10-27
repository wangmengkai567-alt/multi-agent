"""Common admission and delivery boundary for interactive rounds."""

from uuid import uuid4

from agent.runtime.context import get_run_context


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
            call["id"] = "call_" + uuid4().hex
            context.original_call_ids[call["id"]] = original.get("id")
            context.tool_turns[(owner, call["id"])] = turn
            calls.append(call)
    return message.model_copy(update={"tool_calls": calls})
