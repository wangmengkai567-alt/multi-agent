"""One validated review state and its Python/SQLite projections."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

FACTS = ("all_passed", "some_passed", "none_passed")
COVERAGE = ("covered", "missing", "unknown")
PRE_REVIEW = (
    "not_checked",
    "tool_failed",
    "dependency_unavailable",
    "timed_out",
    "check_incomplete",
    "budget_exhausted",
)
LABELS = {
    (facts, coverage): "insufficient"
    if facts == "none_passed"
    else {
        "covered": "passed" if facts == "all_passed" else "partial_passed",
        "missing": "passed_with_gap" if facts == "all_passed" else "partial_passed_with_gap",
        "unknown": "passed_coverage_unknown" if facts == "all_passed" else "partial_coverage_unknown",
    }[coverage]
    for facts in FACTS
    for coverage in COVERAGE
}
DISPLAY_LABELS = {
    "passed": "事实与需求均已核实",
    "partial_passed": "有效事实已保留",
    "passed_with_gap": "事实已核实，仍有缺口",
    "partial_passed_with_gap": "部分事实已核实，仍有缺口",
    "passed_coverage_unknown": "事实已核实，覆盖待确认",
    "partial_coverage_unknown": "部分事实已核实，覆盖待确认",
    "insufficient": "尚无合格事实",
    "not_checked": "尚未逐条审核",
    "check_incomplete": "逐条审核未完成",
    "tool_failed": "工具失败",
    "dependency_unavailable": "依赖不可用",
    "timed_out": "已超时",
    "budget_exhausted": "预算耗尽",
}


class ReviewState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    judged: bool = False
    facts_status: Literal["all_passed", "some_passed", "none_passed"] | None = None
    coverage_status: Literal["covered", "missing", "unknown"] | None = None
    check_status: str = "not_checked"

    @model_validator(mode="after")
    def coherent(self):
        if self.judged:
            if (self.facts_status, self.coverage_status) not in LABELS or self.check_status != LABELS[
                self.facts_status, self.coverage_status
            ]:
                raise ValueError("Review axes and label disagree")
        elif self.facts_status is not None or self.coverage_status is not None or self.check_status not in PRE_REVIEW:
            raise ValueError("Unjudged review must have two null axes and a pre-review label")
        return self

    @classmethod
    def decided(cls, facts, coverage):
        return cls(judged=True, facts_status=facts, coverage_status=coverage, check_status=LABELS[facts, coverage])

    @classmethod
    def from_record(cls, record):
        return cls(
            judged=bool(record["judged"]),
            facts_status=record["facts_status"],
            coverage_status=record["coverage_status"],
            check_status=record["check_status"],
        )


def deliverable(state: ReviewState):
    return state.judged and state.facts_status in ("all_passed", "some_passed")


def publishable(state: ReviewState, summary):
    return (
        deliverable(state)
        and state.coverage_status in ("covered", "missing")
        and summary is not None
        and bool(summary.strip(" "))
    )


def label_sql():
    cases = " ".join(
        "WHEN facts_status='" + f + "' AND coverage_status='" + c + "' THEN '" + v + "'" for (f, c), v in LABELS.items()
    )
    return "CASE " + cases + " END"


def constraints_sql():
    pre = ",".join("'" + v + "'" for v in PRE_REVIEW)
    return f"""CHECK(judged IN (0,1)),
        CHECK((judged=0 AND facts_status IS NULL AND coverage_status IS NULL)
           OR (judged=1 AND facts_status IS NOT NULL AND coverage_status IS NOT NULL)),
        CHECK((judged=0 AND check_status IN ({pre})) OR (judged=1 AND check_status={label_sql()})),
        CHECK(published=0 OR (judged=1 AND facts_status IN ('all_passed','some_passed')
          AND coverage_status IN ('covered','missing') AND summary IS NOT NULL AND length(trim(summary))>0))"""


def result_filter(purpose):
    if purpose not in ("handoff", "published"):
        raise ValueError("Unknown result read purpose")
    sql = "r.judged=1 AND r.facts_status IN ('all_passed','some_passed') AND s.current_result_id=r.result_id AND s.status='completed'"
    return sql + (
        " AND r.published=1 AND r.coverage_status IN ('covered','missing') AND length(trim(r.summary))>0"
        if purpose == "published"
        else ""
    )
