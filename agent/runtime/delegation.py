"""One lifecycle owner for sequential, runtime-identified expert execution.

Based on openai/codex multi_agents_v2/spawn.rs: model supplies the task,
while the runtime owns identity, permission, execution state and returned results.
"""

import asyncio
import json
from contextlib import ExitStack
from datetime import UTC, datetime
from hashlib import sha256
from time import monotonic
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import Runnable

from agent.reflection import source_urls, tool_evidence
from agent.results import clean, delegation_payload, sync_result, task_evidence
from agent.runtime.checkpoints import write_json
from agent.runtime.context import get_run_context
from agent.runtime.errors import ExecutionTerminated
from agent.runtime.events import EventPersistenceError, make_event
from agent.runtime.evidence_protocol import EvidenceError
from agent.runtime.experts import bind_registry
from agent.runtime.hooks import HookCall, HookRejected, active_call, error_category, error_message, pipeline
from agent.runtime.output import cited_urls, source_page
from agent.runtime.skills import skill_scope


class DelegationRunnable(Runnable):
    def __init__(self, spec, executor, *, hooks=None, registry=None):
        spec.validate()
        self.spec, self.executor, self.registry = spec, executor, registry
        self.hooks = hooks or pipeline

    def _prepare(self, inputs):
        context = get_run_context()
        if context is None:
            raise HookRejected("Expert execution requires a run context")
        context.check_active()
        from agent.runtime.capabilities import expert_available
        from agent.runtime.contracts import assert_contract

        assert_contract(context)
        if not expert_available(context, self.spec.agent_id, self.spec):
            raise HookRejected("Expert is unavailable or forbidden by this run's permissions")
        if self.registry is not None:
            bind_registry(context, self.registry)
        messages = inputs.get("messages", [])
        if not messages or not isinstance(messages[-1], HumanMessage) or not messages[-1].text.strip():
            raise HookRejected("Expert execution requires a non-empty task description")
        parent = active_call.get()
        description = messages[-1].text
        call = HookCall(
            self.spec.agent_id,
            str(uuid4()),
            parent.call_id if parent else context.run_id,
            args={"description": description},
            target=self.spec.agent_id,
            source_kind=self.spec.execution.source_kind,
        )
        call.task_id = call.call_id
        record = dict(
            call_id=call.call_id,
            run_id=context.run_id,
            thread_id=context.thread_id,
            request_call_id=parent.call_id if parent else None,
            target=self.spec.agent_id,
            display_name=self.spec.display_name,
            description="[REDACTED]",
            description_length=len(description),
            description_sha256=sha256(description.encode()).hexdigest(),
            status="queued",
            deadline=context.deadline.isoformat() if context.deadline else None,
            created_at=datetime.now(UTC).isoformat(),
            ended_at=None,
            result=None,
            error=None,
            check_status="not_checked",
        )
        with context.budget_lock:
            context.check_active()
            if len(context.task_results) >= context.policy.search.max_tasks_per_run:
                raise HookRejected("本轮委派数量已达到上限")
            context.task_results[call.call_id] = dict(
                record, sources=[], evidence=[], unfinished=[], limitations=[], error_category=None
            )
            if parent:
                context.delegation_calls.setdefault(parent.call_id, []).append(record)
        return context, call, record

    def _save(self, context, call, record, status):
        # Commit success under the same lock as stop(); late results cannot win.
        with context.budget_lock:
            if status in ("running", "completed"):
                context.check_active()
            record["status"] = call.status = status
            if status not in ("queued", "running"):
                record["ended_at"] = datetime.now(UTC).isoformat()
            context.task_results[call.call_id].update(record)
            if context.event_sink is not None:
                event = make_event(
                    context.thread_id,
                    context.run_id,
                    "subagent_" + status,
                    self.spec.display_name + "：" + status,
                    actor=self.spec.agent_id,
                    status=status,
                    call_id=call.call_id,
                    parent_call_id=call.parent_call_id,
                )
                context.event_sink.submit_subtask(record, event)

    def _start(self, context, call, record):
        context.check_active()
        self.hooks.emit("before_delegate", call)
        self._save(context, call, record, "running")
        call.executed = True

    def _result(self, context, call, record, result, evidence):
        context.check_active()
        sources, unfinished, limitations = task_evidence(evidence, call.task_id)
        row = context.task_results[call.call_id]
        row.update(sources=sources, evidence=list(evidence), unfinished=unfinished, limitations=limitations)
        messages = result.get("messages", []) if isinstance(result, dict) else []
        final = messages[-1] if messages else None
        if not isinstance(final, AIMessage) or final.tool_calls or not final.text.strip():
            raise ValueError("Expert did not return a final assistant answer")
        if self.spec.tools and not sources:
            raise ValueError("子任务未取得有效工具结果，不能声明已完成调查")
        allowed = set(source_urls(evidence))
        allowed.update(url for row in sources for url in cited_urls(row["content"]))
        if {source_page(url) for url in cited_urls(final.text)} - {source_page(url) for url in allowed}:
            raise ValueError("Expert cited a URL absent from its actual tool results")
        record["result"] = clean(final.text)
        call.result = record["result"]
        self._save(context, call, record, "completed")
        return {"messages": [AIMessage(content=json.dumps(delegation_payload(row), ensure_ascii=False))]}

    def _failed(self, context, call, record, error, evidence):
        if isinstance(error, ExecutionTerminated):
            context.terminate(error)
        record["error"] = error_message(error)
        record["result"] = call.result = None
        call.error = error
        status = context.stop_status or ("cancelled" if isinstance(error, asyncio.CancelledError) else "failed")
        row = context.task_results[call.call_id]
        try:
            sources, unfinished, limitations = task_evidence(evidence, call.task_id)
        except EvidenceError as evidence_error:
            sources, limitations = [], []
            unfinished = [
                dict(call_id=call.call_id, tool=self.spec.agent_id, status="failed", reason=str(evidence_error))
            ]
        unfinished.append(dict(call_id=call.call_id, tool=self.spec.agent_id, status=status, reason=record["error"]))
        category = (
            "timeout"
            if status == "timed_out"
            else "cancelled"
            if status in ("cancelled", "interrupted")
            else error_category(error)
        )
        row.update(
            sources=sources,
            evidence=list(evidence),
            unfinished=unfinished,
            limitations=limitations,
            error_category=category,
        )
        self._save(context, call, record, status)
        self.hooks.emit("on_error", call)
        if (
            not isinstance(error, Exception)
            or isinstance(error, ExecutionTerminated | EventPersistenceError)
            or context.stop_status
        ):
            raise error
        return {
            "messages": [
                AIMessage(
                    content=json.dumps(
                        delegation_payload(row),
                        ensure_ascii=False,
                    )
                )
            ]
        }

    def _finish(self, context, call, record, started, evidence):
        call.elapsed_ms = (monotonic() - started) * 1000
        row = context.task_results[call.call_id]
        row["evidence"] = list(evidence)
        write_json(context.work_dir / "results" / (call.call_id + ".json"), row)
        self.hooks.emit("after_subagent", call)

    def _inputs(self, context, inputs):
        # The task stays intact; program-owned materials and permissions are data.
        contract = context.task_contract
        binding = json.dumps(
            dict(
                network_policy=context.network_policy,
                materials=[dict(kind=m.kind, reference=m.reference) for m in contract.materials],
            ),
            ensure_ascii=False,
        )
        return dict(inputs, messages=[HumanMessage(content="本轮材料与权限：" + binding), *inputs["messages"]])

    def invoke(self, input, config=None, **kwargs):
        context, call, record = self._prepare(input)
        token = active_call.set(call)
        skill_token = skill_scope.set((self.spec.agent_id, self.spec.allowed_skill_ids))
        evidence = []
        evidence_token = tool_evidence.set(evidence)
        started = monotonic()
        try:
            self._save(context, call, record, "queued")
            with ExitStack() as stack:
                for gate in (
                    context.scheduler.child_gate(context),
                    context.scheduler.gate("children"),
                    context.scheduler.expert_gate(self.spec.resources),
                ):
                    lease = context.scheduler.acquire(gate, context, "expert")
                    stack.callback(lease.release)
                self._start(context, call, record)
                result = self.executor.invoke(self._inputs(context, input), config, **kwargs)
                return self._result(context, call, record, result, evidence)
        except BaseException as error:
            return self._failed(context, call, record, error, evidence)
        finally:
            try:
                self._finish(context, call, record, started, evidence)
                if context.event_sink:
                    sync_result(context.event_sink.flush(context.run_id), context.event_sink)
            finally:
                tool_evidence.reset(evidence_token)
                skill_scope.reset(skill_token)
                active_call.reset(token)
                if context.stop_status:
                    context.scheduler.forget_run(context.run_id)

    async def ainvoke(self, input, config=None, **kwargs):
        context, call, record = self._prepare(input)
        token = active_call.set(call)
        skill_token = skill_scope.set((self.spec.agent_id, self.spec.allowed_skill_ids))
        evidence = []
        evidence_token = tool_evidence.set(evidence)
        started = monotonic()
        try:
            self._save(context, call, record, "queued")
            with ExitStack() as stack:
                for gate in (
                    context.scheduler.child_gate(context),
                    context.scheduler.gate("children"),
                    context.scheduler.expert_gate(self.spec.resources),
                ):
                    lease = await context.scheduler.aacquire(gate, context, "expert")
                    stack.callback(lease.release)
                self._start(context, call, record)
                result = await self.executor.ainvoke(self._inputs(context, input), config, **kwargs)
                return self._result(context, call, record, result, evidence)
        except BaseException as error:
            return self._failed(context, call, record, error, evidence)
        finally:
            try:
                self._finish(context, call, record, started, evidence)
                if context.event_sink:
                    await context.event_sink.flush(context.run_id)
            finally:
                tool_evidence.reset(evidence_token)
                skill_scope.reset(skill_token)
                active_call.reset(token)
                if context.stop_status:
                    context.scheduler.forget_run(context.run_id)
