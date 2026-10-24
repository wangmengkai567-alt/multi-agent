"""Measure complete serialized model requests without persisting their contents."""

import json
from contextvars import ContextVar
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from agent.runtime.context import get_run_context

active_request = ContextVar("active_model_request", default=None)
PHASE_LABELS = {
    "research": "资料收集",
    "report_generation": "事实报告生成",
    "report_revision": "事实报告修订",
    "semantic_review": "证据审核",
    "review_revision": "审核格式修订",
    "final_review": "终稿审核",
    "direct_review": "直接答复审核",
    "coordination": "任务协调",
    "task_contract": "任务契约",
    "summarization": "上下文摘要",
    "final_generation": "终稿生成",
    "final_revision": "终稿修订",
}


def persist(context):
    from agent.runtime.checkpoints import save_job_checkpoint, write_json

    write_json(
        context.work_dir / "model-requests.json",
        dict(scope_id=context.research_scope_id, requests=context.request_ledger, totals=totals(context)),
    )
    save_job_checkpoint(context)


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
            task_id=call.search_task_key if call else None,
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
                # The adapter synthesizes zero for missing dimensions in normalized
                # usage_metadata. Use only the supplier's preserved raw usage instead.
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

    drafts = []
    for message in data.get("messages", []):
        content = message.get("content")
        if isinstance(content, str):
            try:
                value = json.loads(content)
            except ValueError:
                value = None
            if isinstance(value, dict):
                drafts.extend(value[key] for key in ("draft", "untrusted_previous_draft") if key in value)
        if row["phase"] == "review_revision" and message.get("role") == "assistant":
            drafts.append(content)

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
            revision_draft_bytes=sum(size(d) for d in drafts),
            component_encoding="canonical_utf8_json",
            tool_definition_count=len(data.get("tools", [])),
            http_status=None,
            headers_ms=None,
        )
        persist(context)
        measurement.http_started = monotonic()


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


def restore_measurements(context):
    """Reconcile a durable observation written just before a checkpoint update."""
    from agent.runtime.context import RunStopped

    path = context.work_dir / "model-requests.json"
    if path.exists():
        saved = json.loads(path.read_text("utf-8"))
        if saved["scope_id"] != context.research_scope_id:
            context.stop("interrupted")
            raise RunStopped("Model request ledger belongs to another research scope")
        observed = saved["requests"]
        if set(context.request_ledger) - set(observed):
            context.stop("interrupted")
            raise RunStopped("Model request ledger lost checkpointed observations")
        for key, row in observed.items():
            previous = context.request_ledger.get(key)
            if (
                row["request_id"] != key
                or previous is None
                and row["transport"] is not None
                or previous is not None
                and any(
                    previous[field] != row[field]
                    for field in ("request_id", "run_id", "category", "phase", "parent_call_id")
                )
            ):
                context.stop("interrupted")
                raise RunStopped("Unowned model request observation")
        context.request_ledger = observed
    # An interrupted supplier call has unknown billing, even after response headers.
    for row in context.request_ledger.values():
        if row["status"] in ("queued", "running"):
            row["status"] = "interrupted"
            if row["transport"] is not None and row["transport"]["status"] == "started":
                row["transport"]["status"] = "uncertain"
