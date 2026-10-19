"""Bound model-visible search excerpts across a task without changing stored originals."""

import json
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
    # Keep actual call IDs/parameters/URLs and explicit truncation markers.
    result, bodies = [], []
    for item in evidence:
        row = {key: value for key, value in item.items() if key != "model_content"}
        content = (
            item.get("content")
            if item["name"] in ("internet_search", "read_web_page")
            else item.get("model_content", item.get("content"))
        )
        if item["name"] in ("internet_search", "read_web_page"):
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except ValueError:
                    pass
            if isinstance(content, dict):
                content = dict(
                    query=content.get("query"),
                    results=[
                        dict(
                            title=source.get("title", ""),
                            url=source.get("url", ""),
                            content=str(source.get("raw_content") or source.get("content") or ""),
                            excerpt_truncated=bool(source.get("excerpt_truncated", False)),
                        )
                        for source in content.get("results", [])
                        if isinstance(source, dict)
                    ],
                    evidence_note="真实工具记录；部分正文片段可能截断，完整原文另行保存。",
                )
                for source in content.get("results", []):
                    if isinstance(source, dict) and isinstance(source.get("content"), str):
                        bodies.append(source)
                        source["_page"] = item["name"] == "read_web_page"
                        source["_query"] = item.get("request_parameters", {}).get(
                            "query", str(content.get("query") or "")
                        )
        row["content"] = content
        result.append(row)
    capacities = {
        id(source): min(len(source["content"]), 5000 if source["_page"] else per_source_chars) for source in bodies
    }
    remaining, pending = content_chars, list(bodies)
    allowances = {}
    # Targeted page extracts carry the sections the worker actually read. Keep
    # those before spending the remainder on discovery snippets.
    pages = [source for source in bodies if source["_page"]]
    if pages:
        share = min(5000, content_chars // len(pages))
        for source in pages:
            allowances[id(source)] = min(capacities[id(source)], share)
            remaining -= allowances[id(source)]
        pending = [source for source in pending if not source["_page"]]
    while pending:
        share = remaining // len(pending)
        short = [source for source in pending if capacities[id(source)] <= share]
        if not short:
            for source in pending:
                allowances[id(source)] = share
            break
        for source in short:
            allowances[id(source)] = capacities[id(source)]
            remaining -= capacities[id(source)]
        pending = [source for source in pending if id(source) not in allowances]
    for source in bodies:
        text = source["content"]
        size = allowances[id(source)]
        source.pop("_page", None)
        query = source.pop("_query", "")
        if size < len(text):
            source["content"] = relevant_excerpt(text, size, query)
            source["excerpt_truncated"] = True
    return result
