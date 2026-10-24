"""Task-local source catalog, including verified cache-consumption references."""

import json
import re
from dataclasses import dataclass


def evidence_available(item):
    if item.get("status") != "completed":
        return False
    if item.get("executed"):
        return True
    if not item.get("cache_hit") or not item.get("origin_call_id"):
        return False
    from agent.runtime.context import get_run_context

    context = get_run_context()
    if context is None:
        return False
    return any(
        entry.get("status") == "served"
        and entry.get("call_id") == item["origin_call_id"]
        and entry.get("attempt") == item.get("origin_attempt")
        and entry.get("payload") == item.get("content")
        for entry in context.research_cache.values()
    )


def payload(item):
    if not evidence_available(item):
        return {}
    content = item.get("content") or {}
    return json.loads(content) if isinstance(content, str) else content


def source_key(url):
    from hashlib import sha256

    return sha256(url.encode()).hexdigest()[:12]


def usable_page(row):
    """A successful HTTP/extract request can still contain an error or login page."""
    title = str(row.get("title") or "").strip()
    blocked = re.match(
        r"(?i)^(?:404\b|page not found\b|not found\b|access denied\b|just a moment\b|"
        r"verify you are human\b|sign in\b|log in\b|页面不存在|页面未找到|访问验证|安全验证|登录[ -])",
        title,
    )
    text = str(row.get("raw_content") or row.get("content") or "").strip()
    return bool(text) and not blocked


@dataclass(frozen=True)
class SearchHistory:
    evidence: list[dict]

    @property
    def sources(self):
        return {
            source_key(row["url"]): row
            for item in self.evidence
            if item["name"] in ("internet_search", "read_web_page") and evidence_available(item)
            for row in payload(item).get("results", [])
            if row.get("url")
        }

    def resolve_url(self, value):
        key = value.removeprefix("[source:").removesuffix("]")
        source = self.sources.get(key)
        return source["url"] if source else value
