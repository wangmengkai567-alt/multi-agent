"""Shared offline environment, adapted from Codex TestCodexBuilder.

Reference: openai/codex 970111f6, core/tests/common/test_codex.rs.
Only explicit setup migrates databases; production open/auth paths stay intact.
"""

import sqlite3
from contextlib import asynccontextmanager, closing
from dataclasses import dataclass
from pathlib import Path

import httpx

from api.migrate_security import migrate
from api.security import DataLease

ORIGIN = "http://127.0.0.1:8000"
PASSWORD = "offline-test-only-Access-7391!AbCdEf"


def login(client):
    response = client.post(
        "/api/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ORIGIN, "X-Login-Intent": "login"},
    )
    assert response.status_code == 200, response.text
    client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]})


async def login_async(client):
    response = await client.post(
        "/api/auth/login",
        json={"password": PASSWORD},
        headers={"Origin": ORIGIN, "X-Login-Intent": "login"},
    )
    assert response.status_code == 200, response.text
    client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]})


@dataclass(frozen=True)
class OfflineEnvironment:
    root: Path

    @property
    def runs(self):
        return self.root / "runs.db"

    @property
    def jobs(self):
        return self.root / "jobs.db"

    def configure(self, monkeypatch):
        import agent.main_agent as runtime
        from api import server

        monkeypatch.setenv("APP_ORIGIN", ORIGIN)
        monkeypatch.setenv("APP_BIND_HOST", "127.0.0.1")
        monkeypatch.setenv("APP_BIND_PORT", "8000")
        monkeypatch.setenv("APP_SESSION_SECONDS", "43200")
        monkeypatch.setenv("APP_ACCESS_PASSWORD", PASSWORD)
        monkeypatch.setattr(runtime, "checkpoint_db_path", self.root / "checkpoints.db")
        monkeypatch.setattr(runtime, "project_root_path", self.root)
        monkeypatch.setattr(server, "run_db_path", self.runs)
        monkeypatch.setattr(server, "output_dir", self.root / "output")
        monkeypatch.setattr(server, "updated_dir", self.root / "updated")

    async def migrate(self):
        lease = DataLease(self.root)
        try:
            await migrate(self.root, self.root)
        finally:
            lease.close()

    @asynccontextmanager
    async def client(self):
        from api import server

        async with server.lifespan(server.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url=ORIGIN) as client:
                await login_async(client)
                yield client


def legacy_snapshot(source, directory, version):
    """Build a genuine pre-security schema and copy only its historical columns."""
    assert version in (1, 2, 3, 4, 6)
    directory.mkdir()
    path = directory / "runs.db"
    with closing(sqlite3.connect(source)) as current, closing(sqlite3.connect(path)) as old:
        for number in [6] if version == 6 else range(1, version + 1):
            old.executescript(
                (Path(__file__).parent / "fixtures" / f"legacy-v{number}.sql").read_text(encoding="utf-8")
            )
        for (table,) in old.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchall():
            columns = [row[1] for row in old.execute(f"PRAGMA table_info({table})")]
            names = ",".join(columns)
            rows = current.execute(f"SELECT {names} FROM {table}").fetchall()
            old.executemany(f"INSERT INTO {table} ({names}) VALUES ({','.join('?' for _ in columns)})", rows)
        old.commit()
    return OfflineEnvironment(directory)


def bind_research_contract(context):
    from agent.runtime.contracts import bind_contract

    return bind_contract(context)
