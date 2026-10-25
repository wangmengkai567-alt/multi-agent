"""Bound model-visible search excerpts across a task without changing stored originals."""

import math
import re
from itertools import islice


def relevant_excerpt(text, size, query=""):
    """Select real query-matching context inside a fixed budget."""
    if len(text) <= size:
        return text
    marker = " […片段省略…] "
    if size <= 2 * len(marker):
        return text[:size]
    words = re.findall(r"[A-Za-z_][A-Za-z_0-9-]{2,}|[\u4e00-\u9fff]{2,}", query.lower())
    stopwords = {"the", "and", "for", "with", "site", "https", "http", "www", "com", "org", "docs"}
    tokens = set()
    for word in words:
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", word):
            tokens.update(word[i : i + 2] for i in range(len(word) - 1))
        elif word not in stopwords:
            tokens.add(word)
    lowered = text.lower()
    matches = {token: [m.start() for m in islice(re.finditer(re.escape(token), lowered), 64)] for token in tokens}
    matches = {token: positions for token, positions in matches.items() if positions}
    if not matches:
        available = size - len(marker)
        head = available * 2 // 3
        return text[:head] + marker + text[-(available - head) :]
    window = size - 2 * len(marker)
    starts = {0, max(0, len(text) - window)}
    starts.update(
        max(0, min(len(text) - window, pos - window // 3)) for positions in matches.values() for pos in positions
    )

    def score(start):
        return sum(
            (len(token) + 1) / math.sqrt(len(positions))
            for token, positions in matches.items()
            if any(start <= pos and pos + len(token) <= start + window for pos in positions)
        )

    start = max(starts, key=lambda position: (score(position), -position))
    end = min(len(text), start + window)
    return (marker if start else "") + text[start:end] + (marker if end < len(text) else "")


def model_evidence(evidence, content_chars, per_source_chars=1000):
    """Bound normalized source text; original records and provenance remain unchanged."""
    from agent.runtime.evidence_protocol import source_rows

    bodies = [
        dict(
            item=item,
            source=source,
            content=body,
            full_read=source["retrieval"] == "page" or source["kind"] == "document",
            query=str(item["request_parameters"].get("query") or ""),
        )
        for item, source, body in source_rows(evidence)
    ]
    capacities = {id(row): min(len(row["content"]), 5000 if row["full_read"] else per_source_chars) for row in bodies}
    allowances, remaining = {}, content_chars
    pending = [row for row in bodies if row["full_read"]]
    if pending:
        share = min(5000, remaining // len(pending))
        for row in pending:
            allowances[id(row)] = min(capacities[id(row)], share)
            remaining -= allowances[id(row)]
    pending = [row for row in bodies if id(row) not in allowances]
    while pending:
        share = remaining // len(pending)
        short = [row for row in pending if capacities[id(row)] <= share]
        if not short:
            for row in pending:
                allowances[id(row)] = share
            break
        for row in short:
            allowances[id(row)] = capacities[id(row)]
            remaining -= allowances[id(row)]
        pending = [row for row in pending if id(row) not in allowances]
    visible = []
    for row in bodies:
        size = allowances[id(row)]
        if size <= 0:
            continue
        item, source, body = row["item"], row["source"], row["content"]
        excerpt = relevant_excerpt(body, size, row["query"])
        visible.append(
            dict(
                name=item["name"],
                call_id=item["call_id"],
                attempt=item["attempt"],
                source_key=source["key"],
                content=excerpt,
                excerpt_truncated=len(excerpt) < len(body),
            )
        )
    return visible
