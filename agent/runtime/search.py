"""Fair per-task search budgets, reserved refinement calls and a whole-run hard cap."""

import json
from contextvars import ContextVar
from dataclasses import dataclass, field

from langchain.agents.middleware import AgentMiddleware, ModelResponse
from langchain_core.messages import AIMessage

from agent.runtime.experts import append_system
from agent.runtime.policies import SearchLimitReached, load_runtime_policy
from agent.runtime.review_state import ReviewState, deliverable


@dataclass
class SearchTaskBudget:
    calls: set[str] = field(default_factory=set)
    page_calls: set[str] = field(default_factory=set)
    refinement_start: int = 0
    refinement_open: bool = False
    finalize_only: bool = False
    active: bool = False
    closed_reason: str | None = None
    state: ReviewState = field(default_factory=ReviewState)
    current_result_id: str | None = None
    result_run_id: str | None = None
    report_attempt: int = 0
    evidence_version: int = 0
    invalid_decisions: int = 0
    result: str = ""
    sources: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    description: str = ""
    failure_reason: str = ""
    report: dict | None = None


def search_must_finish(context):
    # Two exhausted search failures are a signal to summarize, not rename tasks.
    with context.budget_lock:
        tasks = list(context.search_tasks.values())
        if context.enforce_plan and context.accepted_plan and tasks and not any(t.active for t in tasks):
            from agent.runtime.requirements import task_requirements

            planned = {key for key in context.task_scopes if task_requirements(context, key)}
            if context.plan_closed and planned.issubset(context.search_tasks):
                return True
        return (
            not context.enforce_plan
            and context.business_mode == "ordinary"
            and not any(task.active for task in tasks)
            and sum(not deliverable(task.state) for task in tasks) >= 2
        )


class SearchAllowance:
    def __init__(self, context, task_key=None):
        self.context = context
        self.task_key = task_key or context.run_id
        self.lock = context.budget_lock
        with self.lock:
            if context.policy is None:
                context.policy = load_runtime_policy()
            if self.task_key not in context.search_tasks:
                if search_must_finish(context):
                    raise SearchLimitReached(
                        "本轮已有两个检索任务结束允许的修订，现进入汇总阶段；不要改写描述重新委派。请说明已核实内容和剩余缺口。"
                    )
                if len(context.search_tasks) >= context.policy.search.max_tasks_per_run:
                    raise SearchLimitReached(
                        "本轮检索子任务数量已达到上限，未启动新的检索任务；请复用已有结果并说明缺口。"
                    )
                context.search_tasks[self.task_key] = SearchTaskBudget()
            self.task = context.search_tasks[self.task_key]
        self.calls = self.task.calls

    def claim(self):
        from agent.runtime.hooks import HookRejected

        with self.lock:
            if self.task.closed_reason:
                raise HookRejected(self.task.closed_reason + "；本轮不重复执行相同检索任务，请复用已有结果或说明缺口。")
            if self.task.active:
                raise HookRejected("相同检索子任务已在执行，请等待已有任务，不重复委派。")
            self.task.active = True

    def finish(self, *, executed, state, reason=None, result="", sources=()):
        with self.lock:
            self.task.active = False
            if executed:
                self.task.state = state
                self.task.result = result
                self.task.sources = list(sources)
                self.task.closed_reason = self.task.closed_reason or reason or "该检索子任务本轮已完成"

    @staticmethod
    def complex_task(context):
        complex_skills = {"research", "ecommerce-analysis", "report-writing"}
        with context.budget_lock:
            return (
                context.business_mode == "internal_external"
                or bool(set(context.skill_ids) & complex_skills)
                or any(item[2] in complex_skills and item[3] == "loaded" for item in context.skill_states)
            )

    @property
    def group_key(self):
        scope = self.context.task_scopes.get(self.task_key, {})
        obj = self.context.objects.get(scope.get("object_id"), {})
        return obj.get("budget_owner_id", scope.get("object_id", self.task_key))

    def group_tasks(self):
        return [
            key for key in self.context.search_tasks if SearchAllowance(self.context, key).group_key == self.group_key
        ]

    def used(self, kind):
        return sum(
            len(getattr(self.context.search_tasks[key], "calls" if kind == "search" else "page_calls"))
            for key in self.group_tasks()
        )

    def minimum_run_reserve(self):
        groups = {}
        for key, scope in self.context.task_scopes.items():
            obj = self.context.objects[scope["object_id"]]
            group = obj.get("budget_owner_id", scope["object_id"])
            groups.setdefault(group, []).append(key)
        return sum(
            group != self.group_key
            and not any(self.context.search_tasks.get(key) and self.context.search_tasks[key].calls for key in keys)
            and any(
                any(
                    r["task_id"] == key and r["query_version"] == self.context.query_version
                    for r in self.context.requirements.values()
                )
                and (key not in self.context.search_tasks or not self.context.search_tasks[key].closed_reason)
                for key in keys
            )
            for group, keys in groups.items()
        )

    def initial_limit(self):
        limits = self.context.policy.search
        return limits.research_calls_per_task if self.complex_task(self.context) else limits.calls_per_task

    def run_limit(self):
        limits = self.context.policy.search
        return limits.research_calls_per_run if self.complex_task(self.context) else limits.calls_per_run

    def limit(self):
        return self.initial_limit() + (
            self.context.policy.search.refinement_calls_per_task if self.task.refinement_open else 0
        )

    def begin_refinement(self, *, allow_search):
        with self.lock:
            # Opening this phase is idempotent; no attempt or re-delegation resets usage.
            if not self.task.refinement_open:
                self.task.refinement_start = len(self.task.evidence)
            self.task.refinement_open = True
            self.task.finalize_only = not allow_search

    def snapshot(self):
        with self.lock:
            return dict(
                task_id=self.task_key,
                phase="refinement" if self.task.refinement_open else "initial",
                task_used=len(self.calls),
                task_limit=self.limit(),
                run_used=len(self.context.search_calls),
                run_limit=self.run_limit(),
                finalize_only=self.task.finalize_only,
            )

    def can_read_page(self):
        from agent.runtime.reservations import resource_available

        return not self.task.finalize_only and resource_available(self.context, self, "read")

    def exhausted(self):
        from agent.runtime.reservations import resource_available

        return self.task.finalize_only or not resource_available(self.context, self, "search")


search_allowance = ContextVar("search_allowance", default=None)


def needs_search_refinement(review, evidence):
    from agent.runtime.context import get_run_context

    requirements = get_run_context().requirements
    return any(
        item["kind"] == "missing_requirement" and requirements[item["rid"]]["required"] for item in review.findings
    )


class SearchExecutionMiddleware(AgentMiddleware):
    def prepare_request(self, request):
        allowance = search_allowance.get()
        if allowance is None:
            return request
        from agent.runtime.research import available_actions

        context = allowance.context
        actions = available_actions(context, allowance)
        if allowance.task.finalize_only or actions["allowed_actions"] == ["report"]:
            return ModelResponse(result=[AIMessage(content="资料收集完成；已有缺口由报告步骤保留。")])
        note = (
            "当前日期 "
            + context.reference_time.isoformat()
            + "。只用 research_step；一次一个动作。search/read 必须复制本任务的 gap_id/source_id；read 不传 query，程序按需求构造。资料足够就 report，不为可选背景扩大调查。\n"
            + json.dumps(actions, ensure_ascii=False)
        )
        return request.override(
            system_message=append_system(request.system_message, note),
            model_settings={**request.model_settings, "strict": True, "parallel_tool_calls": False},
        )

    def wrap_model_call(self, request, handler):
        prepared = self.prepare_request(request)
        return prepared if isinstance(prepared, ModelResponse) else handler(prepared)

    async def awrap_model_call(self, request, handler):
        prepared = self.prepare_request(request)
        return prepared if isinstance(prepared, ModelResponse) else await handler(prepared)
