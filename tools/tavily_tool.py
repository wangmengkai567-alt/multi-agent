"""Bounded search output for models; original provider evidence stays in the artifact."""

import ipaddress
import json
import os
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from dotenv import load_dotenv
from langchain_core.tools import tool
from pydantic import BeforeValidator, Field
from tavily import TavilyClient

from agent.runtime.evidence import relevant_excerpt
from agent.runtime.policies import MissingConfiguration, PolicyError, policy_for_run, request_timeout
from agent.runtime.retrieval import SearchFollowUp, SearchHistory, usable_page
from agent.runtime.search import search_allowance
from api.monitor import monitor


def preserve_http_errors(client):
    """Keep status and Retry-After before the SDK converts HTTP errors to headerless exceptions."""
    session = getattr(client, "session", None)
    if session is None:
        return

    def inspect(response, **kwargs):
        if response.status_code >= 400:
            response.raise_for_status()
        return response

    session.hooks.setdefault("response", []).append(inspect)


def compact_search_response(payload, limits, query=None):
    """Keep actual URLs and explicit excerpts without loading full pages into the model context."""
    remaining = limits.content_chars
    rows = []
    for result in payload.get("results", []):
        text = str(result.get("raw_content") or result.get("content") or "")
        excerpt = relevant_excerpt(text, min(limits.result_chars, remaining), query or str(payload.get("query") or ""))
        remaining -= len(excerpt)
        rows.append(
            dict(
                source_id=__import__("hashlib").sha256(str(result.get("url", "")).encode()).hexdigest()[:12],
                title=result.get("title", ""),
                url=result.get("url", ""),
                content=excerpt,
                excerpt_truncated=len(excerpt) < len(text),
            )
        )
    return json.dumps(
        dict(
            query=payload.get("query"),
            results=rows,
            evidence_note="完整检索响应已保存为原始证据；content 为可能截断的片段。",
        ),
        ensure_ascii=False,
    )


def domain_array(value):
    """Accept a JSON-encoded array used by some compatible tool-call transports."""
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, list) and (
        not 1 <= len(value) <= 20
        or any(
            not isinstance(domain, str)
            or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", domain)
            or "." not in domain
            for domain in value
        )
    ):
        raise ValueError("include_domains must contain domain names without URL paths")
    return value


DomainList = Annotated[
    list[str],
    BeforeValidator(domain_array),
    Field(description="Bare domain names only, e.g. docs.python.org. Never include https:// or a URL path."),
]


@tool(response_format="content_and_artifact")
def internet_search(
    query: str,
    topic: Literal["news", "finance", "general"] = "general",
    max_results: int = 3,
    include_raw_content: bool = False,
    include_domains: DomainList | None = None,
    follow_up: SearchFollowUp | None = None,
):
    """Search public sources; start with one query. Every subsequent search requires follow_up grounded in the previous call: confirmed information, a necessary missing fact, why existing sources cannot answer it, and a targeted search goal. Assess key listed source_ids; explain every source only when claiming all previous results are irrelevant; read key relevant pages before searching again. The runtime binds the previous search automatically; do not supply tool-call IDs. For official sources use include_domains, and site: plus the required version path."""
    if include_domains is not None and (
        not 1 <= len(include_domains) <= 20
        or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", domain) or "." not in domain
            for domain in include_domains
        )
    ):
        raise ValueError("include_domains must contain domain names without URL paths")
    if not query.strip() or not 1 <= max_results <= 20:
        raise ValueError("Invalid search arguments")
    load_dotenv()
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        raise MissingConfiguration("Tavily API key is missing")
    limits = policy_for_run().search
    monitor.report_tool("网络搜索工具", {"query": query, "topic": topic, "max_results": max_results})
    allowance = search_allowance.get()
    depth = "advanced" if allowance and allowance.task.refinement_open else "basic"
    with TavilyClient(api_key=key) as client:
        preserve_http_errors(client)
        payload = client.search(
            query=query,
            **({"include_domains": include_domains} if include_domains else {}),
            topic=topic,
            max_results=max_results,
            include_raw_content=include_raw_content,
            search_depth=depth,
            timeout=request_timeout(limits.timeout_seconds),
        )
    return compact_search_response(payload, limits, query), payload


def validate_page_request(url, query):
    """Only this delegation's observed public origins (or user-provided URLs)."""
    from agent.reflection import source_urls, tool_evidence
    from agent.runtime.context import get_run_context
    from agent.runtime.output import cited_urls

    if not isinstance(url, str) or not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise PolicyError("网页读取必须提供 URL 和简短的当前问题")
    url = SearchHistory(tool_evidence.get() or []).resolve_url(url)
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (
        parsed.scheme not in ("https", "http")
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 80, 443)
    ):
        raise PolicyError("只允许读取公开 HTTP(S) 网页")
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    if is_ip or "." not in host or host.endswith((".local", ".localhost", ".internal")):
        raise PolicyError("只允许读取公开域名")
    context = get_run_context()
    allowed = source_urls(tool_evidence.get() or [])
    allowed += list(cited_urls(context.user_query)) if context else []
    if host not in {urlsplit(value).hostname for value in allowed}:
        raise PolicyError("只能读取当前子任务实际搜索取得的域名或用户提供的 URL；不能借用其他子任务来源")
    return url


@tool(response_format="content_and_artifact")
def read_web_page(url: str, query: str):
    """Read only sections needed for query. Pass a returned source_id as url to preserve the exact discovered address. A full URL is used for user-provided pages or a specific new version path on an observed origin. This returns relevant excerpts, not a guarantee of full-page reading. Error/login pages are unavailable evidence."""
    url = validate_page_request(url, query)
    load_dotenv()
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        raise MissingConfiguration("Tavily API key is missing")
    limits = policy_for_run().search
    monitor.report_tool("网页正文读取工具", {"url": url, "query": query})
    with TavilyClient(api_key=key) as client:
        preserve_http_errors(client)
        payload = client.extract(
            urls=[url],
            query=query,
            chunks_per_source=5,
            extract_depth="advanced",
            format="text",
            timeout=request_timeout(limits.timeout_seconds),
        )
    # Keep rejected rows as failure evidence, never as readable/citable page results.
    results = payload.get("results", [])
    rejected = [
        {**row, "error": "未取得有效正文：错误页、登录/验证页或空正文"} for row in results if not usable_page(row)
    ]
    payload = {
        **payload,
        "query": query,
        "results": [row for row in results if usable_page(row)],
        "failed_results": [*payload.get("failed_results", []), *rejected],
        "read_status": "read" if any(usable_page(row) for row in results) else "unavailable",
    }
    compact = json.loads(compact_search_response(payload, limits.model_copy(update={"result_chars": 5000}), query))
    compact["failed_results"] = [
        {"url": row.get("url", ""), "error": row.get("error", "正文不可用")}
        for row in payload.get("failed_results", [])
    ]
    compact["read_status"] = payload["read_status"]
    return json.dumps(compact, ensure_ascii=False), payload
