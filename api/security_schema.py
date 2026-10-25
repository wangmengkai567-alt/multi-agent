"""Shared schema and identity checks; runtime never assigns historical ownership."""

from api.security import MIGRATION_REQUIRED

RUN_SCHEMA = 8
JOB_SCHEMA = 1


async def identity(connection, version, *, require_ready=True):
    async with connection.execute("PRAGMA user_version") as cursor:
        if (await cursor.fetchone())[0] != version:
            raise RuntimeError(MIGRATION_REQUIRED)
    async with connection.execute(
        "SELECT owner_id,migration_id,migration_complete FROM security_identity WHERE singleton=1"
    ) as cursor:
        row = await cursor.fetchone()
    if not row or not row["owner_id"] or not row["migration_id"]:
        raise RuntimeError(MIGRATION_REQUIRED)
    if require_ready and row["migration_complete"] != 1:
        raise RuntimeError(MIGRATION_REQUIRED)
    return {"owner_id": row["owner_id"], "migration_id": row["migration_id"]}


async def verify_jobs(connection, store, *, require_ready=True):
    record = await identity(connection, JOB_SCHEMA, require_ready=require_ready)
    if record != {"owner_id": store.owner_id, "migration_id": store.migration_id}:
        raise RuntimeError("Run/job security identities differ; complete the offline migration")
    async with connection.execute("SELECT * FROM jobs") as cursor:
        jobs = await cursor.fetchall()
    for job in jobs:
        run = await store.get(job["run_id"])
        if job["resource_owner_id"] != store.owner_id or not run or run["thread_id"] != job["thread_id"]:
            raise RuntimeError("Job ownership does not match its parent run")
        if job["execution_run_id"]:
            execution = await store.get(job["execution_run_id"])
            if not execution or execution["thread_id"] != job["task_id"]:
                raise RuntimeError("Job execution thread ownership is inconsistent")
    async with connection.execute("SELECT * FROM job_subscriptions") as cursor:
        subscriptions = await cursor.fetchall()
    by_id = {job["task_id"]: job for job in jobs}
    async with store.connection.execute("SELECT run_id,thread_id,workspace FROM runs") as cursor:
        runs = await cursor.fetchall()
    for run in runs:
        job = by_id.get(run["thread_id"])
        expected = (
            f"background-jobs/{job['task_id']}"
            if job and job["kind"] == "research"
            else f"session_{run['thread_id']}/run_{run['run_id']}"
        )
        if run["workspace"] != expected:
            raise RuntimeError("Run workspace ownership is inconsistent; complete the offline migration")
    for subscription in subscriptions:
        job = by_id.get(subscription["task_id"])
        run = await store.get(subscription["run_id"])
        if not job or not run or run["thread_id"] != job["thread_id"]:
            raise RuntimeError("Job subscription ownership is inconsistent")
