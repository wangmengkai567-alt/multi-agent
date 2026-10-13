"""Checks of the test harness only; these do not test Agent functionality."""

import os
import socket
from pathlib import Path

import pytest


def test_dotenv_cannot_load_a_local_secret(sandbox_dir, monkeypatch):
    from dotenv import load_dotenv

    monkeypatch.delenv("BASELINE_TEST_SECRET", raising=False)
    env_file = sandbox_dir / ".env"
    env_file.write_text("BASELINE_TEST_SECRET=must-not-load\n", encoding="utf-8")
    load_dotenv(env_file)
    assert "BASELINE_TEST_SECRET" not in os.environ


def test_socket_connections_are_denied_before_dialing():
    with socket.socket() as connection:
        with pytest.raises(RuntimeError, match="Network access is disabled"):
            connection.connect(("127.0.0.1", 9))


def test_internal_socketpair_does_not_enable_arbitrary_loopback_connections():
    first, second = socket.socketpair()
    try:
        first.sendall(b"internal")
        assert second.recv(8) == b"internal"
    finally:
        first.close()
        second.close()
    with socket.socket() as connection:
        with pytest.raises(RuntimeError, match="Network access is disabled"):
            connection.connect_ex(("127.0.0.1", 9))


def test_database_and_relative_writes_use_temporary_directory(sandbox_dir, sandbox_db):
    sandbox_db.execute("CREATE TABLE sample (value TEXT)")
    sandbox_db.execute("INSERT INTO sample VALUES (?)", ("offline",))
    assert sandbox_db.execute("SELECT value FROM sample").fetchone() == ("offline",)
    database_file = Path(sandbox_db.execute("PRAGMA database_list").fetchone()[2])
    assert database_file.parent == sandbox_dir
    Path("sample.txt").write_text("offline", encoding="utf-8")
    assert (sandbox_dir / "sample.txt").read_text(encoding="utf-8") == "offline"
