"""Build controlled executors and register them with the framework task route."""

from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.summarization import SummarizationMiddleware, _compute_summarization_defaults
from langchain.agents import create_agent

from agent.reflection import ResultReviewer
from agent.results import RESULT_PROMPT, ResultService
from agent.runtime.backend import run_backend
from agent.runtime.delegation import DelegationRunnable
from agent.runtime.hooks import HookToolMiddleware
from agent.runtime.models import budget_model
from agent.runtime.reporting import GroundedSearchExecutor
from agent.runtime.search import SearchExecutionMiddleware
from agent.runtime.skills import ProjectSkillsMiddleware


def build_subagents(registry, sub_model, *, review_model, models=None, hooks=None):
    models = models or {}
    if set(models) - set(registry.specs):
        raise ValueError("Model override refers to an unregistered expert")
    result = []
    results = ResultService()
    for spec in registry.specs.values():
        raw_model = models.get(spec.agent_id, sub_model)
        if getattr(raw_model, "max_retries", 0) != spec.model_config.max_retries:
            raise ValueError("Expert model SDK retries must match the registry's zero-retry setting")
        child_model = budget_model(raw_model, "sub_model")
        defaults = _compute_summarization_defaults(child_model)
        prompt = (
            spec.system_prompt
            + "\n"
            + (
                "本阶段只收集当前分工所需的真实依据。资料足够或额度用尽时结束收集，"
                "最后只输出‘资料收集完成’，不概述资料、不编写事实报告；报告由后续独立步骤生成。"
                if spec.review_config.grounded_report
                else RESULT_PROMPT
            )
        )
        executor = create_agent(
            model=child_model,
            tools=list(spec.tools),
            system_prompt=prompt,
            middleware=[
                *(
                    [SearchExecutionMiddleware(prompt, grounded_report=spec.review_config.grounded_report)]
                    if spec.agent_id == "search"
                    else []
                ),
                *([ProjectSkillsMiddleware(spec.allowed_skill_ids)] if spec.allowed_skill_ids else []),
                FilesystemMiddleware(backend=run_backend),
                SummarizationMiddleware(
                    model=child_model,
                    backend=run_backend,
                    trigger=defaults["trigger"],
                    keep=defaults["keep"],
                    trim_tokens_to_summarize=None,
                    truncate_args_settings=defaults["truncate_args_settings"],
                ),
                PatchToolCallsMiddleware(),
                HookToolMiddleware(spec.agent_id, hooks=hooks),
            ],
            name=spec.agent_id,
        )
        if spec.review_config.grounded_report:
            executor = GroundedSearchExecutor(executor, child_model)
        result.append(
            {
                "name": spec.agent_id,
                "description": f"{spec.display_name}：{spec.description}",
                "runnable": DelegationRunnable(
                    spec,
                    executor,
                    hooks=hooks,
                    reviewer=ResultReviewer(spec.review_config, review_model),
                    results=results,
                ),
            }
        )
    return result
