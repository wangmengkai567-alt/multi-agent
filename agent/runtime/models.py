"""Count every model invocation, including framework summarization and review."""

from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable

from agent.runtime.policies import reserve_calls


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
        message = self.delegate.invoke(messages, config=config, stop=stop, **kwargs)
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        reserve_calls(self.category)
        config = {"callbacks": run_manager.handlers} if run_manager else None
        message = await self.delegate.ainvoke(messages, config=config, stop=stop, **kwargs)
        return ChatResult(generations=[ChatGeneration(message=message)])


def budget_model(model, category):
    if isinstance(model, BudgetChatModel) and model.category == category:
        return model
    return BudgetChatModel(delegate=model, category=category, profile=getattr(model, "profile", None), cache=False)
