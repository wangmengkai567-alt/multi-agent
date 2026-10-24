"""Exact fact/requirement judgments; source text never becomes instructions."""

import json
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.runtime.review_state import ReviewState


class ContractViolation(ValueError):
    pass


class DecisionItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Finding(DecisionItem):
    kind: Literal[
        "missing_requirement", "unsupported_claim", "contradicted_claim", "bundled_assertion", "expression_issue"
    ]
    rid: str
    fact_id: str
    reason: str = Field(min_length=1, max_length=1000)


class ObjectMatch(DecisionItem):
    object_id: str | None
    relation: Literal["same_object", "family_applies", "different_variant", "unresolved"]
    reason: str = Field(min_length=1, max_length=1000)


class FactDecision(DecisionItem):
    fact_id: str
    verdict: Literal["passed", "insufficient"]
    reason: str = Field(max_length=1000)
    object_match: ObjectMatch


class AnalysisDecision(DecisionItem):
    index: int
    verdict: Literal["passed", "insufficient"]
    reason: str = Field(max_length=1000)


class CoverageDecision(DecisionItem):
    status: Literal["covered", "partial", "missing", "unknown"]
    fact_ids: list[str]
    claim_ids: list[str]


class ReviewDecision(DecisionItem):
    facts: list[FactDecision]
    analysis: list[AnalysisDecision]
    coverage: dict[str, CoverageDecision]
    findings: list[Finding]


class ClaimDecision(DecisionItem):
    claim_id: str
    verdict: Literal["passed", "insufficient"]
    reason: str = Field(max_length=1000)


class FinalReviewDecision(DecisionItem):
    claims: list[ClaimDecision]
    verdict: Literal["passed", "insufficient"]
    coverage: dict[str, CoverageDecision]
    findings: list[Finding]


# A preference cannot be made mandatory by quoting only the noun after it.
_OPTIONAL = re.compile(
    r"(?:优先|最好)(?:参考|引用|查阅|使用|采用|检索|搜索|补充|考虑|查看)?|"
    r"建议(?:参考|引用|查阅|使用|采用|检索|搜索|补充|考虑|查看)|"
    r"可选|如有|若有|可参考|仅供参考|不用|无需|不需要|不要求|未查到请标注|"
    r"\b(?:prefer\w*|optional|if available|where available|not required)\b",
    re.IGNORECASE,
)
_CLAUSE = re.compile(r"[。！？!?；;\n，,]|\.(?=\s|$)")


def required_spans(source):
    """Keep mandatory prefixes; preference suffixes do not erase the original goal."""
    start = 0
    for boundary in [*_CLAUSE.finditer(source), None]:
        end = boundary.start() if boundary else len(source)
        clause = source[start:end]
        optional = _OPTIONAL.search(clause)
        if optional:
            clause = re.sub(r"(?:并且?|以及|且|\band\b)\s*$", "", clause[: optional.start()], flags=re.I)
        value = clause.strip()
        if value:
            offset = start + clause.index(value)
            yield offset, offset + len(value)
        start = boundary.end() if boundary else len(source)


def _required_quote(quote, source):
    if not quote.strip() or quote not in source:
        return False
    spans = list(required_spans(source))
    return any(
        left <= match.start() and match.end() <= right
        for match in re.finditer(re.escape(quote), source)
        for left, right in spans
    )


@dataclass(frozen=True)
class ReviewContract:
    user_query: str
    delegated_task: str
    draft: str
    evidence: tuple
    requirements: tuple = ()
    report: dict | None = None
    final_answer: bool = False
    objects: tuple = ()

    @classmethod
    def create(
        cls,
        user_query,
        delegated_task,
        draft,
        evidence,
        *,
        requirements=(),
        report=None,
        final_answer=False,
        objects=(),
    ):
        return cls(
            user_query or delegated_task,
            delegated_task,
            draft,
            tuple(evidence),
            tuple(requirements),
            report,
            final_answer,
            tuple(objects),
        )

    def requirement_quotes(self):
        return list(dict.fromkeys(r["anchor"] for r in self.requirements))

    def object_choices(self, fact):
        if not fact["identity_evidence"] or self.report.get("identity_errors", {}).get(fact["id"]):
            return []
        assigned = {r["object_id"] for r in self.requirements if r["object_id"] is not None}
        return [o["object_id"] for o in self.objects if o["object_id"] in assigned]

    def target_objects(self, requirement):
        return (
            {requirement["object_id"]}
            if requirement["object_id"] is not None
            else {obj["object_id"] for obj in self.objects}
        )

    def coverage_candidates(self, facts=None, selected=None, claim_verdicts=None):
        """Narrow scope before semantic judgment, then validate against effective facts."""
        from agent.runtime.context import get_run_context
        from agent.runtime.source_policy import source_satisfies

        facts = {f["id"]: f for f in self.report["facts"]} if facts is None else facts
        if selected is None:
            selected = set(facts) - set(self.report.get("fact_errors", {}))
            if self.final_answer:
                selected &= set(get_run_context().delivery_draft["used_fact_ids"])
        blocks = get_run_context().delivery_draft["blocks"] if self.final_answer else []
        result = {}
        for requirement in self.requirements:
            kind = requirement["kind"]
            targets = self.target_objects(requirement)
            eligible = []
            for key, fact in facts.items():
                if key not in selected:
                    continue
                candidate = fact
                if not self.final_answer and "object_id" not in fact and requirement["object_id"] is not None:
                    if requirement["object_id"] not in self.object_choices(fact):
                        continue
                    # A possible match, not an accepted identity: resolve checks the actual judgment below.
                    candidate = {**fact, "object_id": requirement["object_id"]}
                if kind in ("comparison", "recommendation") and targets and candidate.get("object_id") not in targets:
                    continue
                if source_satisfies(requirement, candidate, self.evidence):
                    eligible.append(key)
            claims = [
                b["claim_id"]
                for b in blocks
                if kind in ("comparison", "recommendation")
                and b["kind"] == "analysis"
                and b["analysis_kind"] == kind
                and b["fact_ids"]
                and set(b["fact_ids"]).issubset(eligible)
                and (claim_verdicts is None or claim_verdicts[b["claim_id"]].verdict == "passed")
            ]
            supported_objects = {
                facts[key].get("object_id")
                for block in blocks
                if block["claim_id"] in claims
                for key in block["fact_ids"]
            }
            can_cover = bool(eligible) if kind == "fact" else bool(claims) and targets.issubset(supported_objects)
            result[requirement["rid"]] = {
                "fact_ids": eligible if kind == "fact" else [],
                "claim_ids": claims,
                "statuses": ["unknown"]
                if kind == "unresolved"
                else [
                    *(["covered"] if can_cover else []),
                    "partial",
                    "missing",
                    "unknown",
                ],
            }
        return result

    def coverage_schema(self):
        from copy import deepcopy

        properties = {}
        for rid, allowed in self.coverage_candidates().items():
            fields = {"status": {"type": "string", "enum": allowed["statuses"]}}
            for field in ("fact_ids", "claim_ids"):
                values = allowed[field]
                fields[field] = {"type": "array", "items": {"type": "string"}}
                if values:
                    fields[field]["items"]["enum"] = values
                    fields[field]["maxItems"] = len(values)
                else:
                    fields[field]["maxItems"] = 0
            cases = []
            if "covered" in allowed["statuses"]:
                complete = deepcopy(fields)
                complete["status"]["enum"] = ["covered"]
                support = "fact_ids" if allowed["fact_ids"] else "claim_ids"
                complete[support]["minItems"] = 1
                cases.append(
                    {
                        "type": "object",
                        "properties": complete,
                        "required": list(complete),
                        "additionalProperties": False,
                    }
                )
            fields["status"]["enum"] = [value for value in allowed["statuses"] if value != "covered"]
            cases.append(
                {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
            )
            properties[rid] = {"anyOf": cases} if len(cases) > 1 else cases[0]
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

    def resolve(self, decision):
        from agent.runtime.context import get_run_context
        from agent.runtime.facts import report_references, resolve_facts
        from agent.runtime.requirements import fold

        expected = {r["rid"] for r in self.requirements}
        rows = {rid: row.model_dump() for rid, row in decision.coverage.items()}
        if set(rows) != expected:
            raise ContractViolation("coverage (missing, duplicate or foreign requirement)")
        if self.report is None:
            raise ContractViolation("report (missing structured candidate)")
        facts = {f["id"]: f for f in self.report["facts"]}
        if len(facts) != len(self.report["facts"]):
            raise ContractViolation("facts (duplicate id)")
        claim_verdicts = None
        if self.final_answer:
            blocks = get_run_context().delivery_draft["blocks"]
            claim_verdicts = {item.claim_id: item for item in decision.claims}
            if len(claim_verdicts) != len(decision.claims) or set(claim_verdicts) != {b["claim_id"] for b in blocks}:
                raise ContractViolation("claims (every delivery block must be judged exactly once)")
            if decision.verdict == "passed" and any(item.verdict != "passed" for item in decision.claims):
                raise ContractViolation("passed final verdict contradicts claim judgments")
            passed, rejected = set(facts), set()
            approved_analysis = self.report.get("analysis", [])
        else:
            judgments = {f.fact_id: f for f in decision.facts}
            if len(judgments) != len(decision.facts) or set(judgments) != set(facts):
                raise ContractViolation("facts (every fact must be judged exactly once)")
            indices = {a.index: a for a in decision.analysis}
            if len(indices) != len(decision.analysis) or set(indices) != set(range(len(self.report["analysis"]))):
                raise ContractViolation("analysis (every analysis must be judged exactly once)")
            for key, fact in facts.items():
                match = judgments[key].object_match
                if match.relation in ("same_object", "family_applies"):
                    if match.object_id not in self.object_choices(fact):
                        raise ContractViolation(
                            f"facts[{key}] identity has no valid source evidence or assigned object"
                        )
                elif match.object_id is not None:
                    raise ContractViolation(
                        f"facts[{key}] unresolved/different variant cannot bind the requested object"
                    )
                facts[key] = {**fact, "object_id": match.object_id, "object_match": match.model_dump()}
            for key, errors in self.report.get("fact_errors", {}).items():
                judgments[key] = judgments[key].model_copy(
                    update={"verdict": "insufficient", "reason": "；".join(errors)}
                )
            passed = {key for key, value in judgments.items() if value.verdict == "passed"}
            rejected = set(facts) - passed
            approved_analysis = [
                a
                for i, a in enumerate(self.report["analysis"])
                if indices[i].verdict == "passed" and set(a["fact_ids"]).issubset(passed)
            ]
        resolved = resolve_facts([facts[key] for key in facts if key in passed])
        selected = {f["id"] for f in resolved["facts"]}
        if self.final_answer:
            selected &= set(get_run_context().delivery_draft["used_fact_ids"])
        valid_analysis = [a for a in approved_analysis if set(a["fact_ids"]).issubset(selected)]
        candidates = self.coverage_candidates(facts, selected, claim_verdicts)
        blocks_by_id = (
            {b["claim_id"]: b for b in get_run_context().delivery_draft["blocks"]} if self.final_answer else {}
        )
        for requirement in self.requirements:
            rid, kind = requirement["rid"], requirement["kind"]
            row, allowed = rows[rid], candidates[rid]
            if row["status"] not in allowed["statuses"]:
                raise ContractViolation(
                    f"coverage[{rid}] status is outside its legal support contract: {allowed['statuses']}"
                )
            for field in ("fact_ids", "claim_ids"):
                if len(row[field]) != len(set(row[field])) or set(row[field]) - set(allowed[field]):
                    raise ContractViolation(f"coverage[{rid}].{field} contains duplicate or ineligible support")
            if row["status"] == "covered" and not (row["fact_ids"] if kind == "fact" else row["claim_ids"]):
                raise ContractViolation(f"coverage[{rid}] covered requires nonempty eligible support")
            if kind in ("comparison", "recommendation"):
                row["fact_ids"] = sorted(
                    {key for identity in row["claim_ids"] for key in blocks_by_id[identity]["fact_ids"]}
                )
                if row["status"] == "covered" and not self.target_objects(requirement).issubset(
                    {facts[key].get("object_id") for key in row["fact_ids"]}
                ):
                    raise ContractViolation(f"coverage[{rid}] synthesis omits a requested object")
        for finding in decision.findings:
            if (
                not self.final_answer
                and finding.kind in ("unsupported_claim", "contradicted_claim", "bundled_assertion")
                and finding.fact_id in passed
            ):
                raise ContractViolation(f"facts[{finding.fact_id}] passed contradicts finding {finding.kind}")
            if (finding.rid and finding.rid not in expected) or (finding.fact_id and finding.fact_id not in facts):
                raise ContractViolation("finding (foreign requirement or fact)")
            if finding.kind == "missing_requirement" and not finding.rid:
                raise ContractViolation("finding (missing requirement id)")
            if (
                finding.kind == "missing_requirement"
                and not next(r for r in self.requirements if r["rid"] == finding.rid)["required"]
            ):
                raise ContractViolation("Research support work cannot become a mandatory missing requirement")
            if finding.kind != "missing_requirement" and not finding.fact_id and not self.final_answer:
                raise ContractViolation("finding (missing fact id)")
        coverage = fold(rows, expected)
        facts_status = "none_passed" if not passed else "all_passed" if not rejected else "some_passed"
        state = ReviewState.decided(facts_status, coverage)
        findings = [f.model_dump() for f in decision.findings]
        issues = [f["reason"] for f in findings]
        mandatory = {r["rid"] for r in self.requirements if r["required"]}
        mandatory_satisfied = not mandatory or fold({key: rows[key] for key in mandatory}, mandatory) == "covered"
        verdict = (
            decision.verdict
            if self.final_answer
            else ("passed" if not rejected and mandatory_satisfied and not findings else "insufficient")
        )
        if verdict == "insufficient" and not issues:
            issues = ["仍有事实未通过或原始需求未完整覆盖"]
        accepted = {
            **resolved,
            "analysis": valid_analysis,
            "gaps": [r["label"] for r in self.requirements if rows[r["rid"]]["status"] != "covered"],
            "sources": self.report.get("sources", []),
        }
        used = {ref["source_id"] for ref in report_references([accepted])}
        accepted["sources"] = [s for s in accepted["sources"] if s["source_id"] in used]
        return dict(
            verdict=verdict,
            issues=issues,
            missing_evidence=accepted["gaps"],
            revision_instruction=(
                "仅修订当前对象，按逐事实判定保留有效内容；只补原始必需需求，不为研究支持项扩展调查。\n"
                + json.dumps(findings, ensure_ascii=False)
            )
            if verdict == "insufficient"
            else "",
            state=state,
            coverage=rows,
            accepted_report=accepted,
            fact_decisions=[] if self.final_answer else [judgments[f["id"]].model_dump() for f in self.report["facts"]],
            claim_decisions=[c.model_dump() for c in decision.claims] if self.final_answer else [],
            findings=findings,
            terminal=True,
        )
