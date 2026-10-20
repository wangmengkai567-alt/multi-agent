"""Audit final public-source answers once, with bounded correction and trusted fallback."""

import json

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware import AgentMiddleware, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

from agent.reflection import ResultReviewer, ReviewResult, normalize_source_citations, source_urls
from agent.runtime.context import get_run_context
from agent.runtime.events import make_event
from agent.runtime.finalization import partial_response
from agent.runtime.models import ModelRequestTimeout
from agent.runtime.output import cited_urls, source_page
from agent.runtime.policies import BudgetExceeded, reserve_calls
from agent.subagent_registry import ReviewConfig


class AnswerReviewMiddleware(AgentMiddleware):
    def __init__(self, model):
        self.model = model

    def _normalize_sources(self, response):
        context = get_run_context()
        if context is None or not isinstance(response, ModelResponse) or not response.result:
            return response
        message = response.result[-1]
        if not isinstance(message, AIMessage) or message.tool_calls:
            return response
        evidence = [
            item for task in context.search_tasks.values() if task.check_status == "passed" for item in task.evidence
        ]
        text = normalize_source_citations(message.text, evidence)
        if text != message.text:
            from dataclasses import replace

            message = message.model_copy(update={"content": text})
            response = replace(response, result=[*response.result[:-1], message])
        return response

    def _candidate(self, response):
        if not isinstance(response, ModelResponse) or not response.result:
            return None
        message = response.result[-1]
        if (
            not isinstance(message, AIMessage)
            or message.tool_calls
            or message.additional_kwargs.get("deterministic_fallback")
        ):
            return None
        context = get_run_context()
        if context is None:
            return None
        if (
            context.enforce_plan
            or context.search_tasks
            or context.approved_internal
            or context.answer_repair_attempted
            or cited_urls(message.text) - cited_urls(context.user_query)
        ):
            return message
        return None

    def _reviewer(self, message):
        context = get_run_context()
        evidence = [
            item for task in context.search_tasks.values() if task.check_status == "passed" for item in task.evidence
        ]
        evidence.extend(item for result in context.approved_internal.values() for item in result["evidence"])
        settings = ReviewConfig(
            enabled=True,
            model_check=True,
            require_sources=bool(cited_urls(message.text)),
        )
        return ResultReviewer(settings, self.model, final_answer=True), evidence

    def _source_gap(self, message, evidence):
        context = get_run_context()
        import re

        if re.search(r"\[source:[^\]]+\]", message.text):
            return ReviewResult(
                verdict="insufficient",
                issues=["引用了未登记的来源 ID"],
                missing_evidence=["实际来源ID"],
                revision_instruction="只使用本任务实际 allowed_sources 中的 ID，删除未知引用及其无依据断言。",
            )
        if context.accepted_plan and not evidence and not context.approved_internal:
            return ReviewResult(
                verdict="insufficient",
                issues=["已登记研究分工但尚无通过检查的实际结果"],
                missing_evidence=["当前研究任务的真实证据"],
                revision_instruction="继续执行已登记任务，不以调查计划或即将调用工具的承诺结束；不可核实则明确有限结果。",
            )
        known = {source_page(url) for url in source_urls(evidence)}
        provided = {source_page(url) for url in cited_urls(context.user_query)}
        missing = {source_page(url) for url in cited_urls(message.text)} - known - provided
        if not missing:
            return None
        return ReviewResult(
            verdict="insufficient",
            issues=["部分引用没有本轮实际检索依据"],
            missing_evidence=["可追溯的实际来源"],
            revision_instruction=(
                "下列来源没有实际依据，删除对应链接及无依据引文；不要围绕这些链接增加调查，"
                "不要用记忆重建预期 URL。已有合格资料时只用 allowed_source_urls 修订原始问题的答复；"
                "合法来源可保留同页锚点。若尚无实际结果，按原始问题核实必要来源。\n"
                + json.dumps(
                    dict(
                        excluded_source_urls=sorted(missing),
                        allowed_source_urls=sorted(set(source_urls(evidence)) | cited_urls(context.user_query)),
                        allowed_sources=[
                            {"source_id": __import__("hashlib").sha256(url.encode()).hexdigest()[:12], "url": url}
                            for url in source_urls(evidence)
                        ],
                        repair_phase="citation",
                    ),
                    ensure_ascii=False,
                )
            ),
        )

    def _record(self, review):
        context = get_run_context()
        context.delivery_status = (
            "complete"
            if review.verdict == "passed" and getattr(review, "answer_kind", "complete") == "complete"
            else "partial"
        )
        if context.event_sink is not None:
            context.event_sink.submit(
                make_event(
                    context.thread_id,
                    context.run_id,
                    "answer_validation",
                    "最终答复检查：" + review.verdict,
                    review.model_dump(),
                    actor="main",
                    status=(
                        "partial"
                        if review.verdict == "passed" and getattr(review, "answer_kind", "complete") == "partial"
                        else review.verdict
                    ),
                    call_id=context.run_id,
                )
            )

    def _revision(self, request, message, review):
        context = get_run_context()
        if review.verdict != "insufficient":
            return None
        citation_only = bool(review.issues) and all(
            issue.startswith(("部分引用没有本轮实际检索依据", "引用了未登记的来源 ID")) for issue in review.issues
        )
        flag = "answer_citation_repair_attempted" if citation_only else "answer_repair_attempted"
        if getattr(context, flag):
            return None
        try:
            reserve_calls("retry")
        except BudgetExceeded:
            return None
        setattr(context, flag, True)
        if context.search_tasks or context.approved_internal:
            # Do not restart searches to repair the final prose; approved bodies are the source of truth.
            _, evidence = self._reviewer(message)
            data = dict(
                user_query=context.user_query,
                allowed_source_urls=sorted(set(source_urls(evidence)) | cited_urls(context.user_query)),
                allowed_sources=[
                    {"source_id": __import__("hashlib").sha256(url.encode()).hexdigest()[:12], "url": url}
                    for url in source_urls(evidence)
                ],
                repair_phase="citation" if citation_only else "facts",
                verified_results=[
                    *[task.result for task in context.search_tasks.values() if task.check_status == "passed"],
                    *[result["text"] for result in context.approved_internal.values()],
                ],
                failed_checks=[
                    dict(check_status=task.check_status)
                    for task in context.search_tasks.values()
                    if task.check_status != "passed"
                ],
                draft=message.text,
                revision=review.revision_instruction,
            )
            note = "优先使用 allowed_sources 中的 [source:来源ID] 引用，由程序填入实际 URL。修订定位到具体事实冲突时按真实依据修正或删掉该断言，不为保留可选数字再调查。最终答复未通过检查。只使用已通过检查的资料修订文字和来源，不调用工具；失败任务的内容不能充当事实。allowed_source_urls 是程序从已通过任务的真实工具记录及用户输入中提取的可引用来源，资料正文中的预期链接或记忆链接也不能越过该范围。资料不足时明确有限结论。不回显内部 JSON 或思考过程。"
            return request.override(
                tools=[],
                tool_choice="none",
                model_settings={**request.model_settings, "tool_choice": "none"},
                system_message=append_to_system_message(request.system_message, note),
                messages=[HumanMessage(content=json.dumps(data, ensure_ascii=False))],
            )
        note = "上一次答复引用外部来源但缺少本轮真实工具依据。请先通过 search 子任务核验并修订；若用户限制联网，撤回未经核实的引文或事实断言，明确局限。必须遵守用户的范围和限制，不能编造原文或假装已经访问链接。"
        original = [message for message in request.messages if isinstance(message, HumanMessage)]
        note += "只围绕原始用户问题取必要的少量来源。丢弃未经核实的草稿，不逐项验证草稿的所有链接、锚点或 HTTP 状态码，除非用户明确要求这些检查。"
        return request.override(
            tool_choice="auto",
            system_message=append_to_system_message(request.system_message, note),
            messages=[
                *(original[-1:] or [HumanMessage(content=context.user_query)]),
                HumanMessage(content="围绕原始问题重新核实资料并回答：" + context.user_query),
            ],
        )

    def _fallback(self, review, *, record=True):
        if record:
            self._record(review)
        return partial_response(get_run_context(), "的最终综合答复未能通过证据检查")

    def wrap_model_call(self, request, handler):
        current = request
        while True:
            try:
                response = handler(current)
            except ModelRequestTimeout as error:
                if get_run_context() is None or not (
                    get_run_context().search_tasks or get_run_context().approved_internal
                ):
                    raise
                return self._fallback(ReviewResult(verdict="check_incomplete", issues=[str(error)]))
            response = self._normalize_sources(response)
            message = self._candidate(response)
            if message is None:
                return response
            reviewer, evidence = self._reviewer(message)
            review = self._source_gap(message, evidence) or reviewer.review_sync(
                get_run_context().user_query, message.text, evidence
            )
            self._record(review)
            if review.verdict == "passed":
                return response
            current = self._revision(current, message, review)
            if current is None:
                return self._fallback(review, record=False)

    async def awrap_model_call(self, request, handler):
        current = request
        while True:
            try:
                response = await handler(current)
            except ModelRequestTimeout as error:
                if get_run_context() is None or not (
                    get_run_context().search_tasks or get_run_context().approved_internal
                ):
                    raise
                return self._fallback(ReviewResult(verdict="check_incomplete", issues=[str(error)]))
            response = self._normalize_sources(response)
            message = self._candidate(response)
            if message is None:
                return response
            reviewer, evidence = self._reviewer(message)
            review = self._source_gap(message, evidence) or await reviewer.review(
                get_run_context().user_query, message.text, evidence
            )
            self._record(review)
            if review.verdict == "passed":
                return response
            current = self._revision(current, message, review)
            if current is None:
                return self._fallback(review, record=False)
