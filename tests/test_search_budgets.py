"""Atomic call budgets survive concurrent admission and checkpoints."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from agent.runtime.checkpoints import restore_job_checkpoint, save_job_checkpoint
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.policies import BudgetExceeded, SearchLimits, load_runtime_policy, reserve_calls


@pytest.fixture
def context(sandbox_dir):
    value = RunContext("budget", "thread", sandbox_dir, policy=load_runtime_policy())
    token = set_run_context(value)
    try:
        yield value
    finally:
        reset_run_context(token)


def test_concurrent_call_admission_cannot_overdraw_a_shared_budget(context):
    def request(_):
        marker = set_run_context(context)
        try:
            reserve_calls("tool")
            return True
        except BudgetExceeded:
            return False
        finally:
            reset_run_context(marker)

    with ThreadPoolExecutor(max_workers=8) as workers:
        accepted = sum(workers.map(request, range(80)))
    assert accepted == context.policy.budgets.tool
    assert context.call_counts["tool"] == accepted


def test_multi_category_reservation_is_all_or_none(context):
    context.call_counts["review"] = context.policy.budgets.review
    before = dict(context.call_counts)
    with pytest.raises(BudgetExceeded):
        reserve_calls("main_model", "review")
    assert context.call_counts == before


@pytest.mark.parametrize("field,value", [("calls_per_run", 5), ("research_calls_per_run", 15)])
def test_run_capacity_covers_one_concurrent_batch(field, value):
    with pytest.raises(ValidationError, match="capacity"):
        SearchLimits(**{field: value})


def test_checkpoint_does_not_refresh_used_budget(context):
    reserve_calls("main_model", "review")
    context.job_checkpoint_path = context.work_dir / "checkpoint.json"
    save_job_checkpoint(context)
    restored = RunContext(
        "resumed",
        "thread",
        context.work_dir,
        policy=context.policy,
        research_scope_id=context.research_scope_id,
        job_checkpoint_path=context.job_checkpoint_path,
    )
    restore_job_checkpoint(restored)
    assert restored.call_counts == context.call_counts
