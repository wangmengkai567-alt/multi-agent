"""Normalize provider reasoning envelopes without changing ordinary quoted text."""

import re
from urllib.parse import urldefrag

from langchain_core.messages import AIMessage


def visible_model_text(text):
    prefix, marker, answer = text.partition("</think>")
    if not marker:
        return text
    envelope = prefix.lstrip()
    if envelope.startswith("<think>") or (
        envelope.startswith("{") and '"failed_checks"' in prefix and '"verified_results"' in prefix
    ):
        return answer.strip()
    return text


def normalize_model_message(message):
    if isinstance(message, AIMessage) and isinstance(message.content, str):
        text = visible_model_text(message.content)
        if text != message.content:
            return message.model_copy(
                update=dict(
                    content=text, additional_kwargs={**message.additional_kwargs, "provider_output_normalized": True}
                )
            )
    return message


def _trailing_url_end(candidate):
    """Port Codex terminal_hyperlinks::trailing_url_end's balanced-delimiter scan."""
    pairs = {"(": ")", "[": "]", "{": "}", "<": ">"}
    balances = dict.fromkeys(pairs.values(), 0)
    for char in candidate:
        if char in pairs:
            balances[pairs[char]] += 1
        elif char in balances:
            balances[char] -= 1
    end = len(candidate)
    while end:
        char = candidate[end - 1]
        if char in balances:
            trim = balances[char] < 0
            balances[char] += 1
        else:
            trim = char in ",.;!'\"" or (char == "?" and end > 1 and balances.get(candidate[end - 2], 0) < 0)
        if not trim:
            break
        end -= 1
    return end


def cited_urls(text):
    visible = re.sub(r"```.*?```|`[^`]*`", "", text, flags=re.DOTALL)
    candidates = re.findall(r'https?://[^\s<>\[\]"\'\\（）“”‘’；，。]+', visible)
    return {url[: _trailing_url_end(url)] for url in candidates}


def source_page(url):
    return urldefrag(url)[0]
