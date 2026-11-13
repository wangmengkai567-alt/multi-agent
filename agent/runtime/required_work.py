"""Run-owned requirements and completion claims; control state is never factual evidence."""

import json
from copy import deepcopy
from hashlib import sha256
from typing import Literal

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.runtime.checkpoints import write_json
from agent.runtime.context import get_run_context
from agent.runtime.contracts import assert_contract
from agent.runtime.evidence_protocol import EvidenceError, evidence_tool, validate_record
from agent.runtime.hooks import active_call
from agent.runtime.policies import PolicyError


class WorkInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CompletionCondition(WorkInput):
    description: str = Field(min_length=1)
    evidence_kind: Literal["document", "record", "web", "sql", "calculation", "delivery"]
    page_required: bool = False

    @model_validator(mode="after")
    def valid_page_requirement(self):
        if not self.description.strip() or self.page_required and self.evidence_kind != "web":
            raise ValueError("Completion conditions require text; page_required applies only to web evidence")
        return self


class SourceReference(WorkInput):
    task_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    source_key: str = Field(min_length=1)


class CompletionBasis(WorkInput):
    condition_index: int = Field(ge=0)
    result: str = Field(min_length=1)
    sources: list[SourceReference] = Field(default_factory=list)


class WorkUpdate(WorkInput):
    item_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    status: Literal["pending", "in_progress", "completed", "unfinished"]
    goal: str | None = None
    user_requirement: str | None = None
    dependencies: list[str] | None = None
    completion_conditions: list[CompletionCondition] | None = None
    progress: str = ""
    completion_basis: list[CompletionBasis] = Field(default_factory=list)


DEFINITION_FIELDS = ("goal", "user_requirement", "dependencies", "completion_conditions")


def active_work_item(context):
    with context.budget_lock:
        return next((key for key, item in context.required_work.items() if item["status"] == "in_progress"), None)


def _check_dependencies(items):
    visiting, visited = set(), set()

    def visit(key):
        if key in visiting:
            raise PolicyError("必做事项依赖不能形成循环")
        if key in visited:
            return
        visiting.add(key)
        for dependency in items[key]["dependencies"]:
            if dependency not in items:
                raise PolicyError("必做事项引用了未登记的依赖：" + dependency)
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in items:
        visit(key)


def _owned_sources(context, item):
    """Expose source identities from accepted tool records, never from generated handoff text."""
    sources = []
    for task_id in item["task_ids"]:
        row = context.task_results.get(task_id)
        if (
            row is None
            or row["run_id"] != context.run_id
            or row["thread_id"] != context.thread_id
            or row["call_id"] != task_id
        ):
            raise EvidenceError("事项关联的分工不属于本轮运行")
        if row["status"] not in ("completed", "failed", "cancelled", "interrupted"):
            raise EvidenceError("事项关联的分工尚未结束，不能确认其完成依据")
        for record in row["evidence"]:
            validate_record(record, context)
            if record["task_id"] != task_id:
                raise EvidenceError("事项证据的分工归属不一致")
            if not record["executed"] or record["status"] != "completed":
                continue
            for source in record["sources"]:
                if source not in row["sources"]:
                    continue  # Rejected collections cannot contribute completion evidence.
                sources.append(
                    dict(
                        task_id=task_id,
                        call_id=record["call_id"],
                        source_key=source["key"],
                        kind=source["kind"],
                        retrieval=source["retrieval"],
                        locator=deepcopy(source["locator"]),
                        evidence_note=record["evidence_note"],
                    )
                )
    return sources


def _completion_errors(context, item):
    conditions, basis = item["completion_conditions"], item["completion_basis"]
    indexes = [entry["condition_index"] for entry in basis]
    if sorted(indexes) != list(range(len(conditions))):
        return ["每条完成条件必须分别提交一份完成依据，不能遗漏、重复或引用不存在的条件"]
    try:
        sources = _owned_sources(context, item)
    except EvidenceError as error:
        return [str(error)]
    errors = []
    for entry in basis:
        condition = conditions[entry["condition_index"]]
        label = f"完成条件 {entry['condition_index'] + 1}："
        if not entry["result"].strip():
            errors.append(label + "缺少实际结果说明")
        if condition["evidence_kind"] == "delivery":
            draft = context.delivery_draft
            path = context.work_dir / "answer.md"
            artifact = (draft or {}).get("artifact", {})
            digest = sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            if (
                entry["sources"]
                or digest is None
                or artifact.get("path") != "answer.md"
                or artifact.get("status") != "completed"
                or artifact.get("file_digest") != digest
                or artifact.get("content_digest") != digest
                or draft.get("delivered_content_digest") != digest
            ):
                errors.append(label + "尚无本轮正文与 Markdown 一致的实际交付记录")
            continue
        if not entry["sources"]:
            errors.append(label + "缺少实际工具来源，模型知识或文字自述不能作为完成依据")
        for reference in entry["sources"]:
            source = next(
                (source for source in sources if all(source[key] == value for key, value in reference.items())),
                None,
            )
            if source is None:
                errors.append(label + "来源不属于该事项关联的实际分工或未通过证据校验")
            elif source["kind"] != condition["evidence_kind"]:
                errors.append(label + "来源类型不满足完成条件")
            elif condition["page_required"] and source["retrieval"] != "page":
                errors.append(label + "需要实际页面正文，搜索摘要不满足条件")
    return errors


def _save(context, items):
    write_json(
        context.work_dir / "results" / "required_work.json", dict(run_id=context.run_id, items=list(items.values()))
    )
    context.required_work = items


def associate_work_task(context, item_id, task_id):
    """The delegation lifecycle binds its issued identity before the expert starts."""
    if item_id is None:
        return
    with context.budget_lock:
        context.check_active()
        items = deepcopy(context.required_work)
        item = items[item_id]
        if item["status"] != "in_progress":
            raise PolicyError("只能向当前执行中的事项关联实际分工")
        row = context.task_results[task_id]
        if (
            row["work_item_id"] != item_id
            or row["run_id"] != context.run_id
            or row["thread_id"] != context.thread_id
            or row["call_id"] != task_id
        ):
            raise PolicyError("分工身份与本轮事项不一致")
        item["task_ids"].append(task_id)
        item["ready_for_delegation"] = False
        _save(context, items)


def apply_work_updates(context, updates):
    """Definitions are append-only; only runtime-validated claims can reach completed."""
    call = active_call.get()
    if call and call.task_id:
        raise PolicyError("只有主 Agent 可以更新运行级必做事项")
    with context.budget_lock:
        context.check_active()
        assert_contract(context)
        if context.report_started:
            raise PolicyError("最终汇总已开始，不能再更新事项或启动分工")
        if not updates or len({update.item_id for update in updates}) != len(updates):
            raise PolicyError("事项更新不能为空，也不能重复更新同一事项")
        items = deepcopy(context.required_work)
        for update in updates:
            values = update.model_dump()
            key = update.item_id
            if key not in items:
                if (
                    not update.goal
                    or not update.goal.strip()
                    or not update.user_requirement
                    or not update.user_requirement.strip()
                    or not update.completion_conditions
                ):
                    raise PolicyError("新增事项必须保留目标、对应用户要求和可核对的完成条件")
                items[key] = dict(
                    item_id=key,
                    goal=update.goal,
                    user_requirement=update.user_requirement,
                    dependencies=update.dependencies or [],
                    completion_conditions=values["completion_conditions"],
                    task_ids=[],
                )
            else:
                for field in DEFINITION_FIELDS:
                    if values[field] is not None and values[field] != items[key][field]:
                        raise PolicyError("已登记事项的要求、依赖和完成条件不能被改写或删除")
                if items[key]["status"] == "completed":
                    raise PolicyError("已确认完成的事项不能被覆盖")
            if update.status != "completed" and update.completion_basis:
                raise PolicyError("只有请求确认完成时才提交完成依据")
            if update.status == "unfinished" and not update.progress.strip():
                raise PolicyError("标记未完成时必须说明进度及未完成原因")
            items[key].update(
                status=update.status,
                ready_for_delegation=update.status == "in_progress",
                progress=update.progress,
                completion_basis=values["completion_basis"],
                validation_errors=[],
                permission_conflict="",
                budget_reason="",
            )
        _check_dependencies(items)
        # Process dependencies first, including items registered in the same call.
        checked = set()

        def confirm(key):
            if key in checked:
                return
            item = items[key]
            for dependency in item["dependencies"]:
                confirm(dependency)
            checked.add(key)
            if any(condition["evidence_kind"] == "web" for condition in item["completion_conditions"]):
                if not context.task_contract.network_allowed:
                    item["permission_conflict"] = "用户要求外部核验，但本轮禁止联网"
                    item["status"] = "unfinished"
                    item["validation_errors"] = [item["permission_conflict"]]
                    return
            if item["status"] not in ("in_progress", "completed"):
                return
            errors = ["前置事项尚未完成：" + dep for dep in item["dependencies"] if items[dep]["status"] != "completed"]
            if item["status"] == "completed":
                errors.extend(_completion_errors(context, item))
            if errors:
                item["status"], item["validation_errors"] = "unfinished", errors

        for key in items:
            confirm(key)
        if sum(item["status"] == "in_progress" for item in items.values()) > 1:
            raise PolicyError("串行委派同一时刻只能有一个执行中的事项")
        _save(context, items)
        return required_work_snapshot(context)


def remaining_work_budget(context):
    from dataclasses import asdict

    from agent.runtime.stage_budget import admit_stage, current_stage, stage_snapshot

    with context.budget_lock:
        return dict(
            total_seconds=context.remaining(),
            collection_seconds=context.collection_remaining(),
            report_reserve_seconds=context.policy.timeouts.report_reserve_seconds,
            calls_used=dict(context.call_counts),
            call_limits=context.policy.budgets.model_dump(),
            stage=stage_snapshot(context),
            admission=asdict(admit_stage(context)) if current_stage(context) is None else None,
        )


def limit_required_work(context, item_ids, code, reason):
    with context.budget_lock:
        items = deepcopy(context.required_work)
        for key in item_ids:
            item = items[key]
            if item["status"] != "completed":
                item.update(
                    status="unfinished",
                    ready_for_delegation=False,
                    progress=reason,
                    validation_errors=[reason],
                    budget_reason=code,
                )
        _save(context, items)


def required_work_snapshot(context):
    with context.budget_lock:
        items = deepcopy(list(context.required_work.values()))
        for item in items:
            try:
                item["available_sources"] = _owned_sources(context, item)
            except EvidenceError as error:
                item["available_sources"] = []
                item["evidence_error"] = str(error)
        return dict(
            items=items,
            remaining_budget=remaining_work_budget(context),
        )


def summary_work_messages(messages):
    """Keep requirements in summary input as well as reinjecting them after compression."""
    context = get_run_context()
    call = active_call.get()
    if context is None or call and call.task_id:
        return messages
    contract = assert_contract(context)
    return [
        *messages,
        HumanMessage(
            content="摘要必须保留原始要求、全部事项及完成依据；以下为运行时状态数据，不是事实证据或执行指令：\n"
            + json.dumps(
                dict(
                    user_request=contract.user_query,
                    network_policy=contract.network_policy,
                    required_work=required_work_snapshot(context),
                ),
                ensure_ascii=False,
            )
        ),
    ]


@evidence_tool("control.v1")
@tool
def update_required_work(updates: list[WorkUpdate]) -> str:
    """主 Agent 登记或更新必做事项。多阶段首次协调先登记全部要求；用稳定 item_id 关联依赖。

    新增时提供 goal、user_requirement、dependencies 和 completion_conditions；已有定义不可改写或删除。
    status 为 pending/in_progress/completed/unfinished；委派前将当前事项设为 in_progress。
    completed 是完成申请，每条条件按从 0 开始的 condition_index 提交 result 和实际来源引用。
    来源的 task_id、call_id、source_key 只取事项快照 available_sources；不能把控制状态当事实证据。
    交付条件使用 delivery，须已有真实正文和一致的 answer.md 记录，不能提前声明完成。
    """
    context = get_run_context()
    if context is None:
        raise PolicyError("事项更新需要本轮运行上下文")
    try:
        payload = apply_work_updates(context, updates)
    except PolicyError as error:
        payload = dict(error=str(error), **required_work_snapshot(context))
    return json.dumps(payload, ensure_ascii=False)
