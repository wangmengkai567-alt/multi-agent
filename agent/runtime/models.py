"""Count every model invocation, including framework summarization and review."""

import asyncio
from typing import Literal

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable

from agent.runtime.context import get_run_context
from agent.runtime.output import normalize_model_message
from agent.runtime.policies import request_timeout, reserve_calls


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
        try:
            message = self.delegate.invoke(messages, config=config, stop=stop, **kwargs)
        except Exception as exc:
            if is_model_timeout(exc):
                raise ModelRequestTimeout(self.category, kwargs["timeout"]) from exc
            raise
        get_run_context().check_active()
        return ChatResult(generations=[ChatGeneration(message=normalize_model_message(message))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        reserve_calls(self.category)
        config = {"callbacks": run_manager.handlers} if run_manager else None
        kwargs["timeout"] = request_timeout()
        try:
            async with asyncio.timeout(kwargs["timeout"]):
                message = await self.delegate.ainvoke(messages, config=config, stop=stop, **kwargs)
        except Exception as exc:
            if is_model_timeout(exc):
                raise ModelRequestTimeout(self.category, kwargs["timeout"]) from exc
            raise
        get_run_context().check_active()
        return ChatResult(generations=[ChatGeneration(message=normalize_model_message(message))])


def budget_model(model, category):
    if isinstance(model, BudgetChatModel) and model.category == category:
        return model
    return BudgetChatModel(delegate=model, category=category, profile=getattr(model, "profile", None), cache=False)
