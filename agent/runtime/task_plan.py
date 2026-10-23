"""Server-issued task IDs; the initial plan cannot be rewritten to reopen closed work."""

import json
from uuid import uuid4

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from agent.runtime.context import get_run_context
from agent.runtime.events import make_event, redact_text
from agent.runtime.hooks import HookRejected


class ResearchScope(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(
        min_length=1,
        max_length=8000,
        description="当前对象的必要事实；一般购买对比只分派一个明确容量的售价与少量核心使用差异，未指定的其他容量及渠道价格标为可选。不要另建横向综合或购买推荐任务。",
    )


@tool
def plan_research(tasks: list[ResearchScope]) -> str:
    """Register ALL independent research objects once, before delegating. One task per object; comparison and recommendations are synthesized by the main assistant, never an extra search task. Returns immutable task IDs. Revisions reuse IDs; no second plan can reopen completed work."""
    context = get_run_context()
    if context is None or not tasks:
        raise HookRejected("需要运行上下文及非空分工计划")
    with context.budget_lock:
        if context.accepted_plan:
            return json.dumps(list(context.task_scopes.values()), ensure_ascii=False)
        if len(tasks) > context.policy.search.max_tasks_per_run:
            raise HookRejected("分工数量超过本轮上限，请合并相关问题")
        labels = [" ".join(t.label.split()).casefold() for t in tasks]
        if len(labels) != len(set(labels)):
            raise HookRejected("同一对象不能重复登记")
        for item in tasks:
            task_id = str(uuid4())
            context.task_scopes[task_id] = dict(
                task_id=task_id, label=redact_text(item.label), description=item.description
            )
        context.accepted_plan = True
    if context.event_sink:
        context.event_sink.submit(
            make_event(
                context.thread_id,
                context.run_id,
                "research_plan",
                "研究分工已登记",
                {"tasks": [dict(task_id=t["task_id"], label=t["label"]) for t in context.task_scopes.values()]},
                call_id=context.run_id,
                status="ready",
            )
        )
    from agent.runtime.checkpoints import save_job_checkpoint

    save_job_checkpoint(context)
    return json.dumps(list(context.task_scopes.values()), ensure_ascii=False)


def resolve_task(context, description):
    try:
        item = json.loads(description)
    except (ValueError, TypeError):
        item = {}
    task_id = item.get("task_id") if isinstance(item, dict) else None
    if task_id in context.task_scopes:
        scope = context.task_scopes[task_id]
        return task_id, scope["description"], scope["label"]
    if context.enforce_plan:
        raise HookRejected(
            '先调用 plan_research 登记全部独立分工，再将 task.description 设为 {"task_id":"返回的 ID"}；修订必须复用 ID。'
        )
    return None, description, ""
