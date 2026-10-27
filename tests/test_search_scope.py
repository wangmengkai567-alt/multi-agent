"""Reasoning envelopes remain private without altering ordinary quoted text."""

import pytest

from agent.runtime.output import visible_model_text


@pytest.mark.parametrize(
    "text", ["ordinary answer", "Explain </think> literally", '{"unrelated":true}</think>quoted content']
)
def test_reasoning_cleanup_preserves_unrelated_literal_text(text):
    assert visible_model_text(text) == text


def test_provider_reasoning_envelope_is_removed():
    assert visible_model_text("<think>private reasoning</think>Visible reply") == "Visible reply"
