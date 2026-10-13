"""Offline test defaults, including protection during test collection.

Do not import api.server or agent.main_agent here: they currently construct
external clients and open the application's checkpoint database on import.
"""

import os
import socket
import sqlite3
from pathlib import Path

import pytest

# Set before test modules are collected, not only inside a test fixture.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
for _key in tuple(os.environ):
    if _key.startswith(("OPENAI_", "ANTHROPIC_", "TAVILY_", "RAGFLOW_", "MYSQL_")):
        os.environ.pop(_key)
os.environ.pop("LLM_QWEN_MAX", None)

_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_create_connection = socket.create_connection


def _deny_network(*args, **kwargs):
    raise RuntimeError("Network access is disabled in offline tests; use a fake client.")


def pytest_configure(config):
    socket.socket.connect = _deny_network
    socket.socket.connect_ex = _deny_network
    socket.create_connection = _deny_network


def pytest_unconfigure(config):
    socket.socket.connect = _original_connect
    socket.socket.connect_ex = _original_connect_ex
    socket.create_connection = _original_create_connection


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
