"""Bind expert contracts to runs and enforce serial, allowlisted nested delegation."""

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage

from agent.runtime.context import RunStopped, get_run_context


def append_system(message, text):
    blocks = (
        list(message.content)
        if message is not None and isinstance(message.content, list)
        else ([dict(type="text", text=message.content)] if message is not None and message.content else [])
    )
    blocks.append(dict(type="text", text="\n\n" + text))
    return SystemMessage(content=blocks)


def bind_registry(context, registry):
    contract = registry.contract()
    with context.budget_lock:
        if context.expert_contract and context.expert_contract != contract:
            context.stop("interrupted")
            raise RunStopped("Expert definitions changed; retain the checkpoint and start a new reviewed run")
        context.expert_contract = contract


def source_kind(context, target):
    return context.expert_contract[target]["execution"]["source_kind"]


class ExpertRegistryMiddleware(AgentMiddleware):
    def __init__(self, registry):
        self.registry = registry

    def before_agent(self, state, runtime):
        bind_registry(get_run_context(), self.registry)

    async def abefore_agent(self, state, runtime):
        bind_registry(get_run_context(), self.registry)


class SerialDelegationMiddleware(AgentMiddleware):
    """Ask for one tool at a time; the runtime also serializes batched tasks."""

    def prepare(self, request):
        return request.override(model_settings={**request.model_settings, "parallel_tool_calls": False})

    def wrap_model_call(self, request, handler):
        return handler(self.prepare(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self.prepare(request))
