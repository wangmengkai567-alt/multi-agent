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


def cited_urls(text):
    visible = re.sub(r"```.*?```|`[^`]*`", "", text, flags=re.DOTALL)
    return set(url.rstrip(".,，。；;") for url in re.findall(r'https?://[^\s<>\[\]()"\'\\（）“”‘’；，。]+', visible))


def source_page(url):
    return urldefrag(url)[0]
