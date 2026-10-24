"""OpenAI-compatible clients with explicit, independent role configuration."""

import json
import os
import re

import httpx
from dotenv import find_dotenv, load_dotenv
from langchain.chat_models import init_chat_model

from agent.runtime.policies import MissingConfiguration, load_runtime_policy


def model_options(raw, field_name):
    if not raw or not raw.strip():
        return {}
    try:
        extra = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a JSON object") from exc
    if not isinstance(extra, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return {"extra_body": extra}


def create_role_model(role):
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", role):
        raise ValueError("Invalid model profile name")
    prefix = f"LLM_{role}_"
    settings = {name: os.getenv(prefix + name, "").strip() for name in ("MODEL", "BASE_URL", "API_KEY")}
    missing = [prefix + name for name, value in settings.items() if not value]
    if missing:
        raise MissingConfiguration("Missing required model configuration: " + ", ".join(missing))
    options = model_options(os.getenv(prefix + "EXTRA_BODY"), prefix + "EXTRA_BODY")
    from agent.runtime.measurement import (
        aobserve_http_request,
        aobserve_http_response,
        observe_http_request,
        observe_http_response,
    )
    from agent.runtime.scheduler import account_group

    return init_chat_model(
        http_client=httpx.Client(
            follow_redirects=False,
            event_hooks={
                "request": [observe_http_request],
                "response": [observe_http_response],
            },
        ),
        http_async_client=httpx.AsyncClient(
            follow_redirects=False,
            event_hooks={
                "request": [aobserve_http_request],
                "response": [aobserve_http_response],
            },
        ),
        model=settings["MODEL"],
        model_provider="openai",
        base_url=settings["BASE_URL"],
        api_key=settings["API_KEY"],
        metadata={
            "resource_group": load_runtime_policy().scheduler.model_accounts.get(role)
            or account_group("openai", settings["BASE_URL"], settings["API_KEY"])
        },
        max_retries=0,
        timeout=load_runtime_policy().timeouts.request_seconds,
        **options,
    )


# This module is imported only when the application initializes its agent.
load_dotenv(find_dotenv())
