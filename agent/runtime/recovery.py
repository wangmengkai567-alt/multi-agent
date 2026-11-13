"""Runtime-owned retry decisions. No model may schedule its own transport retry."""

import random
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from agent.runtime.context import get_run_context

READ_ONLY = frozenset({"internet_search", "read_web_page", "list_sql_tables", "get_table_data", "execute_sql_query"})


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


# 解析服务端 Retry-After，返回等待秒数；无法解析时返回 None。
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


# 只允许只读操作或工具自身声明了安全保障的调用重试。
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


# 根据错误类别、次数、剩余时间和预算决定是否重试；返回延迟秒数或 None。
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
        # 限流响应若提供 Retry-After，等待时间至少达到服务端要求。
        delay = max(delay, retry_after(error) or 0)
    from agent.runtime.stage_budget import stage_remaining

    remaining = stage_remaining(context, tools=True)
    if (
        context.report_started
        or remaining is not None
        and delay + min(policy_for_run().timeouts.tool_seconds, 1.0) >= remaining
    ):
        call.workflow_data["retry_blocked"] = "剩余时长预算不足，未安排重试"
        return None
    with context.budget_lock:
        from agent.runtime.policies import calls_available

        if not calls_available("tool", "retry"):
            call.workflow_data["retry_blocked"] = "重试或工具调用 budget 预算不足，未安排重试"
            return None
    return delay
