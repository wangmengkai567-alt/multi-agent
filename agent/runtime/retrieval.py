"""Task-local source catalog, including verified cache-consumption references."""

import re
from dataclasses import dataclass


def evidence_available(item):
    if item.get("status") != "completed":
        return False
    from agent.runtime.context import get_run_context
    from agent.runtime.evidence_protocol import validate_record

    context = get_run_context()
    validate_record(item, context)
    if not item["sources"]:
        return False
    return bool(item["executed"])


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
        from agent.runtime.evidence_protocol import source_rows

        return {source_key(row["url"]): row for _, row, _ in source_rows(self.evidence) if row["kind"] == "web"}

    def resolve_url(self, value):
        key = value.removeprefix("[source:").removesuffix("]")
        source = self.sources.get(key)
        return source["url"] if source else value
