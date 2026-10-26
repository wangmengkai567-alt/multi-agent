"""Complete-request context admission, adapted from Codex's full-window guard.

UTF-8 JSON bytes plus framing reserve are deliberately conservative text token
estimates for Qwen's byte-level text tokenizer, never supplier billing figures.
No content is truncated or a different model selected when admission fails.
"""

import json

from langchain_core.messages import convert_to_openai_messages
from langchain_core.runnables.base import RunnableBindingBase

from agent.runtime.errors import ContextCapacityExceeded
from agent.runtime.policies import MissingConfiguration, policy_for_run


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def prepared_payload(messages, delegate, kwargs):
    """Include bound tools, response schemas and model options before charging a call."""
    bindings = []
    while isinstance(delegate, RunnableBindingBase):
        bindings.append(delegate.kwargs)
        delegate = delegate.bound
    payload = dict(getattr(delegate, "model_kwargs", None) or {})
    for name in ("model_name", "max_tokens", "extra_body"):
        value = getattr(delegate, name, None)
        if value is not None:
            payload["model" if name == "model_name" else name] = value
    for binding in reversed(bindings):
        payload.update(binding)
    payload.update({key: value for key, value in kwargs.items() if key != "budget_retry"})
    payload.update(payload.pop("extra_body", None) or {})
    payload["messages"] = convert_to_openai_messages(messages)
    return payload


def positive_limit(payload, key, maximum):
    value = payload.get(key, maximum)
    if type(value) is not int or not 1 <= value <= maximum:
        raise MissingConfiguration(f"{key} must be an integer between 1 and {maximum}")
    return value


def context_status(payload, *, request_bytes=None):
    limits = policy_for_run().context
    request_bytes = encoded_size(payload) if request_bytes is None else request_bytes
    messages = payload.get("messages", [])
    tools = payload.get("tools", [])
    status = dict(
        admitted=request_bytes <= limits.max_request_bytes,
        request_bytes=request_bytes,
        request_byte_limit=limits.max_request_bytes,
        estimate_method="utf8_json_bytes_plus_framing",
        estimated_input_units=request_bytes + 32 * len(messages) + 256 * len(tools),
        messages_bytes=encoded_size(messages),
        tools_bytes=encoded_size(tools),
        schema_bytes=encoded_size(payload.get("response_format")),
        input_unit_limit=None,
    )
    # Preparation of a generic in-process model can have no wire model ID.
    # Every production HTTP payload must have one and is checked again at send.
    model_id = payload.get("model")
    if model_id is None:
        return status
    model = limits.models.get(model_id)
    if model is None:
        raise MissingConfiguration(f"No verified context limits configured for model {model_id}")
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and any(block.get("type") != "text" for block in content):
            raise MissingConfiguration("Context admission currently supports text input only")
    thinking = payload.get("enable_thinking", True)
    if type(thinking) is not bool:
        raise MissingConfiguration("enable_thinking must be a boolean")
    if "max_completion_tokens" in payload and "max_tokens" in payload:
        raise MissingConfiguration("Configure only one output length parameter")
    if "max_completion_tokens" in payload:
        output_tokens = positive_limit(payload, "max_completion_tokens", model.max_output_tokens)
        reasoning_tokens = 0  # The supplier defines this parameter as total generated tokens.
    else:
        output_tokens = positive_limit(payload, "max_tokens", model.max_output_tokens)
        reasoning_tokens = model.max_thinking_tokens if thinking else 0
        if thinking and "thinking_budget" in payload:
            reasoning_tokens = positive_limit(payload, "thinking_budget", model.max_thinking_tokens)
        if thinking and "reasoning_effort" in payload:
            if "thinking_budget" in payload:
                raise MissingConfiguration("Qwen cannot combine reasoning_effort and thinking_budget")
            budgets = {"low": 4096, "medium": 16384, "xhigh": 262144}
            effort = payload["reasoning_effort"]
            if effort not in budgets:
                raise MissingConfiguration("Unverified Qwen reasoning_effort setting")
            reasoning_tokens = budgets[effort]
    input_limit = min(
        model.max_thinking_input_tokens if thinking else model.max_input_tokens,
        model.window_tokens - output_tokens - reasoning_tokens - limits.safety_tokens,
    )
    status.update(
        model=model_id,
        window_units=model.window_tokens,
        output_reserved_units=output_tokens,
        reasoning_reserved_units=reasoning_tokens,
        safety_units=limits.safety_tokens,
        input_unit_limit=input_limit,
        admitted=status["admitted"] and status["estimated_input_units"] <= input_limit,
    )
    return status


def admit_context(measurement, payload, *, request_bytes=None):
    status = context_status(payload, request_bytes=request_bytes)
    with measurement.context.budget_lock:
        measurement.row["context_admission"] = status
    if not status["admitted"]:
        byte_limit_exceeded = status["request_bytes"] > status["request_byte_limit"]
        raise ContextCapacityExceeded(
            "完整模型请求超过上下文容量，尚未发送",
            used=status["request_bytes"] if byte_limit_exceeded else status["estimated_input_units"],
            limit=status["request_byte_limit"] if byte_limit_exceeded else status["input_unit_limit"],
        )
