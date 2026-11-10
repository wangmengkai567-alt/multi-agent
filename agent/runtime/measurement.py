"""Measure complete serialized model requests without persisting their contents."""

import json
from contextvars import ContextVar
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from agent.runtime.context import get_run_context

active_request = ContextVar("active_model_request", default=None)
PHASE_LABELS = {"research": "资料收集", "coordination": "任务协调", "summarization": "上下文摘要"}


def persist(context):
    from agent.runtime.checkpoints import write_json

    write_json(
        context.work_dir / "model-requests.json",
        dict(run_id=context.run_id, requests=context.request_ledger, totals=totals(context)),
    )


def totals(context):
    rows = list(context.request_ledger.values())
    measured = [r for r in rows if r["transport"] is not None]
    supplied = [r for r in measured if r["usage_available"]]
    return dict(
        invocations=len(rows),
        transport_attempts=len(measured),
        usage_reports=len(supplied),
        unknown_usage=len(measured) - sum(r["usage_available"] for r in measured),
        unknown_input_usage=sum(r["input_units"] is None for r in measured),
        unknown_output_usage=sum(r["output_units"] is None for r in measured),
        request_bytes=sum(r["transport"]["request_bytes"] for r in measured),
        reported_input_units=sum(r["input_units"] for r in supplied if r["input_units"] is not None),
        reported_output_units=sum(r["output_units"] for r in supplied if r["output_units"] is not None),
        unit="tokens",
    )


# 跟踪一次模型调用的排队、传输、耗时与服务商返回的 Token 用量，不保存请求正文。
class RequestMeasurement:
    def __init__(self, category, phase, messages):
        from agent.runtime.hooks import active_call

        self.context = get_run_context()
        if self.context is None or phase not in PHASE_LABELS:
            raise ValueError("Model measurement requires a run and a known request phase")
        call = active_call.get()
        self.started = None
        self.closed = False
        self.row = dict(
            request_id=str(uuid4()),
            run_id=self.context.run_id,
            category=category,
            phase=phase,
            actor=call.target if call and call.target else "main",
            task_id=call.task_id if call else None,
            parent_call_id=call.call_id if call else self.context.run_id,
            status="queued",
            created_at=datetime.now(UTC).isoformat(),
            queue_wait_ms=None,
            elapsed_ms=None,
            input_chars=sum(len(str(item.content)) for item in messages),
            output_chars=0,
            tool_call_count=0,
            invalid_tool_count=0,
            transport=None,
            context_admission=None,
            usage_available=False,
            input_units=None,
            output_units=None,
            reasoning_units=None,
            cached_units=None,
            unit="tokens",
            error_type=None,
        )
        with self.context.budget_lock:
            self.context.request_ledger[self.row["request_id"]] = self.row
            persist(self.context)
        self.token = active_request.set(self)
        self.wait_started = monotonic()

    def admit(self):
        with self.context.budget_lock:
            self.row["queue_wait_ms"] = round((monotonic() - self.wait_started) * 1000)

    def start(self):
        self.started = monotonic()
        with self.context.budget_lock:
            self.row.update(status="running")
            persist(self.context)

    def finish(self, message, error):
        from agent.runtime.events import make_event

        try:
            with self.context.budget_lock:
                # 标准化用量可能把缺失项填成 0，因此只采用服务商原始用量，缺失项记为未知。
                usage = ((message.response_metadata or {}).get("token_usage") or {}) if message is not None else {}

                def units(value):
                    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

                input_units, output_units = units(usage.get("prompt_tokens")), units(usage.get("completion_tokens"))
                self.row.update(
                    status="completed"
                    if error is None
                    else "cancelled"
                    if type(error).__name__ == "CancelledError"
                    else "failed"
                    if self.started is not None
                    else "rejected",
                    queue_wait_ms=self.row["queue_wait_ms"]
                    if self.row["queue_wait_ms"] is not None
                    else round((monotonic() - self.wait_started) * 1000),
                    elapsed_ms=round((monotonic() - self.started) * 1000) if self.started is not None else None,
                    output_chars=len(message.text) if message is not None else 0,
                    tool_call_count=len(message.tool_calls) if message is not None else 0,
                    invalid_tool_count=len(message.invalid_tool_calls) if message is not None else 0,
                    usage_available=input_units is not None or output_units is not None,
                    input_units=input_units,
                    output_units=output_units,
                    reasoning_units=units((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")),
                    cached_units=units((usage.get("prompt_tokens_details") or {}).get("cached_tokens")),
                    error_type=type(error).__name__ if error is not None else None,
                )
                if self.row["transport"] is not None and self.row["transport"]["status"] == "started":
                    self.row["transport"]["status"] = "uncertain"
                persist(self.context)
                if self.context.event_sink:
                    data = {
                        k: v
                        for k, v in self.row.items()
                        if k not in ("actor", "parent_call_id", "run_id", "status", "transport")
                    }
                    data.update(self.row["transport"] or {})
                    self.context.event_sink.submit(
                        make_event(
                            self.context.thread_id,
                            self.context.run_id,
                            "model_request",
                            PHASE_LABELS[self.row["phase"]],
                            data,
                            actor=self.row["actor"],
                            status=self.row["status"],
                            call_id=self.row["request_id"],
                            parent_call_id=self.row["parent_call_id"],
                        )
                    )
        finally:
            self.closed = True
            active_request.reset(self.token)


# HTTP 发送前重新检查实际请求大小并记录字节数；同一次调用只允许一次发送。
def observe_http_request(request):
    """Runs at the SDK's public HTTP send hook; headers and text never enter the ledger."""
    measurement = active_request.get()
    if measurement is None or measurement.closed or measurement.started is None:
        raise RuntimeError("Model HTTP request has no active owned measurement scope")
    context, row = measurement.context, measurement.row
    context.check_active()
    raw = request.content
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Model request body must be a JSON object")

    def size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    from agent.runtime.model_context import admit_context

    if not isinstance(data.get("model"), str) or not isinstance(data.get("messages"), list):
        raise ValueError("Model context admission requires a model ID and chat messages")
    admit_context(measurement, data, request_bytes=len(raw))
    with context.budget_lock:
        if row["transport"] is not None:
            raise RuntimeError("Model SDK attempted a second HTTP send; implicit retries/redirects are forbidden")
        row["transport"] = dict(
            status="started",
            request_bytes=len(raw),
            request_chars=len(raw.decode("utf-8")),
            messages_bytes=size(data["messages"]) if "messages" in data else 0,
            tools_bytes=size(data["tools"]) if "tools" in data else 0,
            schema_bytes=size(data["response_format"]) if "response_format" in data else 0,
            component_encoding="canonical_utf8_json",
            tool_definition_count=len(data.get("tools", [])),
            http_status=None,
            headers_ms=None,
        )
        persist(context)
        measurement.http_started = monotonic()


# 记录服务商响应状态和收到响应头的耗时，不记录响应内容。
def observe_http_response(response):
    measurement = active_request.get()
    if measurement is None or measurement.row["transport"] is None:
        raise RuntimeError("Unowned model HTTP response")
    with measurement.context.budget_lock:
        measurement.row["transport"].update(
            status="received",
            http_status=response.status_code,
            headers_ms=round((monotonic() - measurement.http_started) * 1000),
        )
        persist(measurement.context)


async def aobserve_http_request(request):
    observe_http_request(request)


async def aobserve_http_response(response):
    observe_http_response(response)
