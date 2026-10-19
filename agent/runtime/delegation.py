"""The one lifecycle owner at the actual expert execution boundary."""

import json
import logging
from datetime import UTC, datetime
from hashlib import sha256
from time import monotonic
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import Runnable

from agent.reflection import ResultReviewer, ReviewResult, normalize_source_citations, source_urls, tool_evidence
from agent.results import ResultService, sync_result
from agent.runtime.context import get_run_context
from agent.runtime.events import make_event, redact_text
from agent.runtime.hooks import HookCall, HookRejected, active_call, error_message, pipeline
from agent.runtime.models import ModelRequestTimeout
from agent.runtime.policies import BudgetExceeded, SearchLimitReached, classify_tool_error, reserve_calls
from agent.runtime.search import SearchAllowance, needs_search_refinement, search_allowance
from agent.runtime.skills import skill_scope
from agent.runtime.workflows import prepare_delegate


class DelegationRunnable(Runnable):
    def __init__(self, spec, executor, *, hooks=None, reviewer=None, results=None):
        from agent.subagent_registry import DISPLAY_NAMES

        if spec.agent_id not in DISPLAY_NAMES:
            raise ValueError("Invalid expert executor target")
        self.spec, self.executor, self.hooks = spec, executor, hooks or pipeline
        self.reviewer = reviewer or ResultReviewer(spec.review_config)
        self.results = results or ResultService()

    def _prepare(self, inputs):
        context = get_run_context()
        if context is None:
            raise HookRejected("Expert execution requires a run context")
        with context.budget_lock:
            unavailable = context.unavailable_agents.get(self.spec.agent_id)
        if unavailable:
            raise HookRejected(unavailable + "；本轮不重复调用该助手，请说明依据缺失。")
        messages = inputs.get("messages", [])
        if not messages or not isinstance(messages[-1], HumanMessage) or not messages[-1].text.strip():
            raise HookRejected("Expert execution requires a non-empty task description")
        description = messages[-1].text
        parent = active_call.get()
        request_id = parent.call_id if parent and parent.target == self.spec.agent_id else None
        call = HookCall(
            self.spec.agent_id,
            str(uuid4()),
            request_id or context.run_id,
            args={"description": description},
            target=self.spec.agent_id,
        )
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
        if self.spec.agent_id == "search":
            try:
                call.search_task_key = sha256(
                    (" ".join(description.split()) + f"\ninternal_results:{context.internal_result_version}").encode(
                        "utf-8"
                    )
                ).hexdigest()
                allowance = SearchAllowance(context, call.search_task_key)
                allowance.claim()
                allowance.task.description = description
            except SearchLimitReached as exc:
                raise HookRejected(str(exc)) from exc
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
            context.event_sink.submit_subtask(dict(record), event)
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
        if self.spec.agent_id == "search":
            text = normalize_source_citations(final.text, evidence)
            if text != final.text:
                final = final.model_copy(update={"content": text})
                result = dict(result, messages=[*messages[:-1], final])
        call.review_evidence = list(evidence)
        call.result, call.status = result, "completed"
        record["result"] = redact_text(final.text)
        if self.spec.agent_id == "search":
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
        if self.spec.agent_id == "db" and call.status == "completed" and record["check_status"] == "passed":
            with context.budget_lock:
                context.internal_result_version += 1
        if self.spec.agent_id == "search":
            allowance = SearchAllowance(context, call.search_task_key)
            allowance.task.failure_reason = record["error"] or ""
            allowance.finish(
                executed=call.executed,
                reason=record["error"],
                check_status=record["check_status"],
                result=record["result"] or "",
                sources=allowance.task.sources,
            )
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
        call.check_status = record["check_status"] = review.verdict
        self._review_event(context, call, review, attempt)
        if review.verdict == "passed":
            if self.spec.agent_id == "db":
                with context.budget_lock:
                    context.approved_internal[call.call_id] = dict(text=record["result"], evidence=call.review_evidence)
            return result
        if self.spec.agent_id == "search":
            reason = None
            if review.verdict == "check_incomplete":
                reason = "结果检查未完成，已有检索记录已保存，重复搜索不能修复检查格式"
            elif review.verdict == "insufficient" and attempt >= self.spec.review_config.max_refinements:
                reason = "本轮搜索子任务已完成允许的修订，仍缺少合格依据，不再重新执行"
            elif review.verdict != "insufficient":
                reason = "本轮联网子任务执行失败，不再重复委派：" + "; ".join(review.issues)
            if reason:
                with context.budget_lock:
                    SearchAllowance(context, call.search_task_key).task.closed_reason = reason
        if review.verdict == "check_incomplete" and self.reviewer.allow_unchecked(review):
            review_text = json.dumps(
                dict(review=review.model_dump(), unchecked_result=record["result"]), ensure_ascii=False
            )
        else:
            parent_review = review.model_dump()
            if review.verdict == "insufficient":
                # Grounded findings contain quotes from REJECTED drafts. Keep
                # them in local refinement/durable checks, never in parent facts.
                parent_review.update(
                    issues=["当前分工的必需内容或事实依据未通过检查，具体定位见执行详情；检查说明不能作为事实依据。"],
                    missing_evidence=["当前分工的合格依据"],
                    revision_instruction="本子任务未返回可采纳结果，保留原始问题范围并明确局限，不根据检查说明重建事实。",
                )
            review_text = json.dumps(
                dict(review=parent_review, **({"retry_allowed": False} if self.spec.agent_id == "search" else {})),
                ensure_ascii=False,
            )
            call.status = "failed"
            record["error"] = redact_text("; ".join(review.issues))
        # Never present rejected expert prose as an accepted answer to the parent.
        result = dict(result, messages=[AIMessage(content=review_text)])
        call.result = result
        record["result"] = redact_text(review_text)
        return result

    def _refine(self, context, call, input, review, attempt, result, evidence, record):
        if review.verdict != "insufficient" or attempt >= self.spec.review_config.max_refinements:
            return None
        if context.deadline is not None and context.deadline <= datetime.now(UTC):
            return None
        reserve_calls("retry")
        if self.spec.agent_id == "search":
            SearchAllowance(context, call.search_task_key).begin_refinement(
                allow_search=needs_search_refinement(review, evidence)
            )
        self._review_event(context, call, review, attempt + 1, "subagent_refinement")
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
        if self.spec.agent_id != "search" or not context.user_query:
            return inputs
        messages = inputs["messages"]
        content = (
            "以下 JSON 是本轮需求数据，user_query 是原始用户需求，task 是分派子任务。"
            "仅完成 task 指定的当前对象与分工，user_query 同时约束验收条件的上限；其他并行助手负责的对象不要重复调查。"
            "不要替主助手生成完整对比或推荐。不把附加调查项一律当作必需；缺失的可选项明确披露。\n"
            + json.dumps(dict(user_query=context.user_query, task=messages[-1].text), ensure_ascii=False)
        )
        return dict(inputs, messages=[*messages[:-1], HumanMessage(content=content)])

    def _dependency(self, error):
        if isinstance(error, ModelRequestTimeout):
            context = get_run_context()
            with context.budget_lock:
                context.unavailable_agents[self.spec.agent_id] = str(error)
        category = classify_tool_error(error)
        if category == "execution" and error.__cause__ is not None:
            category = classify_tool_error(error.__cause__)
        reason = redact_text(str(error) or type(error).__name__)
        if isinstance(error, BudgetExceeded) or category == "budget":
            return ReviewResult(verdict="budget_exhausted", issues=["调用预算耗尽"])
        if category in ("configuration", "permission", "transient"):
            return ReviewResult(verdict="dependency_unavailable", issues=["依赖不可用：" + reason])
        return ReviewResult(verdict="tool_failed", issues=["子任务执行失败：" + reason])

    def invoke(self, input, config=None, **kwargs):
        return self._invoke(input, config, **kwargs)

    def _invoke(self, input, config=None, **kwargs):
        context, call, record = self._prepare(input)
        token = active_call.set(call)
        skill_token = skill_scope.set((self.spec.agent_id, self.spec.allowed_skill_ids))
        started = monotonic()
        try:
            self._save(context, call, record, "queued")
            if context.business_mode == "internal_external":
                if context.event_sink is None:
                    raise HookRejected("Workflow execution requires durable results")
                sync_result(prepare_delegate(context, call), context.event_sink)
            self._start(context, call, record)
            input = self._search_request(context, input)
            if self.spec.agent_id == "search" and context.event_sink is not None:
                input = sync_result(self.results.search_input(context, call, input), context.event_sink)
            current = input
            search_evidence = []  # Isolated per delegation; retained only across its own refinements.
            for attempt in range(self.spec.review_config.max_refinements + 1):
                context.check_active()
                evidence = search_evidence if self.spec.agent_id == "search" else []
                evidence_token = tool_evidence.set(evidence)
                search_token = search_allowance.set(
                    SearchAllowance(context, call.search_task_key) if self.spec.agent_id == "search" else None
                )
                dependency = None
                try:
                    try:
                        result = self.executor.invoke(current, config, **kwargs)
                    except Exception as exc:
                        dependency = self._dependency(exc) if self.spec.review_config.enabled else None
                        if dependency is None:
                            raise
                        result = {"messages": [AIMessage(content="")]}
                finally:
                    search_allowance.reset(search_token)
                    tool_evidence.reset(evidence_token)
                context.check_active()
                result = self._returned(call, record, result, evidence)
                if not self.spec.review_config.enabled:
                    if context.event_sink is not None:
                        sync_result(
                            self.results.save(context, call, record["result"], evidence, None, attempt),
                            context.event_sink,
                        )
                    return result
                review = dependency or self.reviewer.review_sync(
                    input["messages"][-1].text, record["result"], evidence, config
                )
                if context.event_sink is not None:
                    sync_result(
                        self.results.save(context, call, record["result"], evidence, review, attempt),
                        context.event_sink,
                    )
                checked = self._checked(context, call, record, result, review, attempt)
                try:
                    current = self._refine(context, call, input, review, attempt, result, evidence, record)
                except BudgetExceeded:
                    return self._checked(
                        context,
                        call,
                        record,
                        result,
                        ReviewResult(verdict="budget_exhausted", issues=["补充查询预算耗尽"]),
                        attempt,
                    )
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
                skill_scope.reset(skill_token)
                active_call.reset(token)

    async def ainvoke(self, input, config=None, **kwargs):
        context, call, record = self._prepare(input)
        token = active_call.set(call)
        skill_token = skill_scope.set((self.spec.agent_id, self.spec.allowed_skill_ids))
        started = monotonic()
        try:
            self._save(context, call, record, "queued")
            await prepare_delegate(context, call)
            self._start(context, call, record)
            input = self._search_request(context, input)
            if self.spec.agent_id == "search":
                input = await self.results.search_input(context, call, input)
            current = input
            search_evidence = []  # Isolated per delegation; retained only across its own refinements.
            for attempt in range(self.spec.review_config.max_refinements + 1):
                evidence = search_evidence if self.spec.agent_id == "search" else []
                evidence_token = tool_evidence.set(evidence)
                search_token = search_allowance.set(
                    SearchAllowance(context, call.search_task_key) if self.spec.agent_id == "search" else None
                )
                dependency = None
                try:
                    try:
                        result = await self.executor.ainvoke(current, config, **kwargs)
                    except Exception as exc:
                        dependency = self._dependency(exc) if self.spec.review_config.enabled else None
                        if dependency is None:
                            raise
                        result = {"messages": [AIMessage(content="")]}
                finally:
                    search_allowance.reset(search_token)
                    tool_evidence.reset(evidence_token)
                result = self._returned(call, record, result, evidence)
                if not self.spec.review_config.enabled:
                    await self.results.save(context, call, record["result"], evidence, None, attempt)
                    return result
                review = dependency or await self.reviewer.review(
                    input["messages"][-1].text, record["result"], evidence, config
                )
                await self.results.save(context, call, record["result"], evidence, review, attempt)
                checked = self._checked(context, call, record, result, review, attempt)
                try:
                    current = self._refine(context, call, input, review, attempt, result, evidence, record)
                except BudgetExceeded:
                    return self._checked(
                        context,
                        call,
                        record,
                        result,
                        ReviewResult(verdict="budget_exhausted", issues=["补充查询预算耗尽"]),
                        attempt,
                    )
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
                skill_scope.reset(skill_token)
                active_call.reset(token)
