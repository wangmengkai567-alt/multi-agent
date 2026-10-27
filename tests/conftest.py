"""Offline test defaults, including protection during test collection.

Application lifecycle tests inject fake agent builders and temporary databases.
External network access remains disabled during collection and execution.
"""

import os
import socket
import sqlite3
from contextvars import ContextVar
from pathlib import Path

import pytest
import pytest_asyncio

# Set before test modules are collected, not only inside a test fixture.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
for _key in tuple(os.environ):
    if _key.startswith(("LLM_", "OPENAI_", "ANTHROPIC_", "TAVILY_", "MYSQL_", "APP_")):
        os.environ.pop(_key)
os.environ.pop("LANGSMITH_API_KEY", None)
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_create_connection = socket.create_connection
_original_socketpair = socket.socketpair
_inside_socketpair = ContextVar("inside_test_socketpair", default=False)


def _deny_network(*args, **kwargs):
    raise RuntimeError("Network access is disabled in offline tests; use a fake client.")


def _guard_connect(connection, address):
    # Windows asyncio's self-pipe uses the stdlib socketpair fallback, which
    # internally connects two local sockets. Do not allow arbitrary loopback.
    if _inside_socketpair.get():
        return _original_connect(connection, address)
    return _deny_network()


def _guard_connect_ex(connection, address):
    if _inside_socketpair.get():
        return _original_connect_ex(connection, address)
    return _deny_network()


def _internal_socketpair(*args, **kwargs):
    token = _inside_socketpair.set(True)
    try:
        return _original_socketpair(*args, **kwargs)
    finally:
        _inside_socketpair.reset(token)


def pytest_configure(config):
    socket.socket.connect = _guard_connect
    socket.socket.connect_ex = _guard_connect_ex
    socket.create_connection = _deny_network
    socket.socketpair = _internal_socketpair


def pytest_unconfigure(config):
    socket.socket.connect = _original_connect
    socket.socket.connect_ex = _original_connect_ex
    socket.create_connection = _original_create_connection
    socket.socketpair = _original_socketpair


@pytest.fixture(autouse=True)
def isolated_working_directory(tmp_path, monkeypatch):
    """Relative file writes stay outside the repository by default."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def sandbox_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def sandbox_db(sandbox_dir):
    """A fresh SQLite connection, never the application's data directory."""
    connection = sqlite3.connect(sandbox_dir / "test.db")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def test_environment(sandbox_dir, monkeypatch):
    from runtime_fixtures import OfflineEnvironment

    environment = OfflineEnvironment(sandbox_dir)
    environment.configure(monkeypatch)
    return environment


@pytest_asyncio.fixture
async def migrated_environment(test_environment):
    await test_environment.migrate()
    return test_environment


@pytest_asyncio.fixture
async def job_runtime(migrated_environment):
    from types import SimpleNamespace

    from agent.runtime.policies import load_runtime_policy
    from agent.runtime.scheduler import ServiceScheduler
    from api.run_store import RunStore

    store = await RunStore.open(migrated_environment.runs)
    try:
        await store.create_thread("thread")
        await store.create("thread", "controlled job parent", run_id="run")
        yield SimpleNamespace(store=store, scheduler=ServiceScheduler(load_runtime_policy().scheduler))
    finally:
        await store.close()


@pytest.fixture
def inline_provider_worker(monkeypatch):
    """Exercise actual SDK adapters with fake SDK clients, without spawning live providers.

    Process containment is exercised separately by test_stage2/test_stage4.
    """
    from agent.runtime import workers
    from tools import db_tools, tavily_tool

    def execute(operation, payload, *, seconds, on_event=None):
        assert seconds > 0
        if operation == "database":
            return db_tools.execute_worker(payload)
        if operation == "tavily":

            def progress(kind, **data):
                if on_event is not None:
                    on_event(dict(event="progress", kind=kind, **data))

            return tavily_tool.execute_worker(payload, progress)
        raise AssertionError(f"Unexpected provider operation in adapter test: {operation}")

    monkeypatch.setattr(workers, "run_operation", execute)


@pytest_asyncio.fixture
async def authenticated_client(migrated_environment, monkeypatch):
    import agent.main_agent as runtime

    monkeypatch.setattr(runtime, "_build_agent", lambda saver: object())
    async with migrated_environment.client() as client:
        yield client
