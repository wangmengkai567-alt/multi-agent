"""Explicit source/report fixtures for the current per-fact protocol."""

import json

from agent.reflection import source_id
from agent.runtime.facts import parse_report
from agent.runtime.review_contract import ReviewContract

URL = "https://example.test/source"
BODY = "Agent A delegates through the task tool. Agent B returns a tool message."


def evidence(body=BODY, url=URL, call_id="source-call"):
    return {
        "name": "internet_search",
        "executed": True,
        "status": "completed",
        "call_id": call_id,
        "content": {"results": [{"url": url, "content": body}]},
    }


def fact(ident="f1", body=BODY, url=URL, value="delegates through task"):
    return {
        "id": ident,
        "dimensions": {
            "model": None,
            "variant": None,
            "region": None,
            "channel": None,
            "window": None,
            "eligibility": None,
            "price_kind": None,
        },
        "subject": "Agent A",
        "aspect": "delegation",
        "value": value,
        "scope": "source snapshot",
        "evidence": [{"source_id": source_id(url), "quote": body}],
        "identity_evidence": [{"source_id": source_id(url), "quote": body}],
        "as_of": None,
        "time_evidence": None,
    }


def report(*facts, gaps=(), analysis=()):
    return json.dumps({"facts": list(facts), "analysis": list(analysis), "gaps": list(gaps)}, ensure_ascii=False)


def contract(context, *, facts=None, requirements=(), objects=(), evidence_rows=None):
    rows = [evidence()] if evidence_rows is None else evidence_rows
    parsed = parse_report(report(*(facts if facts is not None else [fact()])), rows, 4000)
    return ReviewContract.create(
        context.user_query, "Agent A", report(fact()), rows, requirements=requirements, report=parsed, objects=objects
    )


def decision(*, verdict="passed", object_id=None, coverage=None, fact_id="f1"):
    return {
        "facts": [
            {
                "fact_id": fact_id,
                "verdict": verdict,
                "reason": "checked against the supplied source",
                "object_match": {
                    "object_id": object_id,
                    "relation": "same_object" if object_id else "unresolved",
                    "reason": "source identity",
                },
            }
        ],
        "analysis": [],
        "coverage": coverage or {},
        "findings": [],
    }
