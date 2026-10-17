"""Single-attempt Tavily tool; transient retries belong to the runtime Hook."""

import os
from typing import Literal

from dotenv import load_dotenv
from langchain_core.tools import tool
from tavily import TavilyClient

from agent.runtime.policies import MissingConfiguration, policy_for_run, request_timeout
from api.monitor import monitor


@tool
def internet_search(
    query: str,
    topic: Literal["news", "finance", "general"] = "general",
    max_results: int = 5,
    include_raw_content: bool = False,
):
    """Search public web information using a bounded, single-attempt request."""
    if not query.strip() or not 1 <= max_results <= 20:
        raise ValueError("Invalid search arguments")
    load_dotenv()
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        raise MissingConfiguration("Tavily API key is missing")
    monitor.report_tool("网络搜索工具", {"query": query, "topic": topic, "max_results": max_results})
    with TavilyClient(api_key=key) as client:
        return client.search(
            query=query,
            topic=topic,
            max_results=max_results,
            include_raw_content=include_raw_content,
            timeout=request_timeout(policy_for_run().sql.timeout_seconds),
        )
