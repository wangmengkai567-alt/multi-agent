"""Persistent minimum branch capacity and phase-owned delivery reservations."""

from collections import Counter
from copy import deepcopy
from uuid import uuid4


def _counts(row):
    return sum((Counter(values) for values in row.values()), Counter())


def pending_counts(context):
    return sum((_counts(row["stages"]) for row in context.capacity_reservations.values()), Counter())


def unit_seconds(context, kind):
    limits = context.policy.timeouts
    if kind == "tool":
        return limits.tool_seconds
    if kind in ("main_model", "sub_model", "review"):
        return limits.request_seconds
    return 0.0


def seconds_for(context, counts):
    return sum(unit_seconds(context, kind) * count for kind, count in counts.items())


def pending_seconds(context):
    from agent.runtime.reservations import held_counts

    # Invocation holds cover all admitted tasks. Time is committed when an expert
    # starts; unstarted branches and optional repair attempts are not running work.
    seconds = seconds_for(context, held_counts(context))
    delivery = context.capacity_reservations.get("delivery")
    if delivery is not None:
        counts = delivery["stages"]["delivery"]
        reviewed = bool(context.delivery_draft and context.delivery_draft.get("reviewed_content_digest"))
        mandatory = {"tool": counts["tool"]}
        if not reviewed:
            mandatory.update(main_model=min(1, counts["main_model"]), review=min(1, counts["review"]))
        final = seconds_for(context, mandatory)
        seconds += max(final, context.policy.timeouts.finalize_seconds) if context.collecting else final
    return seconds


def _admit(context):
    from agent.runtime.policies import KINDS, ReservationUnavailable
    from agent.runtime.reservations import held_counts

    held = pending_counts(context) + held_counts(context)
    limits = context.policy.budgets
    if sum(context.call_counts.values()) + sum(held[k] for k in KINDS) > limits.total or any(
        context.call_counts[kind] + held[kind] > getattr(limits, kind) for kind in KINDS
    ):
        raise ReservationUnavailable("reserve_exhausted: required branches and delivery do not fit the call budget")
    remaining = context.remaining()
    if remaining is not None and pending_seconds(context) + context.policy.timeouts.shutdown_seconds >= remaining:
        raise ReservationUnavailable("time_exhausted: required branches and delivery do not fit the deadline")


def _branch(context, key, *, tools=1):
    if key in context.capacity_reservations:
        return
    context.capacity_reservations[key] = dict(
        owner=None,
        task_id=None,
        stages={
            "dispatch": {"main_model": 1, "tool": 1},
            "collection": {"sub_model": tools, "tool": tools},
            "completion": {"sub_model": 1, "review": 1},
        },
    )


def initialize(context, contract):
    """Lock all known obligations before the first business model request."""
    if contract.delivery_kind == "clarification":
        return
    with context.budget_lock:
        previous = deepcopy(context.capacity_reservations)
        try:
            context.capacity_reservations["delivery"] = dict(
                owner=None,
                task_id=None,
                stages={
                    "delivery": {
                        "main_model": 2,
                        "review": 2 if contract.delivery_kind == "direct" else 4,
                        "retry": 1 if contract.delivery_kind == "direct" else 3,
                        "tool": {"none": 0, "markdown": 1, "pdf": 2}[contract.file_output],
                    }
                },
            )
            if contract.delivery_kind == "grounded":
                sources = set(contract.required_sources)
                if contract.business_mode == "internal_external":
                    sources.update(("db", "search"))
                if contract.network_allowed:
                    sources.add("search")
                if "search" in sources:
                    context.capacity_reservations["planning"] = dict(
                        owner=None,
                        task_id=None,
                        stages={"plan_create": {"main_model": 1, "tool": 1}, "plan_seal": {"main_model": 1, "tool": 1}},
                    )
                for source in sorted(sources):
                    _branch(context, "source:" + source)
                if contract.materials:
                    _branch(context, "source:document", tools=max(1, len(contract.materials)))
            _admit(context)
        except BaseException:
            context.capacity_reservations = previous
            raise


def admit_plan(context, task_ids):
    """Replace the public-source placeholder atomically, including append operations."""
    with context.budget_lock:
        previous = deepcopy(context.capacity_reservations)
        try:
            placeholder = context.capacity_reservations.get("source:search")
            if placeholder is not None and placeholder["owner"] is None:
                del context.capacity_reservations["source:search"]
            for task_id in task_ids:
                _branch(context, "plan:" + task_id)
            from agent.runtime.policies import ReservationUnavailable
            from agent.runtime.reservations import held_counts
            from agent.runtime.search import SearchAllowance

            limits = context.policy.search
            complex_task = SearchAllowance.complex_task(context)
            run_limit = limits.research_calls_per_run if complex_task else limits.calls_per_run
            task_limit = limits.research_calls_per_task if complex_task else limits.calls_per_task
            groups = {}
            for key, scope in context.task_scopes.items():
                obj = context.objects[scope["object_id"]]
                groups.setdefault(obj["budget_owner_id"], []).append(key)
            pending = 0
            for keys in groups.values():
                used = sum(len(context.search_tasks[key].calls) for key in keys if key in context.search_tasks)
                if not used and any(
                    key not in context.search_tasks or not context.search_tasks[key].closed_reason for key in keys
                ):
                    pending += 1
                if used >= task_limit and set(keys).intersection(task_ids):
                    raise ReservationUnavailable(
                        "reserve_exhausted: appended work inherits exhausted object search capacity"
                    )
            if len(context.search_calls) + held_counts(context)["search"] + pending > run_limit:
                raise ReservationUnavailable("reserve_exhausted: minimum source searches do not fit the run budget")
            _admit(context)
        except BaseException:
            context.capacity_reservations = previous
            raise


def expert_task(context, target, description):
    """Stable work identity; new work retains the same run's cumulative budget."""
    normalized = description.strip()
    with context.budget_lock:
        for task_id, task in context.expert_tasks.items():
            if task["target"] == target and task["description"] == normalized:
                return task_id
        task_id = target + ":" + str(uuid4())
        context.expert_tasks[task_id] = dict(target=target, description=normalized)
        return task_id


def branch_key(context, call):
    planned = "plan:" + str(call.search_task_key)
    if planned in context.capacity_reservations:
        return planned
    for key, row in context.capacity_reservations.items():
        if call.search_task_key and row["task_id"] == call.search_task_key:
            return key
    source = "document" if call.target == "document" else call.source_kind
    key = "source:" + str(source)
    row = context.capacity_reservations.get(key)
    return key if row is not None and row["task_id"] is None else None


def claim(context, call):
    """Transfer report holds to the existing expert ledger without double counting."""
    from agent.runtime.policies import ReservationUnavailable
    from agent.runtime.reservations import hold

    with context.budget_lock:
        key = branch_key(context, call)
        before = deepcopy(context.capacity_reservations)
        reports_before = deepcopy(context.revision_reservations)
        try:
            if key is None:
                key = "task:" + call.search_task_key
                _branch(context, key)
            row = context.capacity_reservations[key]
            previous_owner = row["owner"]
            if previous_owner is not None:
                if not (
                    previous_owner in context.restored_reservation_owners
                    and previous_owner in context.revision_reservations
                    and context.revision_reservations[previous_owner]["task_id"] == call.search_task_key
                ):
                    raise ReservationUnavailable(
                        "This branch already owns its capacity; re-delegation cannot refresh it"
                    )
                restored = context.revision_reservations.pop(previous_owner)
                restored["active"] = False
                context.revision_reservations[call.call_id] = restored
            row["owner"] = call.call_id
            row["task_id"] = call.search_task_key
            row["stages"].pop("completion", None)
            row["stages"]["dispatch"] = {}
            if previous_owner is None:
                hold(context, call.call_id, {"sub_model": 1, "review": 1}, task_id=call.search_task_key)
            _admit(context)
            context.restored_reservation_owners.discard(previous_owner)
        except BaseException:
            context.capacity_reservations = before
            context.revision_reservations = reports_before
            raise


def finish(context, call):
    with context.budget_lock:
        for row in context.capacity_reservations.values():
            if row["owner"] == call.call_id:
                row["stages"] = {}


def validate_restored(context):
    from agent.runtime.context import RunStopped
    from agent.runtime.policies import KINDS, ReservationUnavailable

    try:
        if context.task_contract is not None and context.task_contract.delivery_kind != "clarification":
            if "delivery" not in context.capacity_reservations:
                raise ValueError("Missing delivery reservation")
            contract = context.task_contract
            public_work = (
                contract.network_allowed
                or "search" in contract.required_sources
                or contract.business_mode == "internal_external"
            )
            if public_work and "planning" not in context.capacity_reservations:
                raise ValueError("Missing planning reservation")
            if any("plan:" + key not in context.capacity_reservations for key in context.task_scopes):
                raise ValueError("Missing planned branch reservation")
        for row in context.capacity_reservations.values():
            if set(row) != {"owner", "task_id", "stages"}:
                raise ValueError("Invalid reservation fields")
            if any(value is not None and not isinstance(value, str) for value in (row["owner"], row["task_id"])):
                raise ValueError("Invalid reservation owner or task")
            for stage, counts in row["stages"].items():
                if stage not in ("dispatch", "collection", "completion", "delivery", "plan_create", "plan_seal"):
                    raise ValueError("Invalid reservation phase")
                if set(counts) - set(KINDS) or any(type(v) is not int or v < 0 for v in counts.values()):
                    raise ValueError("Invalid reservation counters")
        identities = set()
        for task_id, task in context.expert_tasks.items():
            if not isinstance(task_id, str) or set(task) != {"target", "description"}:
                raise ValueError("Invalid expert work identity")
            identity = (task["target"], task["description"])
            if not all(isinstance(v, str) and v for v in identity) or identity in identities:
                raise ValueError("Duplicate or invalid expert work identity")
            identities.add(identity)
        for row in context.revision_reservations.values():
            if set(row["remaining"]) - {*KINDS, "search", "read"} or any(
                type(v) is not int or v < 0 for v in row["remaining"].values()
            ):
                raise ValueError("Invalid expert reservation counters")
        _admit(context)
    except (AttributeError, KeyError, TypeError, ValueError, ReservationUnavailable) as exc:
        context.stop("interrupted")
        raise RunStopped("Saved resource reservations failed integrity or admission checks") from exc


def begin_delivery(context):
    """Unused collection allocations no longer compete with terminal delivery."""
    with context.budget_lock:
        context.collecting = False
        for key, row in context.capacity_reservations.items():
            if key != "delivery":
                row["stages"] = {}


def close_blocked(context, task_ids):
    for task_id in task_ids:
        row = context.capacity_reservations.get("plan:" + task_id)
        if row is not None:
            row["stages"] = {}


def eligible(context, category, phase=None):
    """Return only holds owned by the current real execution phase."""
    from agent.runtime.collection import current_collection
    from agent.runtime.delivery_actions import action_authorized
    from agent.runtime.hooks import active_call

    call = active_call.get()
    scope = current_collection()
    delivery = context.capacity_reservations.get("delivery")
    if delivery is not None and scope is None and not (call and call.target):
        permitted = (
            category == "main_model"
            and phase in ("final_generation", "final_revision")
            or category == "review"
            and phase in ("direct_review", "final_review", "review_revision")
            or category == "retry"
            and (not context.collecting or phase == "review_revision")
            or category == "tool"
            and call is not None
            and action_authorized(call)
            and call.actor.rsplit(":", 1)[-1] in ("generate_markdown", "convert_md_to_pdf")
        )
        stage = delivery["stages"]["delivery"]
        if permitted and stage.get(category, 0):
            return stage
    if scope is not None:
        for row in context.capacity_reservations.values():
            stage = row["stages"].get("collection", {})
            if row["owner"] == scope.owner and stage.get(category, 0):
                return stage
    planning = context.capacity_reservations.get("planning")
    if planning is not None and scope is None:
        stage_name = "plan_seal" if context.accepted_plan else "plan_create"
        stage = planning["stages"].get(stage_name, {})
        if category == "main_model" and phase == "coordination" and not (call and call.target):
            if not context.plan_closed and stage.get(category, 0):
                return stage
        if category == "tool" and call is not None and call.actor.rsplit(":", 1)[-1] == "plan_research":
            operation = call.args.get("operation", "initialize")
            valid_operation = operation == ("seal" if context.accepted_plan else "initialize")
            if (
                valid_operation
                and not context.plan_closed
                and call.args.get("expected_version", 0) == context.plan_version
                and stage.get(category, 0)
            ):
                return stage
    if category == "main_model" and phase == "coordination" and not (call and call.target):
        for row in context.capacity_reservations.values():
            stage = row["stages"].get("dispatch", {})
            if row["owner"] is None and stage.get(category, 0):
                return stage
    if category == "tool" and call is not None and call.actor.rsplit(":", 1)[-1] == "task":
        import json

        try:
            task_id = json.loads(call.args.get("description", "")).get("task_id")
        except (ValueError, AttributeError):
            task_id = None
        target = call.args.get("subagent_type")
        definition = context.expert_contract.get(target, {}).get("execution", {})
        if definition.get("input_kind") == "planned_research":
            from agent.runtime.hooks import HookRejected
            from agent.runtime.task_plan import resolve_task

            try:
                task_id, _, _ = resolve_task(context, call.args.get("description", ""))
            except HookRejected:
                return None
        key = "plan:" + str(task_id)
        if key not in context.capacity_reservations:
            target = call.args.get("subagent_type")
            source = context.expert_contract.get(target, {}).get("execution", {}).get("source_kind")
            key = "source:" + ("document" if target == "document" else str(source))
        row = context.capacity_reservations.get(key)
        if row is not None and row["task_id"] is not None and row["task_id"] != task_id:
            row = None
        stage = row["stages"].get("dispatch", {}) if row else {}
        if stage.get(category, 0):
            return stage
    return None


def timeout(context, maximum, *, category=None, phase=None):
    """Apply the configured cap while preserving time for committed later work."""
    from agent.runtime.collection import current_collection
    from agent.runtime.policies import ReservationUnavailable
    from agent.runtime.reservations import current_owner

    with context.budget_lock:
        remaining = context.remaining()
        reserved = pending_seconds(context)
        own = context.revision_reservations.get(current_owner(context))
        protected = eligible(context, category, phase) if category else None
        own_count = own["remaining"].get(category, 0) if own and own["active"] else 0
        if category == "sub_model" and phase not in ("report_generation", "report_revision"):
            own_count = max(0, own_count - 1)
        if own_count:
            protected = own["remaining"]
        delivery = context.capacity_reservations.get("delivery")
        delivery_stage = delivery["stages"]["delivery"] if delivery else None
        if own_count or protected is not None and protected is delivery_stage:
            reserved -= unit_seconds(context, category)
        if remaining is not None:
            shutdown = context.policy.timeouts.shutdown_seconds if reserved else 0
            available = remaining - reserved - shutdown
            if available <= 0:
                scope = current_collection()
                if scope is not None:
                    scope.finish("reserved_time")
                raise ReservationUnavailable("time_exhausted: remaining time belongs to required work and delivery")
            maximum = min(maximum, available)
        return maximum
