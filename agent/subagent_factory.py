# 专家工厂：按注册表构建子助手图，再包装为由运行时管理生命周期的可调用对象。
"""Build the three registered expert graphs with the same controlled tool boundary."""

from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel

from agent.runtime.backend import run_backend
from agent.runtime.capabilities import READONLY_FILESYSTEM_PROMPT, CapabilityDisclosureMiddleware
from agent.runtime.delegation import DelegationRunnable
from agent.runtime.evidence_protocol import bind_framework_tools
from agent.runtime.hooks import HookToolMiddleware
from agent.runtime.models import BudgetSummarizationMiddleware, budget_model
from agent.runtime.skills import ProjectSkillsMiddleware, SkillReadMiddleware


def validate_model(model, *, needs_tools=False, tools=()):
    if not isinstance(model, BaseChatModel):
        raise ValueError("Model profiles require BaseChatModel")
    # 客户端不能自行重试，否则会绕过外层预算和调用记录。
    if getattr(model, "max_retries", 0) != 0:
        raise ValueError("Model SDK retries must be zero")
    if needs_tools:
        if (model.profile or {}).get("tool_calling") is False:
            raise ValueError("Model does not support tool calling")
        try:
            model.bind_tools(list(tools))
        except NotImplementedError as exc:
            raise ValueError("Model does not implement tool calling") from exc


def build_subagents(registry, models, *, hooks=None):
    needed = {spec.model_config.profile for spec in registry.specs.values()}
    if needed - models.keys():
        raise ValueError("Missing expert model profile: " + ", ".join(sorted(needed - models.keys())))
    agents = []
    for spec in registry.specs.values():
        raw = models[spec.model_config.profile]
        validate_model(raw, needs_tools=bool(spec.tools), tools=spec.tools)
        maximum = (raw.profile or {}).get("max_input_tokens")
        if type(maximum) is int and spec.summary.trigger_tokens >= maximum:
            raise ValueError("Configure the expert summary trigger below the model context limit")
        # 子助手对话和框架摘要都经过同一个预算模型，防止摘要调用漏计。
        model = budget_model(raw, "sub_model")
        graph = create_agent(
            model=model,
            tools=list(spec.tools),
            name=spec.agent_id,
            system_prompt=spec.system_prompt
            + "\n完成指定分工后直接返回简明结论及实际来源；说明读取范围、工具失败和未知项。不要编造查询或来源。"
            "任务身份、执行状态、来源归属和工具限制由程序记录，不要自行填写或声称覆盖未知事项。",
            middleware=[
                SkillReadMiddleware(),
                *([ProjectSkillsMiddleware(spec.allowed_skill_ids)] if spec.allowed_skill_ids else []),
                bind_framework_tools(
                    FilesystemMiddleware(backend=run_backend, system_prompt=READONLY_FILESYSTEM_PROMPT)
                ),
                BudgetSummarizationMiddleware(model=model, backend=run_backend, **spec.summary.kwargs()),
                PatchToolCallsMiddleware(),
                CapabilityDisclosureMiddleware(),
                HookToolMiddleware(spec.agent_id, hooks=hooks, managed_delegation=True),
            ],
        )
        agents.append(
            dict(
                name=spec.agent_id,
                description=f"{spec.display_name}：{spec.description}",
                # 图只负责推理；任务身份、排队、来源校验和成功失败状态由此包装层负责。
                runnable=DelegationRunnable(spec, graph, hooks=hooks, registry=registry),
            )
        )
    return agents
