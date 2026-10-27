"""Runtime-owned retry decisions. No model may schedule its own transport retry."""

import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from agent.runtime.context import get_run_context

READ_ONLY = frozenset({"list_sql_tables", "get_table_data", "execute_sql_query", "get_research_job"})


class ToolCallTimeout(TimeoutError):
    def __init__(self, name, seconds):
        self.tool_name, self.seconds = name, seconds
        super().__init__(f"工具 {name} 单次调用超时（{seconds:g} 秒）；执行结果可能尚未确定")


def is_transport_timeout(error):
    import httpx
    import requests

    return (
        isinstance(error, TimeoutError | requests.Timeout | httpx.TimeoutException)
        or type(error).__module__ == "tavily.errors"
        and type(error).__name__ == "TimeoutError"
        or getattr(error, "timed_out", False) is True
    )


def http_status(error):
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None) or getattr(error, "status_code", None)


def retry_after(error, now=None):
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None) or getattr(error, "headers", {}) or {}
    value = next((str(v) for k, v in headers.items() if k.lower() == "retry-after"), None)
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            return max(0.0, (date - (now or datetime.now(UTC))).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


def safe_to_retry(request, call):
    name = request.tool_call["name"]
    if name in READ_ONLY:
        return True
    # This declaration belongs to the registered implementation, never model arguments.
    metadata = getattr(getattr(request, "tool", None), "metadata", None) or {}
    if metadata.get("retry_safety") == "idempotency_key":
        key = metadata.get("idempotency_argument")
        return bool(key and isinstance(call.args.get(key), str) and call.args[key].strip())
    return metadata.get("retry_safety") in ("transaction", "compensated")


def retry_delay(request, call, error):
    from agent.runtime.policies import classify_tool_error, policy_for_run

    context = get_run_context()
    if context is None:
        return None
    context.check_active()
    limits = policy_for_run().retry
    if (
        request.tool_call["name"] not in limits.tools
        or call.attempt >= limits.max_retries
        or not safe_to_retry(request, call)
    ):
        return None
    status = http_status(error)
    if status == 409 or status is not None and 400 <= status < 500 and status not in (408, 429):
        return None
    if classify_tool_error(error) not in ("transient", "rate_limited"):
        return None
    cap = min(limits.max_delay_seconds, limits.delay_seconds * 2**call.attempt)
    delay = cap / 2 + random.uniform(0, cap / 2)
    if status == 429:
        # Without a usable header use the configured backoff, never a model guess.
        delay = max(delay, retry_after(error) or 0)
    from agent.runtime.capacity import pending_seconds

    remaining = context.remaining()
    if remaining is not None:
        remaining -= pending_seconds(context)
    if remaining is not None and delay + min(policy_for_run().timeouts.tool_seconds, 1.0) >= remaining:
        call.workflow_data["retry_blocked"] = "剩余时长预算不足，未安排重试"
        return None
    with context.budget_lock:
        from agent.runtime.policies import calls_available

        if not calls_available("tool", "retry"):
            call.workflow_data["retry_blocked"] = "重试或工具调用 budget 预算不足，未安排重试"
            return None
    return delay
