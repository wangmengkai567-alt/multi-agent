"""Single research entry: task-local decisions, shared requests and per-attempt accounting."""

import copy
import json
import re
from contextvars import ContextVar
from dataclasses import replace
from threading import Event
from time import monotonic
from typing import Literal

from langchain_core.tools import tool
from requests import Timeout as RequestsTimeout
from requests.adapters import HTTPAdapter
from tavily.errors import TimeoutError as TavilyTimeoutError

from agent.runtime.context import get_run_context
from agent.runtime.errors import CollectionFinished, DependencyUnavailable, ExecutionTerminated
from agent.runtime.evidence_protocol import evidence_tool
from agent.runtime.hooks import active_call
from agent.runtime.policies import (
    MissingConfiguration,
    PolicyError,
    SearchLimitReached,
    classify_tool_error,
    reserve_calls,
)
from agent.runtime.retrieval import SearchHistory, source_key
from agent.runtime.search import search_allowance

provider_attempt = ContextVar("provider_attempt", default=None)


def audit_transport(client):
    session = getattr(client, "session", None)
    if session is None or not hasattr(session, "mount"):
        raise MissingConfiguration("Provider has no auditable HTTP transport")

    class AuditedAdapter(HTTPAdapter):
        def send(self, request, **kwargs):
            metadata = provider_attempt.get()
            if metadata is None:
                raise PolicyError("Research HTTP request has no reserved attempt")
            context, key = metadata
            with context.budget_lock:
                context.check_active()
                row = context.attempt_ledger[key]
                if row["status"] != "reserved":
                    raise PolicyError("Duplicate HTTP send for one provider attempt")
                row.update(status="spent", sent_at=monotonic())
                context.outbound.append(
                    dict(
                        run_id=context.run_id,
                        kind=row["kind"],
                        call_id=row["call_id"],
                        attempt=row["attempt"],
                        send_seq=len(context.outbound) + 1,
                        request_key=row["request_key"],
                    )
                )
                audit_path = context.work_dir / "transport.jsonl"
                audit_path.parent.mkdir(parents=True, exist_ok=True)
                with audit_path.open("ab") as stream:
                    stream.write((json.dumps(context.outbound[-1], ensure_ascii=False) + "\r\n").encode("utf-8"))
                    stream.flush()
                _checkpoint(context)
            return super().send(request, **kwargs)

    for scheme in ("http://", "https://"):
        previous = session.get_adapter(scheme)
        session.mount(scheme, AuditedAdapter(max_retries=0))
        previous.close()


def _checkpoint(context):
    from agent.runtime.checkpoints import save_job_checkpoint, write_json

    if context.work_dir.is_dir():
        write_json(
            context.work_dir / "research-ledger.json",
            dict(attempts=context.attempt_ledger, requests=context.research_cache, outbound=context.outbound),
        )
    save_job_checkpoint(context)


def available_actions(context, allowance):
    task = allowance.task
    gaps = [
        g for g in context.gaps.values() if g["task_id"] == allowance.task_key and g["status"] in ("open", "searching")
    ]
    sources = SearchHistory(task.evidence).sources
    actions = ["report"]
    from agent.runtime.collection import current_collection

    collection = current_collection()
    remaining = collection.remaining() if collection is not None else context.remaining()
    if not task.finalize_only and (remaining is None or remaining > 0) and gaps:
        if not allowance.exhausted():
            actions.append("search")
        if allowance.can_read_page() and sources:
            actions.append("read")
    return dict(
        allowed_actions=actions,
        open_gaps=[
            {k: g[k] for k in ("gid", "rid", "status")} | {"label": context.requirements[g["rid"]]["label"]}
            for g in gaps
        ],
        readable_sources=[
            dict(source_id=key, title=row.get("title", "")[:160], url=row["url"]) for key, row in sources.items()
        ],
    )


def _reserve(context, allowance, call, kind, attempt, request_key):
    key = f"{kind}:{call.call_id}:{attempt}"
    with context.budget_lock:
        context.check_active()
        if key in context.attempt_ledger:
            raise PolicyError("Attempt identity already used")
        if kind == "search" and allowance.exhausted() or kind == "read" and not allowance.can_read_page():
            raise SearchLimitReached("本任务研究额度已用完")
        owner = call.parent_call_id or context.run_id
        turn = context.tool_turns.get((owner, call.call_id), owner)
        previous = any(
            r["call_id"] == call.call_id and r["status"] != "released" for r in context.attempt_ledger.values()
        )
        if not previous and context.turn_counts.get(turn, 0) >= context.policy.budgets.tools_per_turn:
            from agent.runtime.collection import current_collection

            collection = current_collection()
            if collection is not None:
                collection.finish("turn_tool_limit")
            raise PolicyError("单次模型回合工具调用次数已达到上限")
        categories = ["tool", *(["retry"] if attempt else [])]
        from agent.runtime.reservations import current_owner

        hold_owner = current_owner(context)
        own = context.revision_reservations.get(hold_owner)
        before = dict(own["remaining"]) if own and own["active"] else {}
        from agent.runtime.capacity import eligible
        from agent.runtime.policies import request_timeout

        protected = eligible(context, "tool")
        protected_key = (
            next(
                (
                    key
                    for key, row in context.capacity_reservations.items()
                    if row["stages"].get("collection") is protected
                ),
                None,
            )
            if protected is not None
            else None
        )
        protected_before = protected.get("tool", 0) if protected is not None else 0

        call.workflow_data["admitted_timeout"] = request_timeout(context.policy.search.timeout_seconds, category="tool")
        reserve_calls(*categories)
        if own and own["active"] and own["remaining"].get(kind, 0):
            own["remaining"][kind] -= 1
        hold_consumed = {k: v - own["remaining"].get(k, 0) for k, v in before.items()} if before else {}
        token = f"{call.call_id}:{attempt}"
        if kind == "search":
            allowance.task.calls.add(token)
            context.search_calls.add((allowance.task_key, token))
        else:
            allowance.task.page_calls.add(token)
        if not previous:
            context.turn_counts[turn] = context.turn_counts.get(turn, 0) + 1
        context.attempt_ledger[key] = dict(
            run_id=context.run_id,
            task_id=allowance.task_key,
            kind=kind,
            call_id=call.call_id,
            attempt=attempt,
            request_key=request_key,
            status="reserved",
            categories=categories,
            turn=turn,
            sent_at=None,
            hold_owner=hold_owner,
            hold_consumed=hold_consumed,
            capacity_owner=protected_key,
            capacity_consumed=protected_before - protected.get("tool", 0) if protected is not None else 0,
        )
        _checkpoint(context)
    return key


def _release(context, allowance, key):
    with context.budget_lock:
        row = context.attempt_ledger[key]
        if row["status"] != "reserved":
            return
        row["status"] = "released"
        for kind in row["categories"]:
            context.call_counts[kind] -= 1
        own = context.revision_reservations.get(row.get("hold_owner"))
        if own:
            for kind, amount in row["hold_consumed"].items():
                own["remaining"][kind] = own["remaining"].get(kind, 0) + amount
        capacity = context.capacity_reservations.get(row["capacity_owner"])
        if capacity is not None and "collection" in capacity["stages"]:
            stage = capacity["stages"]["collection"]
            stage["tool"] = stage.get("tool", 0) + row["capacity_consumed"]
        token = f"{row['call_id']}:{row['attempt']}"
        if row["kind"] == "search":
            allowance.task.calls.discard(token)
            context.search_calls.discard((allowance.task_key, token))
        else:
            allowance.task.page_calls.discard(token)
        if not any(
            r["call_id"] == row["call_id"] and r["status"] in ("reserved", "spent")
            for r in context.attempt_ledger.values()
        ):
            context.turn_counts[row["turn"]] = max(0, context.turn_counts[row["turn"]] - 1)
        _checkpoint(context)


def _evidence(
    context,
    allowance,
    call,
    kind,
    attempt,
    params,
    *,
    content=None,
    status="completed",
    error=None,
    cache=None,
    gap=None,
):
    from agent.reflection import tool_evidence
    from agent.runtime.checkpoints import write_json
    from agent.runtime.events import redact_text
    from agent.runtime.evidence_protocol import make_record

    evidence_call = replace(
        call,
        actor=call.actor.rsplit(":", 1)[0] + (":internet_search" if kind == "search" else ":read_web_page"),
        evidence_adapter="web-search.v1" if kind == "search" else "web-page.v1",
        attempt=attempt,
        search_task_key=allowance.task_key,
        executed=cache is None
        and context.attempt_ledger.get(f"{kind}:{call.call_id}:{attempt}", {}).get("status") == "spent",
        status=status,
    )
    item = make_record(evidence_call, content, content, cache=cache, parameters=copy.deepcopy(params))
    item.update(
        via="research_step",
        error_category="decision_rejected" if status == "rejected" else classify_tool_error(error) if error else None,
        error=redact_text(str(error)) if error else None,
        model_intent=dict(call.args),
        gap_id=gap["gid"] if gap else "",
        rid=gap["rid"] if gap else "",
    )
    if content is not None:
        from agent.runtime.source_policy import source_assessment

        requirement = context.requirements[gap["rid"]]
        item["source_assessments"] = [
            {"url": row.get("url", ""), **source_assessment(requirement, row, context)}
            for row in (*content.get("results", []), *content.get("scope_rejected_results", []))
            if isinstance(row, dict)
        ]
    collector = tool_evidence.get()
    if collector is not None:
        collector.append(item)
        allowance.task.evidence = list(collector)
    else:
        allowance.task.evidence.append(item)
    file_name = re.sub(r"[^A-Za-z0-9_-]", "_", call.call_id) + f"-{attempt}.json"
    write_json(context.work_dir / "results/tools" / file_name, item)
    _checkpoint(context)
    return item


def _progress(context, allowance, gap, payload, *, technical=False, logical=False, kind="search"):
    from agent.runtime.evidence_protocol import web_result_error
    from agent.runtime.source_policy import page_tier

    with context.budget_lock:
        if logical:
            gap["searches" if kind == "search" else "reads"] += 1
        if technical:
            gap["consecutive_technical_failures"] += 1
            gap["status"] = "blocked" if gap["consecutive_technical_failures"] >= 2 else "open"
            return
        gap["consecutive_technical_failures"] = 0
        versions = {
            source_key(row["url"]) + ":" + page_tier(row)["content_version"]
            for row in payload.get("results", [])
            if not web_result_error(row, "page" if kind == "read" else "search")
        }
        known = set(gap["seen_sources"])
        if versions - known:
            gap["no_evidence_streak"] = 0
        elif logical:
            gap["no_evidence_streak"] += 1
        task_known = {
            version
            for item in context.gaps.values()
            if item["task_id"] == allowance.task_key
            for version in item["seen_sources"]
        }
        if versions - task_known:
            allowance.task.evidence_version += 1
        gap["seen_sources"] = sorted(known | versions)
        gap["status"] = "exhausted" if gap["no_evidence_streak"] >= 2 else "open"


def _perform(action, gap_id, query, source_id, reason):
    from agent.runtime.evidence_protocol import web_result_error, web_results
    from agent.runtime.recovery import http_status, retry_after
    from agent.runtime.source_policy import task_domains
    from tools.tavily_tool import compact_search_response, internet_search, read_web_page

    context, allowance, call = get_run_context(), search_allowance.get(), active_call.get()
    if context is None or allowance is None or call is None:
        raise PolicyError("Research requires an owned task and call")
    context.check_active()
    if len(reason) > 1000:
        raise PolicyError("reason exceeds limit")
    if action == "report":
        if gap_id or query or source_id:
            raise PolicyError("report forbids gap_id/query/source_id")
        allowance.task.finalize_only = True
        return json.dumps(dict(status="report", message="资料收集完成，保留未核实缺口"), ensure_ascii=False)
    gap = context.gaps.get(gap_id)
    if gap is None or gap["task_id"] != allowance.task_key or gap["status"] not in ("open", "searching"):
        raise PolicyError("gap_id is not an open gap owned by this task")
    if action == "search":
        if not query.strip() or source_id:
            raise PolicyError("search requires query and forbids source_id")
        params = dict(
            query=query,
            topic="general",
            max_results=3,
            include_raw_content=False,
            include_domains=task_domains(context, allowance.task_key) or None,
            search_depth="advanced" if allowance.task.refinement_open else "basic",
        )
        provider, kind = internet_search.func, "search"
    else:
        if query or not source_id:
            raise PolicyError("read requires source_id and forbids query")
        source = SearchHistory(allowance.task.evidence).sources.get(source_id)
        if source is None:
            raise PolicyError("source_id is outside this task's source catalog")
        from agent.runtime.source_policy import within_task_domains

        if not within_task_domains(context, allowance.task_key, source["url"]):
            raise PolicyError("Source is outside the task's domain restriction")
        requirement = context.requirements[gap["rid"]]
        focus = (requirement["label"] + "；" + requirement["anchor"])[:2000]
        params = dict(url=source["url"], query=focus)
        provider, kind = read_web_page.func, "read"
    key_params = (
        params
        if kind == "search"
        else {**params, "extract_depth": "advanced", "chunks_per_source": 5, "format": "text"}
    )
    request_key = json.dumps(
        (context.research_scope_id, "tavily", kind, key_params),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    owner = False
    with context.budget_lock:
        cached = context.research_cache.get(request_key)
        if cached is None:
            if action not in available_actions(context, allowance)["allowed_actions"]:
                raise SearchLimitReached("当前动作不在 allowed_actions 中")
            context.research_cache[request_key] = dict(
                status="in_flight", call_id=call.call_id, attempt=0, payload=None
            )
            context.research_waiters[request_key] = Event()
            owner = True
    if not owner:
        while True:
            context.check_active()
            with context.budget_lock:
                cached = copy.deepcopy(context.research_cache[request_key])
            if cached["status"] != "in_flight":
                break
            context.research_waiters[request_key].wait(0.1)
        if cached["status"] != "served":
            raise PolicyError(
                "duplicate_empty"
                if cached["status"] == "empty"
                else "request_failed: prior owner exhausted bounded retries"
            )
        _evidence(context, allowance, call, kind, 0, params, content=cached["payload"], cache=cached, gap=gap)
        _progress(context, allowance, gap, cached["payload"], kind=kind)
        compact = json.loads(compact_search_response(cached["payload"], context.policy.search, params["query"]))
        compact.update(cache_hit=True, origin_call_id=cached["call_id"], budget_spent=0)
        return json.dumps(compact, ensure_ascii=False)
    gap["status"] = "searching"
    try:
        for attempt in range(context.policy.retry.max_retries + 1):
            ledger_key = None
            try:
                import os

                from agent.runtime.scheduler import account_group
                from agent.runtime.source_policy import within_task_domains

                group = account_group("tavily", "https://api.tavily.com", os.getenv("TAVILY_API_KEY", ""))
                with context.scheduler.provider(context, "search", group):
                    ledger_key = _reserve(context, allowance, call, kind, attempt, request_key)
                    token = provider_attempt.set((context, ledger_key))
                    try:
                        content, payload = provider(**params)
                    finally:
                        provider_attempt.reset(token)
                accepted, rejected, invalid = [], [], []
                for index, row in enumerate(web_results(payload)):
                    error = web_result_error(row, "page" if kind == "read" else "search")
                    if error:
                        invalid.append(dict(result_index=index, error=error, original_result=row))
                        continue
                    (accepted if within_task_domains(context, allowance.task_key, row["url"]) else rejected).append(row)
                payload = {
                    **payload,
                    "results": accepted,
                    "scope_rejected_results": rejected,
                    "invalid_results": invalid,
                }
                content = compact_search_response(payload, context.policy.search, params["query"])
                context.check_active()
                with context.budget_lock:
                    if context.attempt_ledger[ledger_key]["status"] != "spent":
                        raise PolicyError("Provider returned without an audited send")
                    context.research_cache[request_key] = dict(
                        status="served" if payload.get("results") else "empty",
                        call_id=call.call_id,
                        attempt=attempt,
                        payload=copy.deepcopy(payload),
                    )
                _evidence(context, allowance, call, kind, attempt, params, content=payload, gap=gap)
                _progress(context, allowance, gap, payload, logical=attempt == 0, kind=kind)
                allowance.task.invalid_decisions = 0
                _checkpoint(context)
                return content
            except BaseException as error:
                if ledger_key:
                    _release(context, allowance, ledger_key)
                    row = context.attempt_ledger[ledger_key]
                    row["outcome"] = (
                        "uncertain"
                        if isinstance(error, TimeoutError | RequestsTimeout | TavilyTimeoutError) or context.stop_status
                        else "failed"
                    )
                    _evidence(
                        context,
                        allowance,
                        call,
                        kind,
                        attempt,
                        params,
                        status="uncertain" if row["outcome"] == "uncertain" else "failed",
                        error=error,
                        gap=gap,
                    )
                _progress(context, allowance, gap, {}, technical=True, logical=attempt == 0, kind=kind)
                category = classify_tool_error(error)
                if (
                    not isinstance(error, Exception)
                    or context.stop_status
                    or category not in ("transient", "rate_limited")
                    or attempt >= context.policy.retry.max_retries
                    or gap["status"] == "blocked"
                ):
                    raise
                delay = max(
                    min(context.policy.retry.max_delay_seconds, context.policy.retry.delay_seconds * 2**attempt),
                    retry_after(error) or 0 if http_status(error) == 429 else 0,
                )
                from agent.runtime.capacity import pending_seconds
                from agent.runtime.collection import current_collection
                from agent.runtime.policies import ReservationUnavailable

                remaining = context.remaining()
                if remaining is not None and delay + pending_seconds(context) >= remaining:
                    scope = current_collection()
                    if scope is not None:
                        scope.finish("reserved_time")
                    raise ReservationUnavailable("time_exhausted: not enough unreserved time for retry") from error
                if context.stop_event.wait(delay):
                    context.check_active()
    finally:
        with context.budget_lock:
            if context.research_cache[request_key]["status"] == "in_flight":
                context.research_cache[request_key]["status"] = "failed"
            context.research_waiters[request_key].set()
            _checkpoint(context)


@evidence_tool("research.v1")
@tool
def research_step(
    action: Literal["search", "read", "report"],
    gap_id: str = "",
    query: str = "",
    source_id: str = "",
    reason: str = "",
) -> str:
    """Research one registered gap. search: gap_id+query; read: gap_id+source_id (no query); report: no other IDs/query. Use IDs from current allowed_actions. Never invent requirements or sources."""
    context, allowance, call = get_run_context(), search_allowance.get(), active_call.get()
    try:
        return _perform(action, gap_id, query, source_id, reason)
    except (ExecutionTerminated, CollectionFinished, DependencyUnavailable):
        raise
    except Exception as error:
        if context is None or allowance is None or call is None or context.stop_status:
            raise
        spent = sum(r["call_id"] == call.call_id and r["status"] == "spent" for r in context.attempt_ledger.values())
        if isinstance(error, PolicyError) and not spent:
            allowance.task.invalid_decisions += 1
            if allowance.task.invalid_decisions >= 2:
                allowance.task.finalize_only = True
            if action in ("search", "read") and not any(e["call_id"] == call.call_id for e in allowance.task.evidence):
                _evidence(
                    context,
                    allowance,
                    call,
                    action,
                    0,
                    dict(call.args),
                    status="rejected",
                    error=error,
                    gap=context.gaps.get(gap_id),
                )
            status = "rejected"
        else:
            status = "failed"
        return json.dumps(
            {**available_actions(context, allowance), "status": status, "reason": str(error), "outbound": spent},
            ensure_ascii=False,
        )
