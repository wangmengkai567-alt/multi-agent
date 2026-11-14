# 模型包装层：主助手、子助手和摘要调用统一受并发、上下文大小、预算和超时约束。
"""Count every model invocation, including framework summarization."""

import asyncio
from typing import Literal

import httpx
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import get_buffer_string, trim_messages
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.types import Command

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
        from time import monotonic

        from agent.runtime.stage_budget import current_stage, model_turn_deadline, operation_cost

        if context.report_started:
            return False
        deadline = model_turn_deadline(context)
        minimum = context.policy.stages.expert_request_min_seconds
        if deadline is None or deadline - monotonic() <= 2 * minimum:
            return False
        category = self.model.category
        if current_stage(context) is None:
            return calls_available(category, category, "main_model")
        return calls_available(
            category,
            category,
            "tool",
            *(("sub_model",) * operation_cost(context).internal_model_calls),
            category,
            "main_model",
        )

    def _should_summarize(self, messages, total_tokens):
        return self._can_summarize() and super()._should_summarize(messages, total_tokens)

    def _summary_input(self, messages):
        from agent.runtime.required_work import summary_work_messages

        messages = summary_work_messages(messages)
        if not messages:
            return None, "No previous conversation history."
        helper = self._lc_helper
        if helper.trim_tokens_to_summarize is not None:
            messages = trim_messages(
                messages,
                max_tokens=helper.trim_tokens_to_summarize,
                token_counter=helper.token_counter,
                start_on="human",
                strategy="last",
                allow_partial=True,
                include_system=True,
            )
        if not messages:
            return None, "Previous conversation was too long to summarize."
        return helper.summary_prompt.format(messages=get_buffer_string(messages)).rstrip(), None

    def _create_summary(self, messages_to_summarize):
        prompt, empty_summary = self._summary_input(messages_to_summarize)
        if prompt is None:
            return empty_summary
        from agent.runtime.stage_budget import check_model_turn

        context = get_run_context()
        check_model_turn(context, summary=True, minimum=context.policy.stages.expert_request_min_seconds)
        response = self.model.invoke(prompt, config={"metadata": {"lc_source": "summarization"}})
        check_model_turn(context, summary=True)
        return response.text.strip()

    async def _acreate_summary(self, messages_to_summarize):
        prompt, empty_summary = self._summary_input(messages_to_summarize)
        if prompt is None:
            return empty_summary
        from agent.runtime.stage_budget import check_model_turn

        context = get_run_context()
        check_model_turn(context, summary=True, minimum=context.policy.stages.expert_request_min_seconds)
        response = await self.model.ainvoke(prompt, config={"metadata": {"lc_source": "summarization"}})
        check_model_turn(context, summary=True)
        return response.text.strip()

    def _summary_plan(self, request):
        prepared = request.override(messages=self._get_effective_messages(request))
        if not self._can_summarize():
            return prepared, None
        messages, _ = self._truncate_args(prepared.messages, prepared.system_message, prepared.tools)
        prepared = prepared.override(messages=messages)
        counted = [prepared.system_message, *messages] if prepared.system_message is not None else messages
        total = self.token_counter(counted, tools=prepared.tools)
        if not self._should_summarize(messages, total):
            return prepared, None
        cutoff = self._determine_cutoff_index(messages)
        if cutoff <= 0:
            return prepared, None
        removed, preserved = self._partition_messages(messages, cutoff)
        return prepared, (removed, preserved, cutoff)

    @staticmethod
    def _require_history(path):
        if path is None:
            raise RuntimeError("摘要历史保存失败，不能继续生成摘要")
        return path

    def _summary_request(self, request, plan, summary, path):
        _, preserved, cutoff = plan
        messages = self._build_new_messages_with_path(summary, path)
        previous = request.state.get("_summarization_event")
        event = dict(
            cutoff_index=previous["cutoff_index"] + cutoff - 1 if previous is not None else cutoff,
            summary_message=messages[0],
            file_path=path,
        )
        return request.override(messages=[*messages, *preserved]), event

    def wrap_model_call(self, request, handler):
        from agent.runtime.stage_budget import check_model_turn, model_turn

        context = get_run_context()
        names = [convert_to_openai_tool(tool)["function"]["name"] for tool in request.tools]
        with model_turn(context, names):
            prepared, plan = self._summary_plan(request)
            if plan is None:
                return handler(prepared)
            backend = self._get_backend(request.state, request.runtime)
            path = self._require_history(self._offload_to_backend(backend, plan[0]))
            summary = self._create_summary(plan[0])
            prepared, event = self._summary_request(prepared, plan, summary, path)
            check_model_turn(context, minimum=context.policy.stages.expert_request_min_seconds)
            response = handler(prepared)
            return ExtendedModelResponse(
                model_response=response, command=Command(update={"_summarization_event": event})
            )

    async def awrap_model_call(self, request, handler):
        from agent.runtime.stage_budget import check_model_turn, model_turn

        context = get_run_context()
        names = [convert_to_openai_tool(tool)["function"]["name"] for tool in request.tools]
        with model_turn(context, names):
            prepared, plan = self._summary_plan(request)
            if plan is None:
                return await handler(prepared)
            backend = self._get_backend(request.state, request.runtime)
            path = self._require_history(await self._aoffload_to_backend(backend, plan[0]))
            summary = await self._acreate_summary(plan[0])
            prepared, event = self._summary_request(prepared, plan, summary, path)
            check_model_turn(context, minimum=context.policy.stages.expert_request_min_seconds)
            response = await handler(prepared)
            return ExtendedModelResponse(
                model_response=response, command=Command(update={"_summarization_event": event})
            )


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
