"""Revision holds protect unused capacity without counting it as consumption."""

from collections import Counter
from copy import deepcopy


def current_owner(context):
    from agent.runtime.hooks import active_call

    call = active_call.get()
    if call is None:
        return None
    if call.call_id in context.revision_reservations:
        return call.call_id
    return call.parent_call_id if call.parent_call_id in context.revision_reservations else None


def held_counts(context):
    counts = Counter()
    for row in context.revision_reservations.values():
        counts.update(row["remaining"])
    return counts


def hold(context, owner, amounts, *, activate=False, task_id=None):
    from agent.runtime.policies import KINDS, ReservationUnavailable

    with context.budget_lock:
        context.check_active()
        previous_owner = None
        if owner not in context.revision_reservations and context.resume_source_run_id and task_id is not None:
            candidates = [
                identity
                for identity, saved in context.revision_reservations.items()
                if saved["task_id"] == task_id and identity in context.restored_reservation_owners
            ]
            if len(candidates) > 1:
                from agent.runtime.context import RunStopped

                context.stop("interrupted")
                raise RunStopped("Multiple restored reservation owners for one task")
            previous_owner = candidates[0] if candidates else None
        row = deepcopy(
            context.revision_reservations[previous_owner]
            if previous_owner
            else context.revision_reservations.get(owner, {"remaining": {}, "active": False})
        )
        before = Counter(row["remaining"])
        remaining = Counter(row["remaining"])
        for kind, amount in amounts.items():
            remaining[kind] = max(remaining[kind], amount)
        after = remaining
        add = {kind: max(0, amount - before[kind]) for kind, amount in after.items()}
        held = held_counts(context)
        if task_id is not None:
            row["task_id"] = task_id
        if add.get("search") or add.get("read"):
            from agent.runtime.search import SearchAllowance

            allowance = SearchAllowance(context, row["task_id"])
            task_held = {
                kind: sum(
                    r["remaining"].get(kind, 0)
                    for r in context.revision_reservations.values()
                    if r.get("task_id") in allowance.group_tasks()
                )
                for kind in ("search", "read")
            }
            if (
                len(context.search_calls) + held["search"] + add.get("search", 0) + allowance.minimum_run_reserve()
                > allowance.run_limit()
            ):
                raise ReservationUnavailable("reserve_exhausted: run search capacity")
            if (
                allowance.used("search") + task_held["search"] + add.get("search", 0)
                > allowance.initial_limit() + context.policy.search.refinement_calls_per_task
            ):
                raise ReservationUnavailable("reserve_exhausted: task search capacity")
            if (
                allowance.used("read") + task_held["read"] + add.get("read", 0)
                > context.policy.search.page_reads_per_task
            ):
                raise ReservationUnavailable("reserve_exhausted: task read capacity")
        from agent.runtime.capacity import pending_counts

        held.update(pending_counts(context))
        kinds = context.policy.budgets
        if (
            sum(context.call_counts.values())
            + sum(v for k, v in held.items() if k in KINDS)
            + sum(v for k, v in add.items() if k in KINDS)
            > kinds.total
        ):
            raise ReservationUnavailable("reserve_exhausted: revision total capacity unavailable")
        for key, value in add.items():
            if key in KINDS and context.call_counts[key] + held[key] + value > getattr(kinds, key):
                raise ReservationUnavailable("reserve_exhausted: " + key)
        row["remaining"] = dict(remaining)
        row["active"] = activate
        if previous_owner:
            row["recovered_from"] = [*row.get("recovered_from", []), previous_owner]
            del context.revision_reservations[previous_owner]
            context.restored_reservation_owners.discard(previous_owner)
        context.revision_reservations[owner] = row


def activate_report(context, owner):
    """Initial collection cannot spend the report/check capacity held for this child."""
    with context.budget_lock:
        row = context.revision_reservations.get(owner)
        if row:
            row["active"] = True


def release(context, owner):
    with context.budget_lock:
        context.revision_reservations.pop(owner, None)


def resource_available(context, allowance, kind):
    with context.budget_lock:
        owner = current_owner(context)
        own = context.revision_reservations.get(owner)
        exempt = own["remaining"].get(kind, 0) if own and own["active"] else 0
        held = held_counts(context)[kind] - exempt
        task_held = (
            sum(
                r["remaining"].get(kind, 0)
                for r in context.revision_reservations.values()
                if r.get("task_id") in allowance.group_tasks()
            )
            - exempt
        )
        if kind == "search":
            return (
                len(context.search_calls) + held + allowance.minimum_run_reserve() < allowance.run_limit()
                and allowance.used("search") + task_held < allowance.limit()
            )
        return allowance.used("read") + task_held < context.policy.search.page_reads_per_task
