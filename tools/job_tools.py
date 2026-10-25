"""Nonblocking submission and inspection of durable research jobs."""

import json
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from agent.runtime.context import get_run_context
from agent.runtime.evidence_protocol import evidence_tool
from agent.runtime.hooks import HookRejected


def public_job(job):
    import copy

    from utils.artifact_links import artifact_url

    value = {
        key: job.get(key)
        for key in ("task_id", "kind", "status", "result", "error", "poll_after_seconds", "execution_run_id")
    }
    value["result"] = copy.deepcopy(value["result"])
    if value["result"] and "artifacts" in value["result"]:
        for artifact in value["result"]["artifacts"]:
            artifact["url"] = artifact_url(job["run_id"], artifact["relative_path"])
    return value


@evidence_tool("network-control.v1")
@tool
async def submit_research_job(description: Annotated[str, Field(min_length=1, max_length=12000)]) -> str:
    """Submit a complete, long research objective as a durable background job. Returns task_id and working immediately. The UI polls automatically; do not busy-poll or claim completion. Only one research job per round; retries reuse it."""
    context = get_run_context()
    if (
        context is None
        or context.job_service is None
        or context.background_job
        or context.execution_mode == "foreground"
    ):
        raise HookRejected("当前执行环境不能创建嵌套后台任务")
    from agent.runtime.contracts import CONTRACT_VERSION, assert_contract, background_eligible

    contract = assert_contract(context)
    if not background_eligible(contract):
        raise HookRejected("当前契约不允许创建公开研究后台任务")
    job = await context.job_service.submit(
        context.thread_id,
        context.run_id,
        "research",
        {
            "description": description,
            "user_query": context.user_query,
            "input_text": context.input_text,
            "network_policy": context.network_policy,
            "required_sources": list(context.required_sources),
            "contract_version": CONTRACT_VERSION,
            "task_contract": contract.model_dump(mode="json"),
        },
        context.run_id + ":research",
        context.policy.timeouts.job_seconds,
    )
    context.pending_jobs[job["task_id"]] = public_job(job)
    return json.dumps(public_job(job), ensure_ascii=False)


@evidence_tool("control.v1")
@tool
async def get_research_job(task_id: str) -> str:
    """Get an owned durable job's state and result. Respect poll_after_seconds; the browser already polls automatically."""
    context = get_run_context()
    if context is None or context.job_service is None:
        raise HookRejected("后台任务服务不可用")
    job = await context.job_service.get(task_id, context.thread_id)
    if job is None:
        raise HookRejected("找不到本会话的后台任务")
    return json.dumps(public_job(job), ensure_ascii=False)
