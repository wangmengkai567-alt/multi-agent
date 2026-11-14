# 模型包装层：主助手、子助手和摘要调用统一受并发、上下文大小、预算和超时约束。
"""Count every model invocation, including framework summarization."""

import asyncio
from typing import Literal

import httpx
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable

from agent.runtime.context import get_run_context
from agent.runtime.errors import ExecutionTerminated
from agent.runtime.governance import register_turn
from agent.runtime.model_context import admit_context, prepared_payload
from agent.runtime.output import normalize_model_message
from agent.runtime.policies import calls_available, request_timeout, reserve_calls


class BudgetSummarizationMiddleware(SummarizationMiddleware):
    """Admit optional summaries only when the actual answer still fits."""

    def _can_summarize(self):
        context = get_run_context()
        from agent.runtime.stage_budget import coordination_window, current_stage, stage_remaining

        remaining = stage_remaining(context, tools=True)
        minimum = 2 * context.policy.stages.expert_request_min_seconds
        if context.report_started or remaining is not None and remaining <= minimum:
            return False
        if current_stage(context) is None and coordination_window(context) <= 0:
            return False
        category = self.model.category
        return calls_available(category, category, *(("main_model",) if category == "sub_model" else ()))

    def _should_summarize(self, messages, total_tokens):
        return self._can_summarize() and super()._should_summarize(messages, total_tokens)

    def _create_summary(self, messages_to_summarize):
        from agent.runtime.required_work import summary_work_messages

        return super()._create_summary(summary_work_messages(messages_to_summarize))

    async def _acreate_summary(self, messages_to_summarize):
        from agent.runtime.required_work import summary_work_messages

        return await super()._acreate_summary(summary_work_messages(messages_to_summarize))

    def wrap_model_call(self, request, handler):
        if not self._can_summarize():
            return handler(request)
        return super().wrap_model_call(request, handler)

    async def awrap_model_call(self, request, handler):
        if not self._can_summarize():
            return await handler(request)
        return await super().awrap_model_call(request, handler)


class ModelRequestTimeout(TimeoutError):
    def __init__(self, category, seconds):
        self.category = category
        self.seconds = seconds
        label = {"main_model": "任务协调模型", "sub_model": "子助手模型"}[category]
        super().__init__(f"{label}请求超时（上限 {seconds:g} 秒）")


def is_model_timeout(error):
    return isinstance(error, TimeoutError | httpx.TimeoutException) or type(error).__name__ == "APITimeoutError"


class BudgetChatModel(BaseChatModel):
    delegate: Runnable
    resource_group: str = "standalone"
    category: Literal["main_model", "sub_model"] = "main_model"

    @property
    def _llm_type(self):
        return "budgeted-chat-model"

    def bind_tools(self, tools, **kwargs):
        return BudgetChatModel(
            delegate=self.delegate.bind_tools(tools, **kwargs),
            category=self.category,
            resource_group=self.resource_group,
            profile=self.profile,
            cache=False,
        )

    def _measurement(self, messages, run_manager):
        from agent.runtime.measurement import RequestMeasurement

        metadata = getattr(run_manager, "metadata", None) or {}
        phase = (
            "summarization"
            if metadata.get("lc_source") == "summarization"
            else ("research" if self.category == "sub_model" else "coordination")
        )
        return RequestMeasurement(self.category, phase, messages)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        context = get_run_context()
        measured = self._measurement(messages, run_manager)
        message, error = None, None
        try:
            measured.set_deadline(request_timeout(model_request=True))
            with context.scheduler.provider(context, "models", self.resource_group):
                measured.admit()
                admit_context(measured, prepared_payload(messages, self.delegate, kwargs))
                reserve_calls(self.category)
                measured.start()
                config = {"callbacks": run_manager.handlers} if run_manager else None
                kwargs["stream"] = False
                kwargs["timeout"] = request_timeout(model_request=True)
                try:
                    message = self.delegate.invoke(messages, config=config, stop=stop, **kwargs)
                except BaseException as exc:
                    if is_model_timeout(exc):
                        measured.check_timeout()
                        raise ModelRequestTimeout(self.category, kwargs["timeout"]) from exc
                    raise
                context.check_active()
                request_timeout(model_request=True)
                return ChatResult(generations=[ChatGeneration(message=register_turn(normalize_model_message(message)))])
        except BaseException as exc:
            if isinstance(exc, ExecutionTerminated):
                context.terminate(exc)
            error = exc
            raise
        finally:
            measured.finish(message, error)

    # 先取得账号执行槽，再校验上下文和扣减预算；模型返回后复查本轮是否已经停止。
    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        context = get_run_context()
        measured = self._measurement(messages, run_manager)
        message, error = None, None
        try:
            measured.set_deadline(request_timeout(model_request=True))
            async with context.scheduler.aprovider(context, "models", self.resource_group):
                measured.admit()
                admit_context(measured, prepared_payload(messages, self.delegate, kwargs))
                reserve_calls(self.category)
                measured.start()
                config = {"callbacks": run_manager.handlers} if run_manager else None
                kwargs["stream"] = False
                kwargs["timeout"] = request_timeout(model_request=True)
                try:
                    async with asyncio.timeout_at(measured.deadline_clock):
                        message = await self.delegate.ainvoke(messages, config=config, stop=stop, **kwargs)
                except BaseException as exc:
                    if is_model_timeout(exc):
                        measured.check_timeout()
                        raise ModelRequestTimeout(self.category, kwargs["timeout"]) from exc
                    raise
                context.check_active()
                request_timeout(model_request=True)
                return ChatResult(generations=[ChatGeneration(message=register_turn(normalize_model_message(message)))])
        except BaseException as exc:
            if isinstance(exc, ExecutionTerminated):
                context.terminate(exc)
            error = exc
            raise
        finally:
            measured.finish(message, error)


# 保留模型工具能力和资源分组，同时禁止缓存跳过实际调用的计量过程。
def budget_model(model, category):
    if isinstance(model, BudgetChatModel) and model.category == category:
        return model
    return BudgetChatModel(
        delegate=model,
        category=category,
        resource_group=(getattr(model, "metadata", None) or {}).get("resource_group", "standalone"),
        profile=getattr(model, "profile", None),
        cache=False,
    )
