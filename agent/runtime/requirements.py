"""Server-owned, versioned requirements, support edges and gap lifecycle."""

import json
import re
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from agent.runtime.policies import DecisionRejected

DIMENSIONS = ("model", "variant", "region", "channel", "window", "eligibility", "price_kind")


class RequirementSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    anchor: str = Field(
        min_length=1,
        max_length=2000,
        description="Exact REQUIRED span from the original user query, not coordinator-added conditions",
    )
    label: str = Field(min_length=1, max_length=160)
    kind: Literal["fact", "comparison", "recommendation", "unresolved"]
    model: str | None = None
    variant: str | None = None
    region: str | None = None
    channel: str | None = None
    window: str | None = None
    eligibility: str | None = None
    price_kind: str | None = None
    required_tier: list[Literal["official", "authorized", "marketplace_self", "third_party", "media", "unknown"]] = (
        Field(
            default_factory=list,
            description="Source tiers explicitly REQUIRED by the original user; otherwise []. Never required/optional.",
        )
    )


def register(context, task_id, specs, *, owner="task", object_id=None):
    from agent.runtime.review_contract import _required_quote

    if owner not in ("task", "main"):
        raise ValueError("Unknown requirement owner")
    if object_id is not None:
        obj = context.objects.get(object_id)
        if obj is None or obj["query_version"] != context.query_version:
            raise DecisionRejected("Object ID must be issued by the current plan for this task")
    current = [r for r in context.requirements.values() if r["query_version"] == context.query_version]
    staged = []
    for spec in specs:
        if owner == "task" and spec.kind != "fact":
            raise DecisionRejected("Task requirements must be fact obligations; keep synthesis in main_requirements")
        if spec.kind != "unresolved" and any(
            spec.anchor.strip() == obj["user_name"].strip()
            for obj in context.objects.values()
            if obj["query_version"] == context.query_version
        ):
            raise DecisionRejected(
                "An object name is not an answer obligation; put identity verification in investigations"
            )
        if (
            owner == "task"
            and object_id is not None
            and re.search(
                r"比较|对比|横向|值得买|推荐(?!价)|\b(?:compare|comparison|recommend\w*|worth buying)\b",
                spec.anchor,
                re.I,
            )
        ):
            raise DecisionRejected("Comparison/recommendation actions cannot be registered as task facts")
        if not _required_quote(spec.anchor, context.user_query):
            raise DecisionRejected(
                f"Requirement {spec.label!r}: anchor must be an exact required span from user_query; do not rewrite it"
            )
        # Structured conditions must themselves be anchored; do not invent capacity/region/channel.
        for key in DIMENSIONS:
            value = getattr(spec, key)
            if value is not None and (not value.strip() or value not in context.user_query):
                raise DecisionRejected(
                    f"Requirement {spec.label!r}: {key}={value!r} is not an exact user_query span. "
                    "Use an exact original value, or null when unspecified; never infer region/year/price kind. "
                    "required_tier must be [] unless the user explicitly requires a source tier."
                )
        if set(spec.required_tier) - {"official", "authorized", "marketplace_self", "third_party", "media", "unknown"}:
            raise ValueError("Unknown required source tier")
        tier_terms = {
            "official": ("官方", "official"),
            "authorized": ("授权", "authorized"),
            "marketplace_self": ("自营",),
            "third_party": ("第三方", "third-party"),
            "media": ("媒体", "media"),
            "unknown": ("未知", "unknown"),
        }
        if any(
            not any(_required_quote(term, context.user_query) for term in tier_terms[tier])
            for tier in spec.required_tier
        ):
            raise DecisionRejected("Source requirement is not anchored in the original mandatory scope")
        row_object_id = object_id
        if object_id is not None and spec.model not in (None, context.objects[object_id]["user_name"]):
            raise DecisionRejected("A task model must match its registered original object_name or be null")
        if owner == "main" and spec.model is not None:
            matches = [
                obj["object_id"]
                for obj in context.objects.values()
                if obj["query_version"] == context.query_version and obj["user_name"] == spec.model
            ]
            if len(matches) != 1:
                raise DecisionRejected("A named main obligation must refer to one registered original object_name")
            row_object_id = matches[0]
        key = _requirement_key(spec.model_dump(), row_object_id)
        if any(r["required"] and _requirement_key(r, r["object_id"]) == key for r in [*current, *staged]):
            raise DecisionRejected("Duplicate required obligation for the same object and explicit dimensions")
        if (
            owner == "main"
            and spec.kind == "fact"
            and any(r["required"] and r["owner"] == "task" and r["anchor"] == spec.anchor for r in [*current, *staged])
        ):
            raise DecisionRejected("A main fact must not duplicate an existing per-object fact obligation")
        rid = str(uuid4())
        staged.append(
            {
                **spec.model_dump(),
                "rid": rid,
                "parent_rid": spec.anchor,
                "owner": owner,
                "task_id": task_id,
                "object_id": row_object_id,
                "query_version": context.query_version,
                "required": True,
            }
        )
    for row in staged:
        _store_requirement(context, row)
    return staged


def _requirement_key(row, object_id):
    return (
        row["kind"],
        row["anchor"],
        object_id,
        row["model"] if object_id is None else None,
        *(row[key] for key in DIMENSIONS if key != "model"),
        tuple(sorted(set(row["required_tier"]))),
    )


def _store_requirement(context, row):
    rid = row["rid"]
    context.requirements[rid] = row
    context.coverage_rows[rid] = {"status": "unknown", "fact_ids": [], "claim_ids": []}
    if row["owner"] == "task":
        gid = str(uuid4())
        context.gaps[gid] = dict(
            gid=gid,
            rid=rid,
            task_id=row["task_id"],
            reason="plan",
            source_fact_id=None,
            status="open",
            searches=0,
            reads=0,
            no_evidence_streak=0,
            consecutive_technical_failures=0,
            covered_by=[],
            seen_sources=[],
            drop_reason=None,
        )


def attach_support_requirements(context, task_ids):
    """Give research-only tasks auditable work without adding final-answer obligations."""
    mains = [
        r
        for r in context.requirements.values()
        if r["query_version"] == context.query_version and r["owner"] == "main" and r["required"]
    ]
    for task_id in task_ids:
        if task_requirements(context, task_id):
            continue
        obj = context.objects[context.task_scopes[task_id]["object_id"]]
        if not mains:
            raise DecisionRejected("A research-only task needs an original main obligation to support")
        for main in mains:
            row = {
                **main,
                "rid": str(uuid4()),
                "parent_rid": main["rid"],
                "kind": "fact",
                "label": "研究支持（非完成门槛）：" + main["label"][:145],
                "owner": "task",
                "task_id": task_id,
                "object_id": obj["object_id"],
                "required": False,
                "model": obj["user_name"],
            }
            _store_requirement(context, row)


def task_requirements(context, task_id):
    return [
        r
        for r in context.requirements.values()
        if r["task_id"] == task_id and r["query_version"] == context.query_version
    ]


def original_required_clauses(query):
    from agent.runtime.review_contract import required_spans

    return list(dict.fromkeys(query[start:end] for start, end in required_spans(query)))


def _clause_residual(clause, spans):
    remaining = list(clause)
    for start, end in spans:
        remaining[start:end] = " " * (end - start)
    residue = "".join(remaining)
    residue = re.sub(
        r"帮我|帮忙|麻烦|请问|请|我想知道|我想了解|我想|想知道|想了解|一下|以及|并且|和|与|及|并|且|还有|分别|各自|各|的|\b(?:please|and|also|the|of|for)\b",
        "",
        residue,
        flags=re.I,
    )
    return re.sub(r"[\s、:：/\\()（）\[\]【】‘’“”\"'，,。.!！?？;；]+", "", residue)


def ensure_original_requirements(context):
    """Map every original clause to obligations/objects, preserving all unmatched semantics."""
    for clause in original_required_clauses(context.user_query):
        current = [
            r for r in context.requirements.values() if r["query_version"] == context.query_version and r["required"]
        ]
        objects = [obj for obj in context.objects.values() if obj["query_version"] == context.query_version]
        spans, rids, object_ids = [], [], []
        for requirement in current:
            matches = list(re.finditer(re.escape(requirement["anchor"]), clause))
            if matches:
                spans.extend((m.start(), m.end()) for m in matches)
                rids.append(requirement["rid"])
                if requirement["object_id"] is not None:
                    object_ids.append(requirement["object_id"])
        for obj in objects:
            matches = list(re.finditer(re.escape(obj["user_name"]), clause))
            if matches:
                spans.extend((m.start(), m.end()) for m in matches)
                object_ids.append(obj["object_id"])
                rids.extend(r["rid"] for r in current if r["object_id"] == obj["object_id"])
        if not spans or _clause_residual(clause, spans):
            row = register(
                context,
                None,
                [RequirementSpec(kind="unresolved", anchor=clause, label="未承接原始需求：" + clause[:151])],
                owner="main",
            )[0]
            rids.append(row["rid"])
        context.requirement_links[clause] = dict(
            query_version=context.query_version,
            requirement_ids=list(dict.fromkeys(rids)),
            object_ids=list(dict.fromkeys(object_ids)),
        )


def ensure_task(context, task_id, description):
    if task_requirements(context, task_id):
        return
    if context.enforce_plan:
        raise ValueError("Planned task has no registered requirements")
    anchors = (
        [description]
        if description in original_required_clauses(context.user_query)
        else original_required_clauses(context.user_query)
    )
    if not anchors:
        raise ValueError("A task needs an original user requirement")
    register(
        context,
        task_id,
        [RequirementSpec(kind="fact", anchor=anchor, label=anchor[:160]) for anchor in anchors],
        object_id=None,
    )


def fold(rows, expected):
    if not expected or set(rows) != set(expected) or any(rows[r]["status"] == "unknown" for r in expected):
        return "unknown"
    return "covered" if all(rows[r]["status"] == "covered" for r in expected) else "missing"


def recompute_run(context):
    expected = {
        r["rid"] for r in context.requirements.values() if r["query_version"] == context.query_version and r["required"]
    }
    status = fold({r: context.coverage_rows[r] for r in expected if r in context.coverage_rows}, expected)
    context.coverage = {"covered": "complete", "missing": "partial", "unknown": "unknown"}[status]
    return context.coverage


def apply_coverage(context, task_id, rows):
    expected = {r["rid"] for r in task_requirements(context, task_id)}
    if set(rows) != expected:
        raise ValueError("Coverage key set differs from assigned requirements")
    with context.budget_lock:
        context.coverage_rows.update(rows)
        for gap in context.gaps.values():
            if gap["task_id"] != task_id or gap["status"] == "dropped":
                continue
            row = rows[gap["rid"]]
            gap["covered_by"] = list(row["fact_ids"])
            if row["status"] == "covered":
                gap["status"] = "covered"
            elif gap["status"] in ("covered", "searching"):
                gap["status"] = "open"
        recompute_run(context)


def request_description(context, task_id):
    return json.dumps(task_requirements(context, task_id), ensure_ascii=False)


def register_support(context, task_id):
    """Task-description experts support original obligations without adding completion gates."""
    from copy import deepcopy

    current = [
        r for r in context.requirements.values() if r["required"] and r["query_version"] == context.query_version
    ]
    if not current:
        current = register(
            context,
            None,
            [
                RequirementSpec(kind="fact", anchor=anchor, label=anchor[:160])
                for anchor in original_required_clauses(context.user_query)
            ],
            owner="main",
        )
    if not current:
        raise ValueError("Expert work needs original user requirements")
    with context.budget_lock:
        for parent in current:
            row = deepcopy(parent)
            row.update(
                rid=str(uuid4()),
                parent_rid=parent["rid"],
                task_id=task_id,
                owner="task",
                kind="fact",
                required=False,
                label="研究支持（非完成门槛）：" + parent["label"][:145],
            )
            _store_requirement(context, row)
