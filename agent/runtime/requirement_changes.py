"""Human-only requirement revisions, applied at the root inference boundary."""

from copy import copy, deepcopy

from pydantic import BaseModel, ConfigDict, Field

from agent.runtime.requirements import RequirementSpec


class AddedRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    task_id: str | None
    object_id: str | None
    requirement: RequirementSpec


class RequirementChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(ge=1)
    instruction: str = Field(min_length=1, max_length=16000)
    retain: list[str] = Field(max_length=256)
    withdraw: list[str] = Field(max_length=256)
    additions: list[AddedRequirement] = Field(max_length=64)
    reason: str = Field(min_length=1, max_length=1000)


STATE_FIELDS = (
    "task_contract",
    "query_version",
    "query_history",
    "requirements",
    "requirement_links",
    "coverage_rows",
    "gaps",
    "objects",
    "task_scopes",
    "coverage",
    "plan_closed",
)


def _prepare(context, change):
    from uuid import uuid4

    from agent.runtime.contracts import TaskContract, assert_contract
    from agent.runtime.requirements import (
        _store_requirement,
        drop_gap,
        ensure_original_requirements,
        register,
        task_requirements,
    )

    contract = assert_contract(context)
    if contract.delivery_kind != "grounded":
        raise ValueError("Requirement revisions require an established evidence task")
    if change.expected_version != context.query_version:
        raise ValueError("Requirement version changed")
    from agent.runtime.source_policy import query_domains

    if query_domains(change.instruction) != query_domains(contract.instruction_text):
        raise ValueError("Source-domain changes require a new task contract")
    current = {
        r["rid"] for r in context.requirements.values() if r["query_version"] == context.query_version and r["required"]
    }
    if (
        len(set(change.retain)) != len(change.retain)
        or len(set(change.withdraw)) != len(change.withdraw)
        or set(change.retain) & set(change.withdraw)
        or set(change.retain) | set(change.withdraw) != current
    ):
        raise ValueError("retain/withdraw must partition every current mandatory requirement exactly once")
    if change.instruction == context.task_instruction and not change.withdraw and not change.additions:
        raise ValueError("Requirement update has no change")
    candidate = copy(context)
    for field in STATE_FIELDS:
        setattr(candidate, field, deepcopy(getattr(context, field)))
    candidate.task_contract = TaskContract.model_validate(
        {**contract.model_dump(), "instruction_text": change.instruction, "operation": change.instruction}
    )
    candidate.query_history.append(
        dict(
            version=context.query_version,
            instruction=contract.instruction_text,
            requirements={
                k: deepcopy(v) for k, v in context.requirements.items() if v["query_version"] == context.query_version
            },
            coverage_rows={k: deepcopy(context.coverage_rows[k]) for k in current},
            task_scopes=deepcopy(context.task_scopes),
            reason=change.reason,
            actor="user",
            withdrawn=list(change.withdraw),
        )
    )
    candidate.query_version += 1
    for obj in candidate.objects.values():
        if obj["user_name"] in change.instruction or obj.get("input_material_refs"):
            obj["query_version"] = candidate.query_version
    for gap in candidate.gaps.values():
        if gap["status"] != "dropped":
            drop_gap(candidate, gap, "requirement_changed", change.reason)
    candidate.requirement_links = {}
    mapping = {}
    for rid in change.retain:
        old = context.requirements[rid]
        row = register(
            candidate,
            old["task_id"],
            [RequirementSpec.model_validate({key: old[key] for key in RequirementSpec.model_fields})],
            owner=old["owner"],
            object_id=old["object_id"],
        )[0]
        row["parent_rid"] = rid
        mapping[rid] = row["rid"]
    for addition in change.additions:
        if addition.task_id is not None:
            scope = candidate.task_scopes.get(addition.task_id)
            if scope is None or scope["object_id"] != addition.object_id:
                raise ValueError("New task requirements must use an existing owned task/object")
        register(
            candidate,
            addition.task_id,
            [addition.requirement],
            owner="main" if addition.task_id is None else "task",
            object_id=addition.object_id,
        )
    if not any(
        r["required"] and r["query_version"] == candidate.query_version for r in candidate.requirements.values()
    ):
        raise ValueError("An empty goal requires cancellation, not successful withdrawal")
    ensure_original_requirements(candidate)
    # Supporting rows remain work, never become extra completion gates.
    for old in context.requirements.values():
        if old["query_version"] == context.query_version and not old["required"] and old["parent_rid"] in mapping:
            row = {
                **deepcopy(old),
                "rid": str(uuid4()),
                "parent_rid": mapping[old["parent_rid"]],
                "query_version": candidate.query_version,
            }
            _store_requirement(candidate, row)
    for key, scope in candidate.task_scopes.items():
        scope["requirement_ids"] = [r["rid"] for r in task_requirements(candidate, key)]
    candidate.coverage = "unknown"
    if candidate.accepted_plan:
        candidate.plan_closed = False
    return candidate


def stage_change(context, change):
    """Called only by the authenticated user API; never exposed as a model tool."""
    from agent.runtime.checkpoints import save_job_checkpoint

    with context.budget_lock:
        context.check_active()
        if not context.collecting or context.pending_requirement_change is not None:
            raise ValueError("Run is finalizing or already has a pending requirement change")
        _prepare(context, change)
        context.pending_requirement_change = change.model_dump()
        from agent.runtime.checkpoints import write_state_json

        write_state_json(context.work_dir / "requirement-state.json", requirement_snapshot(context))
        save_job_checkpoint(context)
    return dict(status="pending", query_version=context.query_version, effective_at="next_root_request")


def apply_pending(context):
    from agent.runtime.checkpoints import save_job_checkpoint, write_state_json
    from agent.runtime.events import make_event
    from agent.runtime.review_state import ReviewState

    with context.budget_lock:
        if context.pending_requirement_change is None:
            return False
        context.check_active()
        if any(t.active for t in context.search_tasks.values()):
            raise ValueError("Cannot revise requirements while expert work is active")
        change = RequirementChange.model_validate(context.pending_requirement_change)
        candidate = _prepare(context, change)
        for field in STATE_FIELDS:
            setattr(context, field, getattr(candidate, field))
        # Preserve every cumulative call, attempt, reservation and evidence identity.
        for task in context.search_tasks.values():
            if task.state.judged:
                task.state = ReviewState.decided(task.state.facts_status, "unknown")
        for item in context.approved_internal.values():
            state = ReviewState.model_validate(item["state"])
            if state.judged:
                item["state"] = ReviewState.decided(state.facts_status, "unknown").model_dump()
        context.review_cache.clear()
        context.delivery_draft = None
        context.pending_requirement_change = None
        context.plan_version += 1
        write_state_json(context.work_dir / "requirement-state.json", requirement_snapshot(context))
        save_job_checkpoint(context)
        if context.event_sink:
            context.event_sink.submit(
                make_event(
                    context.thread_id,
                    context.run_id,
                    "requirements_changed",
                    "用户需求版本已更新，覆盖待重新审核",
                    dict(query_version=context.query_version, withdrawn=change.withdraw, retained=change.retain),
                    status="unknown",
                    call_id=context.run_id,
                )
            )
    return True


def requirement_snapshot(context):
    return deepcopy(
        dict(
            query_version=context.query_version,
            instruction=context.task_instruction,
            requirements=context.requirements,
            coverage_rows=context.coverage_rows,
            gaps=context.gaps,
            history=context.query_history,
            pending=context.pending_requirement_change,
        )
    )


def validate_versions(context):
    """Check only the current checkpoint protocol; no legacy migration path."""
    from agent.runtime.evidence_protocol import fingerprint
    from agent.runtime.review_contract import _required_quote

    if type(context.query_version) is not int or context.query_version != len(context.query_history) + 1:
        raise ValueError("Requirement version history is not contiguous")
    for version, entry in enumerate(context.query_history, 1):
        if entry["version"] != version or entry["actor"] != "user" or not entry["instruction"].strip():
            raise ValueError("Invalid requirement history")
    instructions = {entry["version"]: entry["instruction"] for entry in context.query_history}
    instructions[context.query_version] = context.task_instruction
    for rid, requirement in context.requirements.items():
        if (
            rid != requirement["rid"]
            or requirement["query_version"] not in instructions
            or not _required_quote(requirement["anchor"], instructions[requirement["query_version"]])
        ):
            raise ValueError("Requirement lost its original version/anchor")
        if rid not in context.coverage_rows or context.coverage_rows[rid]["status"] not in (
            "covered",
            "partial",
            "missing",
            "unknown",
        ):
            raise ValueError("Requirement has no valid coverage row")
    for gap in context.gaps.values():
        requirement = context.requirements[gap["rid"]]
        if gap["task_id"] != requirement["task_id"] or gap["reason"] not in (
            "plan",
            "review_missing",
            "fact_rejected",
            "claim",
        ):
            raise ValueError("Gap ownership/reason mismatch")
        if gap["status"] not in ("open", "searching", "covered", "exhausted", "blocked", "dropped"):
            raise ValueError("Unknown gap status")
        if gap["status"] == "dropped" and (
            gap["drop_reason"] not in ("requirement_changed", "claim_resolved", "superseded")
            or not gap.get("drop_detail")
        ):
            raise ValueError("Dropped gap has no auditable cause")
        if requirement["query_version"] != context.query_version and gap["status"] != "dropped":
            raise ValueError("Historical gap is still active")
        if gap["reason"] == "claim" and (
            not gap["source_fact_id"]
            or not gap.get("source_assertion")
            or not gap.get("source_fact_snapshot")
            or gap["source_fact_version"] != fingerprint(gap["source_fact_snapshot"])
            or gap["source_assertion_version"] != fingerprint(gap["source_assertion"])
        ):
            raise ValueError("Claim gap lost or changed its source assertion")
    if context.pending_requirement_change is not None:
        _prepare(context, RequirementChange.model_validate(context.pending_requirement_change))
