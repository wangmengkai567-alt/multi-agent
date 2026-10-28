"""Frozen v6 result constraints, used only by explicit offline migration."""

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
