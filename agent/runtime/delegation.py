"""The one lifecycle owner at the actual expert execution boundary."""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from time import monotonic
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import Runnable

from agent.reflection import ResultReviewer, ReviewResult, source_urls, tool_evidence
from agent.results import ResultService, sync_result
from agent.runtime.context import get_run_context
from agent.runtime.errors import ExecutionTerminated
from agent.runtime.events import make_event, redact_text
from agent.runtime.hooks import HookCall, HookRejected, active_call, error_message, pipeline
from agent.runtime.models import ModelRequestTimeout
from agent.runtime.policies import ReservationUnavailable, SearchLimitReached, classify_tool_error, reserve_calls
from agent.runtime.reporting import ReportRevision, ResearchOutputError
from agent.runtime.requirements import apply_coverage, ensure_task
from agent.runtime.review_state import ReviewState, deliverable
from agent.runtime.search import SearchAllowance, needs_search_refinement, search_allowance
from agent.runtime.skills import skill_scope
from agent.runtime.workflows import prepare_delegate


@dataclass(frozen=True)
class ReviewOnly:
    result: dict
    evidence: list


class DelegationRunnable(Runnable):
    def __init__(self, spec, executor, *, hooks=None, reviewer=None, results=None, registry=None):
        spec.validate()
        self.registry = registry
        self.spec, self.executor, self.hooks = spec, executor, hooks or pipeline
        self.reviewer = reviewer or ResultReviewer(spec.review_config)
        self.results = results or ResultService()

    def _prepare(self, inputs):
        context = get_run_context()
        if context is None:
            raise HookRejected("Expert execution requires a run context")
        if self.spec.execution.network_access and context.network_policy == "forbidden":
            raise HookRejected("显式联网限制禁止执行联网专家")
        if self.registry is not None:
            from agent.runtime.experts import bind_registry

            bind_registry(context, self.registry)
        if context.task_contract is not None:
            from agent.runtime.contracts import assert_contract

            contract = assert_contract(context)
            if (
                contract.delivery_kind != "grounded"
                or not contract.network_allowed
                and self.spec.execution.network_access
            ):
                raise HookRejected("Expert execution contradicts the locked task contract")
        with context.budget_lock:
            unavailable = context.unavailable_agents.get(self.spec.agent_id)
        if unavailable:
            raise HookRejected(unavailable + "；本轮不重复调用该助手，请说明依据缺失。")
        messages = inputs.get("messages", [])
        if not messages or not isinstance(messages[-1], HumanMessage) or not messages[-1].text.strip():
            raise HookRejected("Expert execution requires a non-empty task description")
        description = messages[-1].text
        task_id, description, label = None, description, ""
        if self.spec.execution.planned:
            from agent.runtime.task_plan import resolve_task

            task_id, description, label = resolve_task(context, description)
            inputs["messages"] = [*messages[:-1], HumanMessage(content=description)]
        parent = active_call.get()
        request_id = parent.call_id if parent and parent.target == self.spec.agent_id else None
        call = HookCall(
            self.spec.agent_id,
            str(uuid4()),
            request_id or context.run_id,
            args={"description": description},
            target=self.spec.agent_id,
            source_kind=self.spec.execution.source_kind,
        )
        if label:
            context.call_labels[call.call_id] = label
            if request_id:
                context.call_labels[request_id] = label
        record = dict(
            call_id=call.call_id,
            run_id=context.run_id,
            thread_id=context.thread_id,
            request_call_id=request_id,
            target=self.spec.agent_id,
            display_name=self.spec.display_name,
            description="[REDACTED]",
            description_length=len(description),
            description_sha256=sha256(description.encode("utf-8")).hexdigest(),
            status="queued",
            deadline=context.deadline.isoformat() if context.deadline else None,
            created_at=datetime.now(UTC).isoformat(),
            ended_at=None,
            result=None,
            error=None,
            check_status="not_checked",
        )
        if self.spec.execution.planned:
            try:
                call.search_task_key = (
                    task_id
                    or sha256(
                        (
                            " ".join(description.split()) + f"\ninternal_results:{context.internal_result_version}"
                        ).encode("utf-8")
                    ).hexdigest()
                )
                allowance = SearchAllowance(context, call.search_task_key)
                allowance.claim()
                allowance.task.description = description
            except SearchLimitReached as exc:
                raise HookRejected(str(exc)) from exc
        if not self.spec.execution.planned:
            from agent.runtime.requirements import register_support

            call.search_task_key = self.spec.agent_id + ":" + call.call_id
            register_support(context, call.search_task_key)
        else:
            ensure_task(context, call.search_task_key, description)
        from agent.runtime.reservations import hold

        try:
            hold(
                context,
                call.call_id,
                {"sub_model": 1, "review": 1},
                task_id=call.search_task_key,
                protect_revision=self.spec.review_config.max_refinements > 0,
            )
        except ReservationUnavailable as exc:
            if self.spec.execution.planned:
                from agent.runtime.reservations import release

                with context.budget_lock:
                    for owner, reservation in list(context.revision_reservations.items()):
                        if reservation["task_id"] == call.search_task_key:
                            release(context, owner)
                    allowance.task.active = False
                    allowance.task.state = ReviewState(check_status="budget_exhausted")
                    allowance.task.closed_reason = allowance.task.failure_reason = str(exc)
            raise HookRejected(str(exc)) from exc
        record["state"] = ReviewState()
        if request_id:
            with context.budget_lock:
                context.delegation_calls.setdefault(request_id, []).append(record)
        return context, call, record

    def _save(self, context, call, record, status):
        record["status"] = status
        if status not in ("queued", "running"):
            record["ended_at"] = datetime.now(UTC).isoformat()
        if context.event_sink is None:
            return  # Standalone has no persistence claim, but still uses controls.
        event = make_event(
            context.thread_id,
            context.run_id,
            f"subagent_{status}",
            f"{self.spec.display_name}："
            + {
                "queued": "任务已接收",
                "running": "开始执行",
                "completed": "返回结果",
                "failed": "执行失败",
                "rejected": "执行被拒绝",
                "cancelled": "执行已取消",
            }[status],
            {
                "assistant_name": self.spec.display_name,
                "target": self.spec.agent_id,
                "request_call_id": record["request_call_id"],
                "check_status": record["check_status"],
            },
            actor=self.spec.agent_id,
            status=status,
            call_id=call.call_id,
            parent_call_id=call.parent_call_id,
        )
        try:
            context.event_sink.submit_subtask({k: v for k, v in record.items() if k != "state"}, event)
        except Exception as exc:
            logging.getLogger(__name__).exception("Subtask record was NOT saved")
            context.hook_failures.append(f"subtask {call.call_id}: {redact_text(str(exc))}")

    def _start(self, context, call, record):
        self.hooks.emit("before_delegate", call)
        if context.deadline is not None and context.deadline <= datetime.now(UTC):
            raise HookRejected("Run deadline has expired; expert execution was not started")
        self._save(context, call, record, "running")
        call.executed = True

    def _returned(self, call, record, result, evidence):
        messages = result.get("messages", []) if isinstance(result, dict) else []
        final = messages[-1] if messages else None
        if not isinstance(final, AIMessage) or final.tool_calls:
            raise RuntimeError("Expert executor did not return a final assistant message")
        call.review_evidence = list(evidence)
        call.result, call.status = result, "completed"
        record["result"] = redact_text(final.text)
        if self.spec.execution.planned:
            task = SearchAllowance(get_run_context(), call.search_task_key).task
            task.sources = source_urls(evidence)
            task.evidence = list(evidence)
        return result

    def _error(self, call, record, error):
        call.error = error
        call.status = (
            "rejected"
            if isinstance(error, HookRejected)
            else "cancelled"
            if type(error).__name__ == "CancelledError"
            else "failed"
        )
        record["error"] = error_message(error)
        self.hooks.emit("on_error", call)

    def _finish(self, context, call, record, started):
        from agent.runtime.reservations import release

        release(context, call.call_id)
        if self.spec.execution.source_kind == "db" and call.status == "completed" and deliverable(record["state"]):
            with context.budget_lock:
                context.internal_result_version += 1
        if self.spec.execution.planned:
            allowance = SearchAllowance(context, call.search_task_key)
            allowance.task.failure_reason = record["error"] or ""
            allowance.finish(
                executed=call.executed,
                reason=record["error"],
                state=call.state,
                result=record["result"] or "",
                sources=allowance.task.sources,
            )
        from agent.runtime.checkpoints import save_job_checkpoint

        save_job_checkpoint(context)
        call.elapsed_ms = (monotonic() - started) * 1000
        self._save(context, call, record, call.status)
        if call.executed:
            self.hooks.emit("after_subagent", call)

    def _review_event(self, context, call, result, attempt, kind="subagent_review"):
        if context.event_sink is None:
            return
        try:
            context.event_sink.submit(
                make_event(
                    context.thread_id,
                    context.run_id,
                    kind,
                    f"{self.spec.display_name}："
                    + ("补充查询" if kind == "subagent_refinement" else "结果检查：" + result.verdict),
                    dict(result.model_dump(), attempt=attempt),
                    actor=self.spec.agent_id,
                    status=result.verdict,
                    call_id=call.call_id,
                    parent_call_id=call.parent_call_id,
                )
            )
        except Exception as exc:
            context.hook_failures.append(f"review {call.call_id}: {redact_text(str(exc))}")

    def _checked(self, context, call, record, result, review, attempt):
        from agent.runtime.facts import render_report

        call.state = record["state"] = review.state
        call.check_status = record["check_status"] = review.state.check_status
        call.report_attempt = attempt
        self._review_event(context, call, review, attempt)
        if review.coverage:
            apply_coverage(context, call.search_task_key, review.coverage)
        else:
            from agent.runtime.requirements import task_requirements

            apply_coverage(
                context,
                call.search_task_key,
                {
                    r["rid"]: dict(status="unknown", fact_ids=[], claim_ids=[])
                    for r in task_requirements(context, call.search_task_key)
                },
            )
        if self.spec.execution.planned:
            task = SearchAllowance(context, call.search_task_key).task
            task.state = review.state
            task.report = review.accepted_report if deliverable(review.state) else None
            task.current_result_id, task.report_attempt = call.current_result_id, attempt
            task.result_run_id = context.run_id
        if deliverable(review.state):
            report = review.accepted_report
            if report is None:
                raise ValueError("Deliverable state has no reviewed report")
            record["result"] = render_report(report)
            if self.spec.execution.planned:
                task = SearchAllowance(context, call.search_task_key).task
                task.result = record["result"]
            else:
                import copy

                internal = copy.deepcopy(report)
                mapping = {f["id"]: f"{call.search_task_key}:{attempt}:{f['id']}" for f in internal["facts"]}
                for fact in internal["facts"]:
                    fact["id"] = mapping[fact["id"]]
                for item in internal["analysis"]:
                    item["fact_ids"] = [mapping[k] for k in item["fact_ids"]]
                with context.budget_lock:
                    context.approved_internal[call.call_id] = dict(
                        text=record["result"],
                        evidence=call.review_evidence,
                        report=internal,
                        result_id=call.current_result_id,
                        result_run_id=context.run_id,
                        state=review.state.model_dump(),
                        task_id=call.search_task_key,
                        report_attempt=attempt,
                    )
            parent_evidence = tool_evidence.get()
            if parent_evidence is not None:
                import copy

                known = {(e["name"], e["call_id"], e.get("attempt", 0)) for e in parent_evidence}
                for item in call.review_evidence:
                    identity = (item["name"], item["call_id"], item.get("attempt", 0))
                    if identity not in known:
                        parent_evidence.append(dict(copy.deepcopy(item), delegated_from=call.search_task_key))
                        known.add(identity)
            content = dict(verified_report=report, state=review.state.model_dump(), coverage=review.coverage)
        else:
            record["result"] = ""
            content = dict(state=review.state.model_dump(), missing=review.missing_evidence, error=review.error)
        final = result["messages"][-1].model_copy(update={"content": json.dumps(content, ensure_ascii=False)})
        result = dict(result, messages=[*result["messages"][:-1], final])
        call.result, call.status = result, "completed" if review.state.judged else "failed"
        return result

    def _refine(self, context, call, input, review, attempt, result, evidence, record):
        if review.verdict == "check_incomplete" and review.error == "ReviewFormatError":
            return None
        report_invalid = review.verdict == "check_incomplete" and review.error == "ReportError"
        review_only = review.verdict == "check_incomplete" and not report_invalid
        if (
            review.verdict != "insufficient" and not report_invalid and not review_only
        ) or attempt >= self.spec.review_config.max_refinements:
            return None
        if context.deadline is not None and context.deadline <= datetime.now(UTC):
            return None
        from agent.runtime.reservations import hold

        collection = context.collection_states.get(call.search_task_key)
        allow_search = (
            not review_only
            and not (collection and collection["closed_reason"])
            and needs_search_refinement(review, evidence)
        )
        remaining = context.remaining()
        requests = 1 if review_only else 3 if allow_search else 2
        needed = requests * context.policy.timeouts.request_seconds
        if allow_search:
            needed += 2 * context.policy.timeouts.tool_seconds
        if remaining is not None and remaining <= needed:
            record["error"] = "time_exhausted: 未启动无法在总时限内完成的修订"
            self._review_event(
                context, call, review.model_copy(update={"error": "time_exhausted"}), attempt + 1, "revision_stopped"
            )
            return None
        amounts = {"review": 1, "retry": 1}
        if not review_only:
            amounts["sub_model"] = 2 if allow_search else 1
        if allow_search:
            amounts.update(tool=2, search=1, read=1)
        try:
            hold(context, call.call_id, amounts, activate=True, task_id=call.search_task_key)
        except ReservationUnavailable:
            record["error"] = "reserve_exhausted"
            self._review_event(
                context, call, review.model_copy(update={"error": "reserve_exhausted"}), attempt + 1, "revision_stopped"
            )
            return None
        reserve_calls("retry")
        if review_only:
            self._review_event(context, call, review, attempt + 1, "subagent_refinement")
            return ReviewOnly(result, list(evidence))
        if self.spec.execution.planned:
            SearchAllowance(context, call.search_task_key).begin_refinement(allow_search=allow_search)
        self._review_event(context, call, review, attempt + 1, "subagent_refinement")
        if not allow_search:
            return ReportRevision(result["messages"][-1].text, review.revision_instruction)
        # Keep the actual tool turns as well as the draft: an exhausted quota must
        # still allow citation/wording fixes using sources already retrieved.
        messages = result["messages"]
        if messages[: len(input["messages"])] != input["messages"]:
            messages = [*input["messages"], *messages]
        correction = (
            "在原始分工内修订，不改变负责对象，不接管其他并行子任务。"
            "下面 JSON 中 delegated_task 固定本任务边界，correction 仅定位本次修订。\n"
            + json.dumps(
                dict(delegated_task=call.args["description"], correction=review.revision_instruction),
                ensure_ascii=False,
            )
        )
        return dict(input, messages=[*messages, HumanMessage(content=correction)])

    def _search_request(self, context, inputs):
        if not self.spec.execution.planned or not context.task_instruction:
            return inputs
        messages = inputs["messages"]
        content = (
            "以下 JSON 是本轮需求数据，user_query 是原始用户需求，task 是分派子任务。"
            "仅完成 task 指定的当前对象与分工，user_query 同时约束验收条件的上限；其他并行助手负责的对象不要重复调查。"
            "不要替主助手生成完整对比或推荐。不把附加调查项一律当作必需；缺失的可选项明确披露。\n"
            + json.dumps(dict(user_query=context.task_instruction, task=messages[-1].text), ensure_ascii=False)
        )
        return dict(inputs, messages=[*messages[:-1], HumanMessage(content=content)])

    def _dependency(self, error):
        if isinstance(error, ExecutionTerminated):
            raise error
        from agent.runtime.recovery import ToolCallTimeout

        if isinstance(error, ResearchOutputError):
            return ReviewResult(verdict="insufficient", issues=[str(error)], revision_instruction=str(error))
        if isinstance(error, ToolCallTimeout):
            return ReviewResult(verdict="tool_failed", issues=[str(error)])
        if isinstance(error, ModelRequestTimeout):
            context = get_run_context()
            with context.budget_lock:
                context.unavailable_agents[self.spec.agent_id] = str(error)
        category = classify_tool_error(error)
        if category == "execution" and error.__cause__ is not None:
            category = classify_tool_error(error.__cause__)
        reason = redact_text(str(error) or type(error).__name__)
        if category in ("configuration", "permission", "transient"):
            return ReviewResult(verdict="dependency_unavailable", issues=["依赖不可用：" + reason])
        return ReviewResult(verdict="tool_failed", issues=["子任务执行失败：" + reason])

    def _recovered_result(self, inputs):
        context = get_run_context()
        if context is not None and self.registry is not None:
            from agent.runtime.experts import bind_registry

            bind_registry(context, self.registry)
        if not context or not context.background_job or not context.resume_graph or not self.spec.execution.planned:
            return None
        from agent.runtime.task_plan import resolve_task

        task_id, _, _ = resolve_task(context, inputs["messages"][-1].text)
        task = context.search_tasks.get(task_id)
        if task and deliverable(task.state) and task.result:
            if task.report is None:
                raise HookRejected("缺少已核验的结构化事实，不能恢复该检索结果")
            return {"messages": [AIMessage(content=json.dumps({"verified_report": task.report}, ensure_ascii=False))]}
        return None

    def invoke(self, input, config=None, **kwargs):
        return self._invoke(input, config, **kwargs)

    def _invoke(self, input, config=None, **kwargs):
        recovered = self._recovered_result(input)
        if recovered is not None:
            return recovered
        context, call, record = self._prepare(input)
        token = active_call.set(call)
        skill_token = skill_scope.set((self.spec.agent_id, self.spec.allowed_skill_ids))
        started = monotonic()
        leases = []
        slot_token = None
        parent_slot, branch_slot = None, None
        try:
            self._save(context, call, record, "queued")
            if context.business_mode == "internal_external":
                if context.event_sink is None:
                    raise HookRejected("Workflow execution requires durable results")
                sync_result(prepare_delegate(context, call), context.event_sink)
            from agent.runtime.scheduler import child_owner

            inherited_slot = child_owner.get()
            if inherited_slot is not None and inherited_slot["run_id"] == context.run_id:
                parent_slot = inherited_slot
                parent_slot["lease"].release()
            else:
                for gate in (context.scheduler.child_gate(context), context.scheduler.gate("children")):
                    leases.append(context.scheduler.acquire(gate, context, "children"))
            branch_slot = dict(
                run_id=context.run_id,
                resources=self.spec.resources,
                lease=context.scheduler.acquire(context.scheduler.expert_gate(self.spec.resources), context, "expert"),
            )
            slot_token = child_owner.set(branch_slot)
            self._start(context, call, record)
            input = self._search_request(context, input)
            if self.spec.execution.receive_internal_results and context.event_sink is not None:
                input = sync_result(self.results.search_input(context, call, input), context.event_sink)
            current = input
            search_evidence = (
                list(SearchAllowance(context, call.search_task_key).task.evidence)
                if self.spec.execution.planned
                else []
            )
            for attempt in range(self.spec.review_config.max_refinements + 1):
                context.check_active()
                evidence = (
                    current.evidence
                    if isinstance(current, ReviewOnly)
                    else search_evidence
                    if self.spec.execution.planned
                    else []
                )
                evidence_token = tool_evidence.set(evidence)
                search_token = search_allowance.set(
                    SearchAllowance(context, call.search_task_key) if self.spec.execution.planned else None
                )
                dependency = None
                try:
                    try:
                        result = (
                            current.result
                            if isinstance(current, ReviewOnly)
                            else self.executor.invoke(current, config, **kwargs)
                        )
                    except ExecutionTerminated:
                        raise
                    except Exception as exc:
                        dependency = self._dependency(exc)
                        result = (
                            exc.result
                            if isinstance(exc, ResearchOutputError)
                            else {"messages": [AIMessage(content="")]}
                        )
                finally:
                    search_allowance.reset(search_token)
                    tool_evidence.reset(evidence_token)
                context.check_active()
                result = self._returned(call, record, result, evidence)
                review = dependency or self.reviewer.review_sync(
                    input["messages"][-1].text, record["result"], evidence, config
                )
                if context.event_sink is not None:
                    sync_result(
                        self.results.save(context, call, record["result"], evidence, review, attempt),
                        context.event_sink,
                    )
                checked = self._checked(context, call, record, result, review, attempt)
                current = self._refine(context, call, input, review, attempt, result, evidence, record)
                if current is None:
                    return checked
                call.status, record["error"] = "running", None
        except BaseException as exc:
            self._error(call, record, exc)
            raise
        finally:
            try:
                self._finish(context, call, record, started)
            finally:
                if branch_slot is not None:
                    branch_slot["lease"].release()
                if slot_token is not None:
                    child_owner.reset(slot_token)
                for lease in reversed(leases):
                    lease.release()
                if context.stop_status:
                    context.scheduler.forget_run(context.run_id)
                skill_scope.reset(skill_token)
                active_call.reset(token)
                if parent_slot is not None and not context.stop_status:
                    parent_slot["lease"] = context.scheduler.acquire(
                        context.scheduler.expert_gate(parent_slot["resources"]), context, "expert"
                    )

    async def ainvoke(self, input, config=None, **kwargs):
        recovered = self._recovered_result(input)
        if recovered is not None:
            return recovered
        context, call, record = self._prepare(input)
        token = active_call.set(call)
        skill_token = skill_scope.set((self.spec.agent_id, self.spec.allowed_skill_ids))
        started = monotonic()
        leases = []
        slot_token = None
        parent_slot, branch_slot = None, None
        try:
            self._save(context, call, record, "queued")
            await prepare_delegate(context, call)
            from agent.runtime.scheduler import child_owner

            inherited_slot = child_owner.get()
            if inherited_slot is not None and inherited_slot["run_id"] == context.run_id:
                parent_slot = inherited_slot
                parent_slot["lease"].release()
            else:
                for gate in (context.scheduler.child_gate(context), context.scheduler.gate("children")):
                    leases.append(await context.scheduler.aacquire(gate, context, "children"))
            branch_slot = dict(
                run_id=context.run_id,
                resources=self.spec.resources,
                lease=await context.scheduler.aacquire(
                    context.scheduler.expert_gate(self.spec.resources), context, "expert"
                ),
            )
            slot_token = child_owner.set(branch_slot)
            self._start(context, call, record)
            input = self._search_request(context, input)
            if self.spec.execution.receive_internal_results:
                input = await self.results.search_input(context, call, input)
            current = input
            search_evidence = (
                list(SearchAllowance(context, call.search_task_key).task.evidence)
                if self.spec.execution.planned
                else []
            )
            for attempt in range(self.spec.review_config.max_refinements + 1):
                evidence = (
                    current.evidence
                    if isinstance(current, ReviewOnly)
                    else search_evidence
                    if self.spec.execution.planned
                    else []
                )
                evidence_token = tool_evidence.set(evidence)
                search_token = search_allowance.set(
                    SearchAllowance(context, call.search_task_key) if self.spec.execution.planned else None
                )
                dependency = None
                try:
                    try:
                        result = (
                            current.result
                            if isinstance(current, ReviewOnly)
                            else await self.executor.ainvoke(current, config, **kwargs)
                        )
                    except ExecutionTerminated:
                        raise
                    except Exception as exc:
                        dependency = self._dependency(exc)
                        result = (
                            exc.result
                            if isinstance(exc, ResearchOutputError)
                            else {"messages": [AIMessage(content="")]}
                        )
                finally:
                    search_allowance.reset(search_token)
                    tool_evidence.reset(evidence_token)
                result = self._returned(call, record, result, evidence)
                review = dependency or await self.reviewer.review(
                    input["messages"][-1].text, record["result"], evidence, config
                )
                await self.results.save(context, call, record["result"], evidence, review, attempt)
                checked = self._checked(context, call, record, result, review, attempt)
                current = self._refine(context, call, input, review, attempt, result, evidence, record)
                if current is None:
                    return checked
                call.status, record["error"] = "running", None
        except BaseException as exc:
            self._error(call, record, exc)
            raise
        finally:
            try:
                self._finish(context, call, record, started)
            finally:
                if branch_slot is not None:
                    branch_slot["lease"].release()
                if slot_token is not None:
                    child_owner.reset(slot_token)
                for lease in reversed(leases):
                    lease.release()
                if context.stop_status:
                    context.scheduler.forget_run(context.run_id)
                skill_scope.reset(skill_token)
                active_call.reset(token)
                if parent_slot is not None and not context.stop_status:
                    parent_slot["lease"] = await context.scheduler.aacquire(
                        context.scheduler.expert_gate(parent_slot["resources"]), context, "expert"
                    )
