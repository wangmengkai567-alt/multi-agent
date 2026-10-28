"""Role-specific clients stay isolated and never use shared credentials."""

import runpy
from pathlib import Path
from types import SimpleNamespace

import dotenv
import langchain.chat_models
import pytest

from agent.runtime.policies import MissingConfiguration

ROLES = ("MAIN", "SUB", "CUSTOM")
LOADER = Path(__file__).resolve().parents[1] / "agent" / "llm.py"


@pytest.fixture
def loader(monkeypatch):
    calls = []
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(dotenv, "find_dotenv", lambda *args, **kwargs: "")
    for role in ROLES:
        monkeypatch.setenv(f"LLM_{role}_MODEL", f"model-{role.lower()}")
        monkeypatch.setenv(f"LLM_{role}_BASE_URL", f"https://{role.lower()}.example.test/v1")
        monkeypatch.setenv(f"LLM_{role}_API_KEY", f"test-key-{role.lower()}")
        monkeypatch.delenv(f"LLM_{role}_EXTRA_BODY", raising=False)
    # Ambient SDK credentials must not override any role's explicit connection.
    monkeypatch.setenv("OPENAI_API_KEY", "shared-key-must-not-be-used")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://shared.example.test/v1")

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(langchain.chat_models, "init_chat_model", create)

    def load():
        module = runpy.run_path(str(LOADER))
        assert not calls
        module["models"] = {role: module["create_role_model"](role) for role in ROLES}
        return module

    yield load, calls
    for options in calls:
        options["http_client"].close()
        import asyncio

        asyncio.run(options["http_async_client"].aclose())


def test_clients_use_separate_models_endpoints_and_keys(loader):
    load, calls = loader
    module = load()
    assert len(calls) == 3
    assert len({id(model) for model in module["models"].values()}) == 3
    for role, options in zip(ROLES, calls, strict=True):
        assert options["model"] == f"model-{role.lower()}"
        assert options["base_url"] == f"https://{role.lower()}.example.test/v1"
        assert options["api_key"] == f"test-key-{role.lower()}"
        assert options["max_retries"] == 0
        assert "extra_body" not in options


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("field", ("MODEL", "BASE_URL", "API_KEY"))
@pytest.mark.parametrize("value", (None, "   "))
def test_missing_role_config_is_rejected_despite_shared_sdk_config(loader, monkeypatch, role, field, value):
    load, _ = loader
    variable = f"LLM_{role}_{field}"
    if value is None:
        monkeypatch.delenv(variable)
    else:
        monkeypatch.setenv(variable, value)
    with pytest.raises(MissingConfiguration, match=variable):
        load()


@pytest.mark.parametrize("role", ROLES)
def test_extra_body_only_applies_to_its_role(loader, monkeypatch, role):
    load, calls = loader
    monkeypatch.setenv(f"LLM_{role}_EXTRA_BODY", '{"thinking":{"type":"disabled"}}')
    load()
    for current, options in zip(ROLES, calls, strict=True):
        if current == role:
            assert options["extra_body"] == {"thinking": {"type": "disabled"}}
        else:
            assert "extra_body" not in options


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("value", ("[]", "null", "invalid-private-value"))
def test_invalid_extra_body_reports_role_without_echoing_value(loader, monkeypatch, role, value):
    load, _ = loader
    variable = f"LLM_{role}_EXTRA_BODY"
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=variable) as error:
        load()
    assert value not in str(error.value)


def test_import_does_not_create_clients(loader):
    _, calls = loader
    module = runpy.run_path(str(LOADER))
    assert calls == [] and callable(module["create_role_model"])
