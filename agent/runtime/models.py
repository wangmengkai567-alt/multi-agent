"""Count every model invocation, including framework summarization and review."""

import asyncio
from contextvars import ContextVar
from time import monotonic
from typing import Literal

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable

from agent.runtime.context import get_run_context
from agent.runtime.governance import register_turn
from agent.runtime.output import normalize_model_message
from agent.runtime.policies import request_timeout, reserve_calls

model_phase = ContextVar("model_phase", default=None)


def observe_request(category, messages, started, message, error):
    """Record timing and sizes, never prompts, credentials or model reasoning."""
    context = get_run_context()
    if context is None or context.event_sink is None:
        return
    from agent.runtime.events import make_event
    from agent.runtime.hooks import active_call

    call = active_call.get()
    usage = (message.usage_metadata or {}) if message is not None else {}
    phase = model_phase.get() or {"sub_model": "research", "review": "semantic_review"}.get(category, "coordination")
    label = {
        "research": "资料收集",
        "report_generation": "事实报告生成",
        "report_revision": "事实报告修订",
        "semantic_review": "证据审核",
        "coordination": "任务协调",
    }[phase]
    context.event_sink.submit(
        make_event(
            context.thread_id,
            context.run_id,
            "model_request",
            label,
            dict(
                category=category,
                phase=phase,
                elapsed_ms=round((monotonic() - started) * 1000),
                input_chars=sum(len(str(item.content)) for item in messages),
                output_chars=len(message.text) if message is not None else 0,
                tool_call_count=len(message.tool_calls) if message is not None else 0,
                invalid_tool_count=len(message.invalid_tool_calls) if message is not None else 0,
                input_units=usage.get("input_tokens"),
                output_units=usage.get("output_tokens"),
                reasoning_units=usage.get("output_token_details", {}).get("reasoning"),
                cached_units=usage.get("input_token_details", {}).get("cache_read"),
                unit="tokens",
                error_type=type(error).__name__ if error is not None else None,
            ),
            actor=call.target if call else "main",
            status="failed" if error is not None else "completed",
            parent_call_id=call.call_id if call else context.run_id,
        )
    )


class ModelRequestTimeout(TimeoutError):
    def __init__(self, category, seconds):
        self.category = category
        self.seconds = seconds
        label = {"main_model": "任务协调模型", "sub_model": "子助手模型", "review": "结果检查模型"}.get(
            category, "模型"
        )
        super().__init__(f"{label}请求超时（上限 {seconds:g} 秒）")


def is_model_timeout(error):
    return isinstance(error, TimeoutError | httpx.TimeoutException) or type(error).__name__ == "APITimeoutError"


class BudgetChatModel(BaseChatModel):
    delegate: Runnable
    category: Literal["main_model", "sub_model", "review"] = "main_model"

    @property
    def _llm_type(self):
        return "budgeted-chat-model"

    def bind_tools(self, tools, **kwargs):
        return BudgetChatModel(
            delegate=self.delegate.bind_tools(tools, **kwargs),
            category=self.category,
            profile=self.profile,
            cache=False,
        )

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        reserve_calls(self.category)
        config = {"callbacks": run_manager.handlers} if run_manager else None
        kwargs["timeout"] = request_timeout()
        started, message, error = monotonic(), None, None
        try:
            message = self.delegate.invoke(messages, config=config, stop=stop, **kwargs)
        except BaseException as exc:
            error = exc
            if is_model_timeout(exc):
                raise ModelRequestTimeout(self.category, kwargs["timeout"]) from exc
            raise
        finally:
            observe_request(self.category, messages, started, message, error)
        get_run_context().check_active()
        return ChatResult(generations=[ChatGeneration(message=register_turn(normalize_model_message(message)))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        reserve_calls(self.category)
        config = {"callbacks": run_manager.handlers} if run_manager else None
        kwargs["timeout"] = request_timeout()
        started, message, error = monotonic(), None, None
        try:
            async with asyncio.timeout(kwargs["timeout"]):
                message = await self.delegate.ainvoke(messages, config=config, stop=stop, **kwargs)
        except BaseException as exc:
            error = exc
            if is_model_timeout(exc):
                raise ModelRequestTimeout(self.category, kwargs["timeout"]) from exc
            raise
        finally:
            observe_request(self.category, messages, started, message, error)
        get_run_context().check_active()
        return ChatResult(generations=[ChatGeneration(message=register_turn(normalize_model_message(message)))])


def budget_model(model, category):
    if isinstance(model, BudgetChatModel) and model.category == category:
        return model
    return BudgetChatModel(delegate=model, category=category, profile=getattr(model, "profile", None), cache=False)
