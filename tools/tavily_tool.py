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

from agent.runtime.errors import DependencyUnavailable
from agent.runtime.evidence import relevant_excerpt
from agent.runtime.evidence_protocol import evidence_tool, web_result_error, web_results
from agent.runtime.policies import PolicyError, policy_for_run, request_timeout
from agent.runtime.retrieval import SearchHistory
from api.monitor import monitor


def require_network_permission():
    from agent.runtime.context import get_run_context
    from agent.runtime.contracts import assert_contract

    context = get_run_context()
    if context is None or context.network_policy == "forbidden" or not assert_contract(context).network_allowed:
        raise PolicyError("当前契约禁止取得新公开网络证据")


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
    rows, excluded = [], []
    retrieval = "page" if "read_status" in payload else "search"
    for index, result in enumerate(web_results(payload)):
        error = web_result_error(result, retrieval)
        if error:
            excluded.append(dict(result_index=index, error=error))
            continue
        text = result.get("raw_content") or result.get("content")
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
            excluded_results=excluded,
            evidence_note="完整检索响应已保存为原始证据；content 为可能截断的片段。",
        ),
        ensure_ascii=False,
    )


def domain_array(value):
    """Accept a JSON-encoded array used by some compatible tool-call transports."""
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    if value is None or value == []:
        return None
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
    list[str] | None,
    BeforeValidator(domain_array),
    Field(description="Bare domain names only, e.g. docs.python.org. Never include https:// or a URL path."),
]


@evidence_tool("web-search.v1")
@tool(response_format="content_and_artifact")
def internet_search(
    query: str,
    topic: Literal["news", "finance", "general"] = "general",
    max_results: int = 3,
    include_raw_content: bool = False,
    include_domains: DomainList = None,
    search_depth: Literal["basic", "advanced"] = "basic",
):
    """Internal provider search. Model-visible decisions use research_step."""
    require_network_permission()
    include_domains = domain_array(include_domains)
    if not query.strip() or not 1 <= max_results <= 20:
        raise ValueError("Invalid search arguments")
    load_dotenv()
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        raise DependencyUnavailable("Tavily API key is missing")
    limits = policy_for_run().search
    monitor.report_tool("网络搜索工具", {"query": query, "topic": topic, "max_results": max_results})

    with TavilyClient(api_key=key) as client:
        preserve_http_errors(client)
        from agent.runtime.research import audit_transport

        audit_transport(client)
        payload = client.search(
            query=query,
            **({"include_domains": include_domains} if include_domains else {}),
            topic=topic,
            max_results=max_results,
            include_raw_content=include_raw_content,
            search_depth=search_depth,
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
    allowed += list(cited_urls(context.task_instruction)) if context else []
    if host not in {urlsplit(value).hostname for value in allowed}:
        raise PolicyError("只能读取当前子任务实际搜索取得的域名或用户提供的 URL；不能借用其他子任务来源")
    return url


@evidence_tool("web-page.v1")
@tool(response_format="content_and_artifact")
def read_web_page(
    url: Annotated[
        str,
        Field(
            description="Required argument named url, not source_id. Copy read_request.url or a discovered full URL."
        ),
    ],
    query: Annotated[str, Field(description="The specific missing facts to read from this page.")],
):
    """Read only sections needed for query. Pass a returned source_id as url to preserve the exact discovered address. A full URL is used for user-provided pages or a specific new version path on an observed origin. This returns relevant excerpts, not a guarantee of full-page reading. Error/login pages are unavailable evidence."""
    require_network_permission()
    url = validate_page_request(url, query)
    load_dotenv()
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        raise DependencyUnavailable("Tavily API key is missing")
    limits = policy_for_run().search
    monitor.report_tool("网页正文读取工具", {"url": url, "query": query})
    with TavilyClient(api_key=key) as client:
        preserve_http_errors(client)
        from agent.runtime.research import audit_transport

        audit_transport(client)
        payload = client.extract(
            urls=[url],
            query=query,
            chunks_per_source=5,
            extract_depth="advanced",
            format="text",
            timeout=request_timeout(limits.timeout_seconds),
        )
    # Keep rejected rows as failure evidence, never as readable/citable page results.
    results = web_results(payload)
    rejected = [
        dict(
            url=row.get("url", "") if isinstance(row, dict) else "",
            result_index=index,
            error=error,
            original_result=row,
        )
        for index, row in enumerate(results)
        if (error := web_result_error(row, "page"))
    ]
    payload = {
        **payload,
        "query": query,
        "results": [row for row in results if not web_result_error(row, "page")],
        "failed_results": [*payload.get("failed_results", []), *rejected],
        "read_status": "read" if any(not web_result_error(row, "page") for row in results) else "unavailable",
    }
    compact = json.loads(compact_search_response(payload, limits.model_copy(update={"result_chars": 5000}), query))
    compact["failed_results"] = [
        {"url": row.get("url", ""), "error": row.get("error", "正文不可用")}
        for row in payload.get("failed_results", [])
    ]
    compact["read_status"] = payload["read_status"]
    return json.dumps(compact, ensure_ascii=False), payload
