"""Single persistence outlet for runtime events; delivery follows the DB commit."""

import asyncio
import logging
import re
from datetime import UTC, datetime
from threading import Lock
from types import SimpleNamespace
from uuid import uuid4

logger = logging.getLogger(__name__)


class EventPersistenceError(RuntimeError):
    pass


_SECRET_ASSIGNMENT = re.compile(
    r'(?i)(api[_-]?key|access[_-]?token|token|password|secret|authorization|cookie)(\s*[:=]\s*)(?:"[^"\n]*"|\'[^\'\n]*\'|Bearer\s+[^\s,;]+|[^\s,;]+)'
)


def redact_text(value: str) -> str:
    value = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", value)
    value = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", value)
    return re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", value)


def sanitize(value, *, arguments=False, depth=0):
    """Parameter strings and document bodies are omitted, not merely truncated."""
    if depth > 8:
        return "[OMITTED]"
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:50]:
            key = str(key)
            sensitive = any(
                word in key.lower()
                for word in (
                    "password",
                    "secret",
                    "token",
                    "api_key",
                    "apikey",
                    "authorization",
                    "cookie",
                    "content",
                    "document",
                    "body",
                    "description",
                    "instruction",
                    "query",
                    "question",
                    "文本",
                    "内容",
                    "密钥",
                    "密码",
                )
            )
            result[redact_text(key)[:80]] = (
                "[REDACTED]" if sensitive else sanitize(item, arguments=arguments or key == "args", depth=depth + 1)
            )
        return result
    if isinstance(value, list | tuple):
        return [sanitize(item, arguments=arguments, depth=depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return "[REDACTED]" if arguments else redact_text(value)[:512]
    if value is None or isinstance(value, bool | int | float):
        return value
    return "[OMITTED]"


def make_event(
    thread_id, run_id, event, message, data=None, *, actor="main", status="info", call_id=None, parent_call_id=None
):
    from agent.runtime.context import get_run_context

    context = get_run_context()
    label = context.call_labels.get(call_id) if context else None
    if context and call_id in context.original_call_ids:
        data = dict(data or {}, provider_call_id=context.original_call_ids[call_id])
    if label:
        data = dict(data or {}, task_label=label)
    return {
        "thread_id": thread_id,
        "run_id": run_id,
        "event": event,
        "call_id": call_id or str(uuid4()),
        "parent_call_id": parent_call_id,
        "actor": redact_text(actor)[:128],
        "status": status,
        "timestamp": datetime.now(UTC).isoformat(),
        "message": redact_text(message)[:512],
        "data": sanitize(data or {}),
    }


class EventSink:
    def __init__(self, store, deliver):
        self.store = store
        self.deliver = deliver
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()
        self.failures = []
        self.accepting = True
        self.submission_lock = Lock()
        self.worker = self.loop.create_task(self._consume(), name="event-persistence")

    def _dispatch(self, event):
        payload = dict(event, type="monitor_event")
        try:
            self.deliver(payload, event["thread_id"])
        except Exception:
            # Notification transport has no authority to change business status.
            logger.exception("Saved event delivery failed: run=%s seq=%s", event["run_id"], event["event_seq"])

    def submit(self, event):
        """Sync adapter captures immutable event data before crossing threads."""
        with self.submission_lock:
            if not self.accepting or self.loop.is_closed():
                raise EventPersistenceError("Event outlet is closed; event was not saved")
            try:
                same_loop = asyncio.get_running_loop() is self.loop
            except RuntimeError:
                same_loop = False
            if same_loop:
                self.queue.put_nowait(event)
            else:
                self.loop.call_soon_threadsafe(self.queue.put_nowait, event)

    async def _consume(self):
        while True:
            event = await self.queue.get()
            try:
                if event is None:
                    return
                subtask = event.pop("_subtask", None)
                saved = (
                    await self.store.record_subtask(subtask, event)
                    if subtask is not None
                    else await self.store.append_event(event)
                )
                if saved is not None:
                    self._dispatch(saved)
            except Exception as exc:
                self.failures.append((event["run_id"], exc))
                logger.exception("Event was NOT saved: run=%s", event["run_id"])
            finally:
                self.queue.task_done()

    def submit_subtask(self, record, event):
        self.submit(dict(event, _subtask=dict(record)))

    async def flush(self, run_id=None):
        # Flush thread-safe queue insertions scheduled by synchronous tools.
        await asyncio.sleep(0)
        await self.queue.join()
        failures = [item for item in self.failures if run_id is None or item[0] == run_id]
        self.failures = [item for item in self.failures if run_id is not None and item[0] != run_id]
        if failures:
            raise EventPersistenceError(
                f"{len(failures)} event(s) were not saved for run {run_id or 'all'}"
            ) from failures[0][1]

    async def create(self, thread_id, query, run_id, *, job_id=None):
        event = make_event(thread_id, run_id, "run_queued", "任务已接收", actor="main", status="queued", call_id=run_id)
        record = await self.store.create(thread_id, redact_text(query), run_id=run_id, event=event, job_id=job_id)
        self._dispatch(record.pop("saved_event"))
        return record

    async def start(self, context):
        event = make_event(
            context.thread_id, context.run_id, "run_started", "任务开始执行", status="running", call_id=context.run_id
        )
        saved = await self.store.start(context.run_id, event=event)
        if saved:
            self._dispatch(saved)
        return bool(saved)

    async def finish(self, context, status, *, answer=None, error=None):
        recording_error = None
        try:
            await self.flush(context.run_id)
        except EventPersistenceError as exc:
            recording_error = exc
        if status != "completed" and not getattr(context, "partial_answer", False):
            answer = None
        answer = redact_text(answer) if answer is not None else None
        error = redact_text(error) if error is not None else None
        kind = "task_result" if status == "completed" else "error" if status == "failed" else f"run_{status}"
        data = {"result": answer} if status == "completed" else {"error": error, "partial_result": answer}
        from agent.runtime.completion import completion_snapshot

        data["completion"] = completion_snapshot(context, status)
        data["coverage"] = getattr(context, "coverage", "unknown")
        draft = getattr(context, "delivery_draft", None)
        if draft and draft.get("source_status") is not None:
            data["source_status"] = draft["source_status"]
        if getattr(context, "pending_jobs", None):
            data["jobs"] = list(context.pending_jobs.values())
        if hasattr(context, "call_counts"):
            data["call_counts"] = dict(context.call_counts)
        hook_failures = getattr(context, "hook_failures", [])
        if recording_error or hook_failures:
            data.update(
                recording_status="incomplete",
                recording_errors=sanitize([str(recording_error)] if recording_error else hook_failures),
            )
        event = make_event(
            context.thread_id,
            context.run_id,
            kind,
            "任务执行完成" if status == "completed" else "任务执行结束",
            status=status,
            call_id=context.run_id,
        )
        # Final answers/errors are durable business output, not raw tool parameters.
        event["data"] = data
        saved = await self.store.finish(
            context.run_id,
            status,
            answer=answer,
            error=error,
            event=event,
            coverage=getattr(context, "coverage", "unknown"),
        )
        if saved:
            self._dispatch(saved)
        if recording_error:
            raise recording_error
        return bool(saved)

    async def interrupt_active(self, reason):
        for record in await self.store.list_active():
            context = SimpleNamespace(**record)
            await self.finish(context, "interrupted", error=reason)

    async def close(self):
        with self.submission_lock:
            if not self.accepting:
                return
            self.accepting = False
        try:
            await self.flush()
        finally:
            self.queue.put_nowait(None)
            await self.worker
