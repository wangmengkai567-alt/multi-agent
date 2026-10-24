"""Versioned append-only plans with server identities and inherited budget ownership."""

import json
from copy import deepcopy
from typing import Literal
from uuid import uuid4

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field

from agent.runtime.context import get_run_context
from agent.runtime.events import make_event, redact_text
from agent.runtime.hooks import HookRejected
from agent.runtime.requirements import (
    RequirementSpec,
    attach_support_requirements,
    ensure_original_requirements,
    register,
    task_requirements,
)
from agent.runtime.source_policy import normalize_domain, query_domains, requested_tiers


class ExplicitCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    dimension: Literal["model", "variant", "region", "channel", "window", "eligibility", "price_kind"]
    value: str = Field(min_length=1, max_length=200)


class PlanRequirement(BaseModel):
    """Compact tool wire format; the runtime still has one full RequirementSpec."""

    model_config = ConfigDict(extra="forbid", strict=True)
    anchor: str = Field(min_length=1, max_length=2000)
    label: str = Field(min_length=1, max_length=160)
    kind: Literal["fact", "comparison", "recommendation", "unresolved"]
    conditions: list[ExplicitCondition] = Field(default_factory=list, max_length=7)

    def as_spec(self, context):
        values = {item.dimension: item.value for item in self.conditions}
        if len(values) != len(self.conditions):
            raise HookRejected("An explicit condition dimension cannot be repeated")
        return RequirementSpec(
            anchor=self.anchor,
            label=self.label,
            kind=self.kind,
            required_tier=list(
                context.task_contract.required_source_tiers
                if context.task_contract
                else requested_tiers(context.user_query)
            ),
            **values,
        )


class ResearchScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    object_name: str = Field(min_length=1, max_length=2000)
    object_id: str | None = Field(default=None, description="Existing object ID for supporting work; never invent IDs")
    discovery_fact_id: str | None = Field(
        default=None, description="Current approved fact proving a newly discovered name"
    )
    depends_on: list[str] = Field(default_factory=list, max_length=32)
    search_domains: list[str] = Field(default_factory=list, max_length=20)
    requirements: list[PlanRequirement] = Field(max_length=16)
    investigations: list[str] = Field(max_length=32)
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=8000)


def plan_snapshot(context):
    tasks = []
    for scope in context.task_scopes.values():
        task = context.search_tasks.get(scope["task_id"])
        deps = [context.search_tasks.get(key) for key in scope["depends_on"]]
        blocked = any(
            d and d.closed_reason and (not d.state.judged or d.state.facts_status == "none_passed") for d in deps
        )
        status = (
            "blocked"
            if blocked
            else "finished"
            if task and task.closed_reason
            else "running"
            if task and task.active
            else "waiting"
            if any(d is None or not d.closed_reason for d in deps)
            else "ready"
        )
        tasks.append({**scope, "status": status, "check_status": task.state.check_status if task else "not_checked"})
    return dict(
        version=context.plan_version, closed=context.plan_closed, expansions=context.plan_expansions, tasks=tasks
    )


def _add(context, items, *, initial):
    from agent.runtime.delivery import fact_pool

    facts = fact_pool(context)["facts"] if not initial else {}
    names = {" ".join(obj["user_name"].split()).casefold() for obj in context.objects.values()}
    pending = []
    for item in items:
        if len(set(item.depends_on)) != len(item.depends_on) or any(
            key not in context.task_scopes for key in item.depends_on
        ):
            raise HookRejected("Dependencies must be unique existing task IDs; append cannot create cycles")
        domains = sorted({normalize_domain(domain) for domain in item.search_domains})
        if domains and not set(domains).issubset(query_domains(context.user_query)):
            raise HookRejected("Search domains must be explicitly specified by the original user")
        object_id = item.object_id
        if object_id is not None:
            obj = context.objects.get(object_id)
            if (
                initial
                or obj is None
                or obj["query_version"] != context.query_version
                or item.object_name != obj["user_name"]
                or item.discovery_fact_id
            ):
                raise HookRejected("Supporting work must retain one existing object identity")
            if item.requirements:
                raise HookRejected("Additional work cannot add mandatory user requirements")
        else:
            name = " ".join(item.object_name.split()).casefold()
            if name in names:
                raise HookRejected("Object already registered; reuse its object_id without refreshing its budget")
            proof = None
            if item.object_name not in context.user_query:
                proof = facts.get(item.discovery_fact_id)
                if initial or proof is None or proof.get("object_id") not in context.objects:
                    raise HookRejected("A discovered object requires a current approved fact and an original scope")
                quotes = [r["quote"] for r in (*proof["evidence"], *proof.get("identity_evidence", []))]
                if not any(item.object_name in quote for quote in quotes):
                    raise HookRejected("Discovered object name is absent from its verified source")
            elif item.discovery_fact_id:
                raise HookRejected("Original objects do not require invented discovery evidence")
            if not initial and item.requirements:
                raise HookRejected("Appended tasks are supporting investigations, not new completion gates")
            object_id = str(uuid4())
            parent_id = proof["object_id"] if proof else None
            if parent_id is not None:
                parent_scope = next(s for s in context.task_scopes.values() if s["object_id"] == parent_id)
                if domains and domains != parent_scope["search_domains"]:
                    raise HookRejected("Discovered work must retain its original source scope")
                domains = parent_scope["search_domains"]
            budget_owner = context.objects[parent_id].get("budget_owner_id", parent_id) if parent_id else object_id
            context.objects[object_id] = dict(
                object_id=object_id,
                user_name=item.object_name,
                query_version=context.query_version,
                budget_owner_id=budget_owner,
                discovery_fact_id=item.discovery_fact_id,
                parent_object_id=parent_id,
            )
            names.add(name)
        duplicates = [
            s
            for s in context.task_scopes.values()
            if s["object_id"] == object_id
            and s["description"] == item.description
            and s["investigations"] == item.investigations
        ]
        if duplicates:
            raise HookRejected("Same investigation already registered; closed tasks cannot be recreated")
        task_id = object_id if initial else str(uuid4())
        inherited = [s for s in context.task_scopes.values() if s["object_id"] == object_id]
        if inherited:
            inherited_domains = inherited[0]["search_domains"]
            if domains and domains != inherited_domains:
                raise HookRejected("Append cannot change an existing object's source scope")
            domains = inherited_domains
        context.task_scopes[task_id] = dict(
            task_id=task_id,
            object_id=object_id,
            object_name=item.object_name,
            label=redact_text(item.label),
            description=item.description,
            investigations=list(item.investigations),
            depends_on=list(item.depends_on),
            search_domains=domains,
            plan_version=context.plan_version + 1,
            requirement_ids=[],
        )
        pending.append((task_id, object_id, item))
    for task_id, object_id, item in pending:
        if initial:
            register(context, task_id, [r.as_spec(context) for r in item.requirements], object_id=object_id)
    return pending


@tool
def plan_research(
    tasks: list[ResearchScope],
    main_requirements: list[PlanRequirement],
    operation: Literal["initialize", "append", "seal"] = "initialize",
    expected_version: int = 0,
) -> str:
    """Manage one append-only plan. initialize registers original objects/requirements; append adds supporting work using existing object_id or a verified discovery_fact_id, never new mandatory requirements. expected_version must equal the latest returned version. seal uses tasks=[]/main_requirements=[] after all planned work finishes. task.description must be JSON with a returned task_id. Observe ready/waiting states; never recreate closed work."""
    context = get_run_context()
    if context is None:
        raise HookRejected("A plan requires an owned run")
    from agent.runtime.checkpoints import save_job_checkpoint

    with context.budget_lock:
        context.check_active()
        if not context.collecting or context.plan_closed:
            raise HookRejected("Plan is closed for collection")
        if expected_version != context.plan_version:
            raise HookRejected(f"Plan version changed; expected_version must be {context.plan_version}")
        if operation == "seal":
            if tasks or main_requirements or not context.accepted_plan:
                raise HookRejected("seal requires an accepted plan and empty task/requirement lists")
            pending = {
                key
                for key in context.task_scopes
                if key not in context.search_tasks or not context.search_tasks[key].closed_reason
            }
            blocked = set()
            while True:
                previous = len(blocked)
                for key in pending - blocked:
                    dependency_ids = context.task_scopes[key]["depends_on"]
                    dependencies = [context.search_tasks.get(dep) for dep in dependency_ids]
                    finished = all(
                        dep in blocked or task is not None and task.closed_reason
                        for dep, task in zip(dependency_ids, dependencies, strict=True)
                    )
                    failed = any(
                        dep in blocked
                        or task is not None
                        and task.closed_reason
                        and (not task.state.judged or task.state.facts_status == "none_passed")
                        for dep, task in zip(dependency_ids, dependencies, strict=True)
                    )
                    if dependency_ids and finished and failed:
                        blocked.add(key)
                if len(blocked) == previous:
                    break
            if pending - blocked:
                raise HookRejected("Cannot seal while tasks are pending or active")
            from agent.runtime.review_state import ReviewState
            from agent.runtime.search import SearchTaskBudget

            for key in blocked:
                context.search_tasks[key] = SearchTaskBudget(
                    closed_reason="dependency_failed: no approved prerequisite facts",
                    failure_reason="dependency_failed: task was not executed",
                    state=ReviewState(check_status="dependency_unavailable"),
                )
            context.plan_closed = True
        else:
            if not tasks or (operation == "initialize") == context.accepted_plan:
                raise HookRejected("Initialize once, then append to the existing plan")
            if len(context.task_scopes) + len(tasks) > context.policy.search.max_tasks_per_run:
                raise HookRejected("Total planned tasks exceed the run limit")
            if operation == "append" and (
                main_requirements or context.plan_expansions >= context.policy.search.max_plan_expansions
            ):
                raise HookRejected("Append cannot change original requirements or exceed expansion rounds")
            fields = ("requirements", "coverage_rows", "gaps", "objects", "requirement_links", "task_scopes")
            original = {key: deepcopy(getattr(context, key)) for key in fields}
            try:
                pending = _add(context, tasks, initial=operation == "initialize")
                if operation == "initialize":
                    register(context, None, [r.as_spec(context) for r in main_requirements], owner="main")
                    ensure_original_requirements(context)
                attach_support_requirements(context, [key for key, _, _ in pending])
                for task_id, _, _ in pending:
                    context.task_scopes[task_id]["requirement_ids"] = [
                        r["rid"] for r in task_requirements(context, task_id)
                    ]
            except Exception as error:
                for key, value in original.items():
                    setattr(context, key, value)
                raise HookRejected(str(error)) from error
            context.accepted_plan = True
            if operation == "append":
                context.plan_expansions += 1
        context.plan_version += 1
        save_job_checkpoint(context)
        snapshot = plan_snapshot(context)
    if context.event_sink:
        context.event_sink.submit(
            make_event(
                context.thread_id,
                context.run_id,
                "research_plan",
                "研究计划：" + operation,
                {
                    "version": context.plan_version,
                    "closed": context.plan_closed,
                    "tasks": [
                        {"task_id": t["task_id"], "label": t["label"], "status": t["status"]} for t in snapshot["tasks"]
                    ],
                },
                call_id=context.run_id,
                status="ready",
            )
        )
    return json.dumps(snapshot, ensure_ascii=False)


def resolve_task(context, description):
    try:
        item = json.loads(description)
    except (ValueError, TypeError):
        item = {}
    task_id = item.get("task_id") if isinstance(item, dict) else None
    if task_id in context.task_scopes:
        scope = context.task_scopes[task_id]
        deps = [context.search_tasks.get(key) for key in scope["depends_on"]]
        if any(task is None or not task.closed_reason for task in deps):
            raise HookRejected("Task dependencies are still pending; dispatch the ready task IDs first")
        if any(not task.state.judged or task.state.facts_status == "none_passed" for task in deps):
            raise HookRejected("Dependency has no approved facts; do not claim the dependent work executed")
        description = scope["description"]
        if scope["investigations"]:
            description += "\n\n研究工作（不是额外完成门槛）：\n" + "\n".join("- " + x for x in scope["investigations"])
        return task_id, description, scope["label"]
    if context.enforce_plan:
        raise HookRejected('Use plan_research, then task.description={"task_id":"returned ID"}; reuse stable IDs')
    return None, description, ""
