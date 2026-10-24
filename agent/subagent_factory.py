"""Compile experts solely from validated definitions and explicit model profiles."""

from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.subagents import SubAgentMiddleware
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel

from agent.reflection import ResultReviewer
from agent.results import ResultService
from agent.runtime.backend import run_backend
from agent.runtime.delegation import DelegationRunnable
from agent.runtime.experts import SerialDelegationMiddleware
from agent.runtime.hooks import HookToolMiddleware
from agent.runtime.models import budget_model
from agent.runtime.reporting import GroundedSearchExecutor
from agent.runtime.search import SearchExecutionMiddleware
from agent.runtime.skills import ProjectSkillsMiddleware


def validate_model(model, *, needs_tools=False, tools=()):
    if not isinstance(model, BaseChatModel):
        raise ValueError("Model profiles require BaseChatModel")
    if getattr(model, "max_retries", 0) != 0:
        raise ValueError("Model SDK retries must be zero")
    if (model.profile or {}).get("structured_output") is False:
        raise ValueError("Model does not support structured output")
    if needs_tools:
        if (model.profile or {}).get("tool_calling") is False:
            raise ValueError("Model does not support tool calling")
        try:
            model.bind_tools(list(tools))
        except NotImplementedError as exc:
            raise ValueError("Model does not implement tool calling") from exc


def build_subagents(registry, models, *, hooks=None):
    needed = {p for s in registry.specs.values() for p in (s.model_config.profile, s.model_config.review_profile)}
    if needed - models.keys():
        raise ValueError("Missing expert model profile: " + ", ".join(sorted(needed - models.keys())))
    for spec in registry.specs.values():
        raw, review = models[spec.model_config.profile], models[spec.model_config.review_profile]
        validate_model(raw, needs_tools=bool(spec.tools or spec.delegate_targets), tools=spec.tools)
        validate_model(review)
        maximum = (raw.profile or {}).get("max_input_tokens")
        if type(maximum) is int and spec.summary.trigger_tokens >= maximum:
            raise ValueError("Configure the expert summary trigger below the model context limit")
    results, compiled = ResultService(), {}

    def build(key):
        if key in compiled:
            return compiled[key]
        spec = registry.get(key)
        raw = models[spec.model_config.profile]
        child_model = budget_model(raw, "sub_model")
        prompt = spec.system_prompt + (
            "\n本阶段只收集当前分工的真实依据；资料足够或额度用尽时结束，不编写事实报告。"
            "事实报告由后续独立步骤生成。委派返回的已审核事实也必须保留实际工具证据链。"
        )
        middleware = [
            *(
                [SearchExecutionMiddleware(prompt, grounded_report=spec.review_config.grounded_report)]
                if spec.execution.planned
                else []
            ),
            *(
                [
                    SubAgentMiddleware(backend=run_backend, subagents=[build(t) for t in spec.delegate_targets]),
                    SerialDelegationMiddleware(),
                ]
                if spec.delegate_targets
                else []
            ),
            *([ProjectSkillsMiddleware(spec.allowed_skill_ids)] if spec.allowed_skill_ids else []),
            FilesystemMiddleware(backend=run_backend),
            SummarizationMiddleware(model=child_model, backend=run_backend, **spec.summary.kwargs()),
            PatchToolCallsMiddleware(),
            HookToolMiddleware(
                spec.agent_id,
                allowed_targets={t: registry.get(t) for t in spec.delegate_targets},
                hooks=hooks,
                managed_delegation=True,
            ),
        ]
        researcher = create_agent(
            model=child_model, tools=list(spec.tools), system_prompt=prompt, middleware=middleware, name=spec.agent_id
        )
        executor = GroundedSearchExecutor(researcher, child_model)
        compiled[key] = dict(
            name=spec.agent_id,
            description=f"{spec.display_name}：{spec.description}；输入协议={spec.execution.input_kind}，输出=已审核事实报告",
            runnable=DelegationRunnable(
                spec,
                executor,
                hooks=hooks,
                reviewer=ResultReviewer(spec.review_config, models[spec.model_config.review_profile]),
                results=results,
                registry=registry,
            ),
        )
        return compiled[key]

    return [build(key) for key in registry.specs]
