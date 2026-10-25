"""Structured delivery claims rendered from the current reviewed fact pool."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.runtime.facts import approved_reports
from agent.runtime.output import cited_urls


class DeliveryItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DirectDraft(DeliveryItem):
    answer: str = Field(min_length=1, max_length=30000)


class DeliveryClaim(DeliveryItem):
    claim_id: str
    kind: Literal["fact", "analysis", "limitation"]
    text: str = Field(max_length=4000)
    fact_ids: list[str]
    analysis_ids: list[str]
    analysis_kind: Literal["comparison", "recommendation", "deduction"] | None
    requirement_ids: list[str]


class DeliveryDraft(DeliveryItem):
    blocks: list[DeliveryClaim] = Field(min_length=1, max_length=60)


def fact_pool(context):
    reports = approved_reports(context)
    reports += [item["report"] for item in context.approved_internal.values() if item.get("report")]
    facts, analyses, sources = {}, {}, {}
    for report in reports:
        facts.update((f["id"], f) for f in report["facts"])
        for index, item in enumerate(report["analysis"]):
            identity = item.get("id") or "analysis:" + ":".join(item["fact_ids"]) + f":{index}"
            analyses[identity] = item
        sources.update((s["source_id"], s) for s in report["sources"])
    return dict(facts=facts, analysis=analyses, sources=sources)


def collection_status(context):
    from agent.runtime.retrieval import evidence_available

    return [
        dict(
            task_id=key,
            label=context.task_scopes.get(key, {}).get("label", key),
            active=task.active,
            check_status=task.state.check_status,
            completed=bool(task.closed_reason),
            evidence_available=any(evidence_available(item) for item in task.evidence),
            failure_reason=task.failure_reason,
        )
        for key, task in context.search_tasks.items()
    ]


def response_format():
    from agent.runtime.context import get_run_context

    context = get_run_context()
    if context is not None and context.task_contract is not None:
        from agent.runtime.contracts import assert_contract, strict_format

        if assert_contract(context).delivery_kind == "direct":
            return strict_format(DirectDraft, "direct_draft")
    schema = DeliveryDraft.model_json_schema()
    if context is not None:
        pool = fact_pool(context)
        for definition, field, values in (
            ("DeliveryClaim", "fact_ids", list(pool["facts"])),
            ("DeliveryClaim", "analysis_ids", list(pool["analysis"])),
            (
                "DeliveryClaim",
                "requirement_ids",
                [
                    r["rid"]
                    for r in context.requirements.values()
                    if r["required"] and r["query_version"] == context.query_version
                ],
            ),
        ):
            node = schema["$defs"][definition]["properties"][field]
            if values:
                node["items"]["enum"] = values
            else:
                node["maxItems"] = 0
    return {"type": "json_schema", "json_schema": {"name": "delivery_draft", "strict": True, "schema": schema}}


def parse_delivery(text, context):
    if context.task_contract is not None:
        from agent.runtime.contracts import assert_contract

        contract = assert_contract(context)
        if contract.delivery_kind == "direct":
            if context.search_tasks or context.approved_internal:
                raise ValueError("A direct task cannot acquire research evidence or change delivery kind")
            direct = DirectDraft.model_validate_json(text)
            if not direct.answer.strip():
                raise ValueError("Direct answer must not be blank")
            context.delivery_draft = {
                "answer": direct.answer,
                "delivery_kind": "direct",
                "material_refs": [m.reference for m in contract.materials],
            }
            return direct.answer
    draft = DeliveryDraft.model_validate_json(text)
    pool = fact_pool(context)
    facts, analyses, sources = pool["facts"], pool["analysis"], pool["sources"]
    if len({b.claim_id for b in draft.blocks}) != len(draft.blocks):
        raise ValueError("Duplicate delivery claim id")
    from agent.runtime.quotes import validate_fact

    evidence = [item for task in context.search_tasks.values() for item in task.evidence]
    evidence.extend(e for item in context.approved_internal.values() for e in item["evidence"])
    used, rendered, validated = set(), [], set()
    for claim in draft.blocks:
        if set(claim.fact_ids) - set(facts) or len(set(claim.fact_ids)) != len(claim.fact_ids):
            raise ValueError("Delivery references unknown/rejected/superseded facts")
        if set(claim.analysis_ids) - set(analyses):
            raise ValueError("Delivery references unknown analysis")
        if claim.kind != "limitation" and (not claim.text.strip() or claim.requirement_ids):
            raise ValueError("Factual/analysis text must be nonempty and cannot carry limitation requirements")
        if claim.kind != "analysis" and claim.analysis_kind is not None:
            raise ValueError("Only analysis blocks may declare analysis_kind")
        if claim.kind == "fact" and (not claim.fact_ids or claim.analysis_ids):
            raise ValueError("Fact claim requires fact support only")
        if claim.kind == "analysis" and (
            claim.analysis_kind is None
            or not claim.fact_ids
            or any(not set(analyses[k]["fact_ids"]).issubset(set(claim.fact_ids)) for k in claim.analysis_ids)
        ):
            raise ValueError("Analysis dependencies do not match support")
        if claim.kind == "limitation":
            if claim.text or claim.fact_ids or claim.analysis_ids or not claim.requirement_ids:
                raise ValueError("Limitation text must be empty; provide requirement_ids only for program rendering")
            if len(set(claim.requirement_ids)) != len(claim.requirement_ids):
                raise ValueError("Duplicate limitation requirement")
            notices = []
            for rid in claim.requirement_ids:
                requirement = context.requirements.get(rid)
                if (
                    requirement is None
                    or not requirement["required"]
                    or requirement["query_version"] != context.query_version
                ):
                    raise ValueError("Limitation references foreign or stale requirement")
                if context.coverage_rows[rid]["status"] == "covered":
                    raise ValueError("Covered requirement cannot be described as unverified")
                subject = (
                    "「" + requirement["model"] + "」的"
                    if requirement["model"] and requirement["model"] != requirement["anchor"]
                    else ""
                )
                notices.append(
                    "关于"
                    + subject
                    + "「"
                    + requirement["anchor"]
                    + "」：本轮尚未获得足够的已审核证据以完整核实该需求。"
                )
            if context.workflow and context.workflow.get("db") == "degraded":
                from agent.runtime.workflows import DEGRADED_NOTICE

                notices.insert(0, DEGRADED_NOTICE)
            claim.text = "\n\n".join(dict.fromkeys(notices))
            if len(claim.text) > 4000:
                raise ValueError("Split limitation requirements into smaller blocks")
        for key in set(claim.fact_ids) - validated:
            validate_fact(facts[key], evidence)
            validated.add(key)
        allowed = {
            sources[ref["source_id"]]["url"]
            for key in claim.fact_ids
            for ref in facts[key]["evidence"]
            if sources[ref["source_id"]].get("url")
        }
        if claim.kind != "limitation" and cited_urls(claim.text) - allowed:
            raise ValueError("Delivery cites a URL outside its supporting facts")
        from agent.runtime.evidence_protocol import source_citation

        selected_sources = {
            ref["source_id"]: sources[ref["source_id"]] for key in claim.fact_ids for ref in facts[key]["evidence"]
        }
        citations = " ".join(
            source_citation(source)
            for source in selected_sources.values()
            if not source["url"] or source["url"] not in claim.text
        )
        rendered.append(claim.text + (" " + citations if citations else ""))
        used.update(claim.fact_ids)
    if (
        context.workflow
        and context.workflow.get("db") == "degraded"
        and not any(block.kind == "limitation" for block in draft.blocks)
    ):
        raise ValueError("Degraded database workflow requires a program-rendered limitation block")
    context.delivery_draft = {
        **draft.model_dump(),
        "used_fact_ids": sorted(used),
        "available_fact_ids": sorted(facts),
        "omitted": [dict(fact_id=fact_id, reason="not_selected") for fact_id in sorted(set(facts) - used)],
    }
    return "\n\n".join(rendered)
