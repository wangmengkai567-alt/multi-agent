"""Acceptance labels distinguish recovered attempts, limited output, and complete answers."""

from manual.system_regression import evaluate


def record(kind="complete"):
    return {
        "answer": "TaskGroup cancels other tasks: https://docs.python.org/3.11/library/asyncio-task.html",
        "checks": {
            key: True
            for key in (
                "no_database",
                "renamed_search",
                "no_ecommerce_skill",
                "bounded_searches",
                "task_budgets_recorded",
                "each_task_got_search",
                "final_answer_checked",
            )
        },
        "flow_checks": {"no_timeout": True, "bounded_tasks": True, "final_answer": True},
        "subtasks": [{"check_status": "insufficient"}, {"check_status": "passed"}],
        "final_check": {"status": "passed" if kind == "complete" else "partial", "data": {"answer_kind": kind}},
    }


def test_complete_verified_answer_can_recover_from_earlier_failed_attempt():
    assert evaluate("official_docs", record())["functional_passed"]


def test_safe_partial_answer_is_not_a_complete_success():
    result = evaluate("official_docs", record("partial"))
    assert result["runtime_passed"] and not result["functional_passed"]


def test_unchecked_final_answer_and_unapproved_evidence_cannot_pass():
    data = record()
    data["subtasks"] = [{"check_status": "insufficient"}]
    assert not evaluate("official_docs", data)["functional_passed"]
    data = record()
    data["final_check"] = None
    assert not evaluate("official_docs", data)["functional_passed"]


def test_wrong_routing_and_cancel_sentinel_are_runtime_failures():
    data = record()
    data["checks"]["no_ecommerce_skill"] = False
    assert not evaluate("official_docs", data)["runtime_passed"]
    data = record()
    data["answer"] += " <object object at 0x123>"
    assert not evaluate("official_docs", data)["runtime_passed"]
