"""Task-owned immutable evidence snapshots and server-issued quote references."""

import re
from hashlib import sha256

from agent.runtime.context import get_run_context
from agent.runtime.evidence_protocol import adapter_by_id, source_rows, validate_input, validate_source


class QuoteError(ValueError):
    pass


def task_owner():
    from agent.runtime.hooks import active_call

    call = active_call.get()
    if call is None or not call.search_task_key:
        raise QuoteError("Quote registration requires an owned delegated task")
    return call.search_task_key


def _body(row):
    return row["content"]


def _persist(context):
    from agent.runtime.checkpoints import save_job_checkpoint, write_json

    write_json(
        context.work_dir / "quote-registry.json",
        dict(scope_id=context.research_scope_id, snapshots=context.evidence_snapshots, quotes=context.quote_registry),
    )
    save_job_checkpoint(context)


def register_options(evidence, visible):
    """Register only contiguous text actually shown, pinned to its original result."""
    context, owner = get_run_context(), task_owner()
    context.check_active()
    options, sources, seen = [], {}, set()
    with context.budget_lock:
        originals = list(source_rows(evidence))
        for shown in visible:
            candidates = [
                (item, row, body)
                for item, row, body in originals
                if item["call_id"] == shown["call_id"]
                and item["attempt"] == shown["attempt"]
                and item["name"] == shown["name"]
                and row["key"] == shown["source_key"]
            ]
            if len(candidates) != 1:
                raise QuoteError("Visible source does not identify exactly one normalized result")
            for item, row, raw_body in candidates:
                excerpt = shown["content"]
                original = _body(row)
                digest = sha256(original.encode("utf-8")).hexdigest()
                identity = dict(
                    task_id=owner,
                    evidence_task_id=item["task_id"],
                    scope_id=item["scope_id"],
                    adapter=item["adapter"],
                    run_id=item["run_id"],
                    thread_id=item["thread_id"],
                    call_id=item["call_id"],
                    attempt=item.get("attempt", 0),
                    origin_call_id=item.get("origin_call_id"),
                    origin_attempt=item.get("origin_attempt"),
                    origin_source_id=row["origin_source_id"],
                    digest=digest,
                )
                snapshot = next(
                    (s for s in context.evidence_snapshots.values() if all(s[k] == v for k, v in identity.items())),
                    None,
                )
                if snapshot is None:
                    sid = f"s{len(context.evidence_snapshots) + 1}"
                    snapshot = dict(identity, snapshot_id=sid, row=row)
                    context.evidence_snapshots[sid] = snapshot
                sid = snapshot["snapshot_id"]
                sources[sid] = dict(
                    source_id=sid,
                    url=row["url"],
                    title=row["title"],
                    kind=row["kind"],
                    locator=row["locator"],
                    provenance=row["provenance"],
                    inputs=row["inputs"],
                )
                for section in re.split(r"\s*\[…(?:片段省略|独立摘录)…\]\s*", excerpt):
                    for line in re.split(r"(?<=[。！？])|\n", section):
                        for offset in range(0, len(line), 400):
                            quote = line[offset : offset + 400].strip()
                            start = original.find(quote)
                            if (
                                not quote
                                or start < 0
                                or quote not in raw_body
                                or (sid, quote) in seen
                                or "[REDACTED]" in quote
                                or "[OMITTED]" in quote
                            ):
                                continue
                            seen.add((sid, quote))
                            registered = next(
                                (
                                    q
                                    for q in context.quote_registry.values()
                                    if q["snapshot_id"] == sid and q["quote"] == quote
                                ),
                                None,
                            )
                            if registered is None:
                                qid = f"q{len(context.quote_registry) + 1}"
                                registered = dict(
                                    quote_id=qid,
                                    snapshot_id=sid,
                                    task_id=owner,
                                    quote=quote,
                                    start=start,
                                    end=start + len(quote),
                                )
                                context.quote_registry[qid] = registered
                            options.append(dict(quote_id=registered["quote_id"], source_id=sid, quote=quote))
        _persist(context)
    return options, list(sources.values())


def resolve_quote(quote_id, evidence, *, owner=None):
    context = get_run_context()
    if context is None:
        raise QuoteError("Quote resolution requires a run context")
    quote = context.quote_registry.get(quote_id)
    if quote is None or owner is not None and quote["task_id"] != owner:
        raise QuoteError("Unknown or foreign-task quote_id: " + quote_id)
    snapshot = context.evidence_snapshots.get(quote["snapshot_id"])
    if snapshot is None or snapshot["task_id"] != quote["task_id"]:
        raise QuoteError("Quote snapshot ownership mismatch")
    body = _body(snapshot["row"])
    if (
        sha256(body.encode("utf-8")).hexdigest() != snapshot["digest"]
        or body[quote["start"] : quote["end"]] != quote["quote"]
    ):
        raise QuoteError("Quote snapshot content changed")
    if not any(
        item["call_id"] == snapshot["call_id"]
        and item["adapter"] == snapshot["adapter"]
        and item["scope_id"] == snapshot["scope_id"]
        and item["task_id"] == snapshot["evidence_task_id"]
        and item["run_id"] == snapshot["run_id"]
        and item["thread_id"] == snapshot["thread_id"]
        and item["attempt"] == snapshot["attempt"]
        and item.get("origin_call_id") == snapshot["origin_call_id"]
        and item.get("origin_attempt") == snapshot["origin_attempt"]
        and row == snapshot["row"]
        and quote["quote"] in raw_body
        for item, row, raw_body in source_rows(evidence)
    ):
        raise QuoteError("Quote does not belong to the supplied actual evidence")
    return dict(quote_id=quote_id, source_id=snapshot["snapshot_id"], quote=quote["quote"]), snapshot["row"]


def verified_sources(fact, evidence):
    """Recheck snapshots at coverage/delivery time, never use the latest page by URL."""
    for ref in fact["evidence"]:
        expanded, row = resolve_quote(ref["quote_id"], evidence)
        if expanded != ref:
            raise QuoteError("Expanded evidence reference changed")
        yield row


def validate_fact(fact, evidence):
    refs = [*fact["evidence"], *fact["identity_evidence"]]
    if fact["time_evidence"]:
        refs.append(fact["time_evidence"])
    for ref in refs:
        expanded, _ = resolve_quote(ref["quote_id"], evidence)
        if ref != expanded:
            raise QuoteError("Expanded evidence reference changed")


def validate_registry(context):
    """Validate checkpoint contents without granting new evidence ownership."""
    inputs = {}
    for sid, snapshot in context.evidence_snapshots.items():
        if snapshot["scope_id"] != context.research_scope_id or snapshot["thread_id"] != context.thread_id:
            raise QuoteError("Saved source belongs to another logical task")
        source = validate_source(snapshot["row"], adapter_by_id(snapshot["adapter"]))
        if (
            sid != snapshot["snapshot_id"]
            or source.digest != snapshot["digest"]
            or source.origin_source_id != snapshot["origin_source_id"]
        ):
            raise QuoteError("Saved source snapshot changed")
        for reference in source.inputs:
            inputs[(reference.path, reference.sha256)] = reference
    for reference in inputs.values():
        validate_input(reference, context)
    for qid, quote in context.quote_registry.items():
        snapshot = context.evidence_snapshots.get(quote["snapshot_id"])
        if (
            quote["quote_id"] != qid
            or snapshot is None
            or "[REDACTED]" in quote["quote"]
            or "[OMITTED]" in quote["quote"]
            or snapshot["snapshot_id"] != quote["snapshot_id"]
            or snapshot["task_id"] != quote["task_id"]
            or not 0 <= quote["start"] < quote["end"] <= len(_body(snapshot["row"]))
            or _body(snapshot["row"])[quote["start"] : quote["end"]] != quote["quote"]
            or sha256(_body(snapshot["row"]).encode("utf-8")).hexdigest() != snapshot["digest"]
        ):
            raise QuoteError("Invalid saved quote registry")
