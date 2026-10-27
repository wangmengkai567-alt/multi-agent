"""Step 1 guards: explicit migration and actual browser authentication remain required."""

import httpx
import pytest
from runtime_fixtures import ORIGIN

from api.run_store import RunStore


@pytest.mark.asyncio
async def test_runtime_requires_explicit_temporary_database_migration(test_environment):
    with pytest.raises(RuntimeError, match="Security database migration required"):
        await RunStore.open(test_environment.runs)
    await test_environment.migrate()
    store = await RunStore.open(test_environment.runs)
    try:
        assert store.owner_id and store.migration_id
        assert test_environment.jobs.is_file()
        assert not await store.owns_thread("unowned")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_authenticated_fixture_keeps_cookie_origin_csrf_and_ownership_guards(authenticated_client):
    from api import server

    client = authenticated_client
    assert (await client.get("/api/auth/session")).status_code == 200
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url=ORIGIN) as anonymous:
        assert (await anonymous.get("/api/skills")).status_code == 401
    assert (await client.post("/api/threads", headers={"Origin": "https://foreign.example"})).status_code == 403
    assert (await client.post("/api/threads", headers={"X-CSRF-Token": "wrong-token"})).status_code == 403
    assert (await client.post("/api/task", json={"query": "denied", "thread_id": "unowned"})).status_code == 404
    created = await client.post("/api/threads")
    assert created.status_code == 200
    assert await server.app.state.run_store.owns_thread(created.json()["thread_id"])
    assert not server.app.state.agent_tasks
