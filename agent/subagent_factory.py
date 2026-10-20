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
from agent.runtime.search import SearchExecutionMiddleware
from agent.runtime.skills import ProjectSkillsMiddleware


def build_subagents(registry, model, *, models=None, hooks=None):
    models = models or {}
    if set(models) - set(registry.specs):
        raise ValueError("Model override refers to an unregistered expert")
    result = []
    results = ResultService()
    for spec in registry.specs.values():
        raw_model = models.get(spec.agent_id, model)
        if getattr(raw_model, "max_retries", 0) != spec.model_config.max_retries:
            raise ValueError("Expert model SDK retries must match the registry's zero-retry setting")
        child_model = budget_model(raw_model, "sub_model")
        defaults = _compute_summarization_defaults(child_model)
        executor = create_agent(
            model=child_model,
            tools=list(spec.tools),
            system_prompt=spec.system_prompt + "\n" + RESULT_PROMPT,
            middleware=[
                *(
                    [SearchExecutionMiddleware(spec.system_prompt + "\n" + RESULT_PROMPT)]
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
        result.append(
            {
                "name": spec.agent_id,
                "description": f"{spec.display_name}：{spec.description}",
                "runnable": DelegationRunnable(
                    spec, executor, hooks=hooks, reviewer=ResultReviewer(spec.review_config, raw_model), results=results
                ),
            }
        )
    return result
