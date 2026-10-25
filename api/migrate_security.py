"""Explicit offline migration. Back up both databases before changing either one."""

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import aiosqlite

from api.run_store import RunStore
from api.security import DataLease
from api.security_schema import JOB_SCHEMA, RUN_SCHEMA, verify_jobs
from utils.secure_paths import confined_path, resource_path, validate_identifier

ROOT = Path(__file__).resolve().parent.parent


def backup_databases(data):
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    directory = data / ("security-backup-" + stamp)
    directory.mkdir()
    manifest = {}
    for name in ("runs.db", "jobs.db"):
        source = data / name
        manifest[name] = source.exists()
        if source.exists():
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
                with closing(sqlite3.connect(directory / name)) as copied:
                    original.backup(copied)
    (directory / "manifest.json").write_bytes(
        (json.dumps(manifest, indent=2).replace("\n", "\r\n") + "\r\n").encode("utf-8")
    )
    return directory


def migrate_jobs(path, owner_id, migration_id):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, JOB_SCHEMA}:
            raise RuntimeError("Unsupported jobs schema; databases were backed up and retained")
        connection.execute("BEGIN IMMEDIATE")
        if version == 0:
            connection.execute("""CREATE TABLE IF NOT EXISTS jobs (
                task_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, run_id TEXT NOT NULL,
                kind TEXT NOT NULL, request_key TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('working','completed','failed','cancelled','input_required')),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, result TEXT, error TEXT,
                execution_run_id TEXT, resume_input TEXT, UNIQUE(thread_id,request_key))""")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS job_subscriptions(run_id TEXT NOT NULL,task_id TEXT NOT NULL,PRIMARY KEY(run_id,task_id))"
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            additions = {
                "owner": "TEXT",
                "lease_until": "TEXT",
                "recovery_count": "INTEGER NOT NULL DEFAULT 0",
                "resource_owner_id": "TEXT",
            }
            for column, sql_type in additions.items():
                if column not in columns:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {column} {sql_type}")
            connection.execute(
                "CREATE TABLE security_identity(singleton INTEGER PRIMARY KEY CHECK(singleton=1),owner_id TEXT NOT NULL UNIQUE,migration_id TEXT NOT NULL,migration_complete INTEGER NOT NULL DEFAULT 0)"
            )
            connection.execute(
                "INSERT INTO security_identity(singleton,owner_id,migration_id) VALUES(1,?,?)", (owner_id, migration_id)
            )
            connection.execute("UPDATE jobs SET resource_owner_id=?", (owner_id,))
            connection.execute("""CREATE TRIGGER require_job_owner BEFORE INSERT ON jobs
                WHEN NEW.resource_owner_id IS NULL OR NEW.resource_owner_id != (SELECT owner_id FROM security_identity WHERE singleton=1)
                BEGIN SELECT RAISE(ABORT,'Job resource owner required'); END""")
            connection.execute("""CREATE TRIGGER immutable_job_identity BEFORE UPDATE OF resource_owner_id,thread_id,run_id,task_id ON jobs
                BEGIN SELECT RAISE(ABORT,'Job ownership is immutable'); END""")
            connection.execute(f"PRAGMA user_version={JOB_SCHEMA}")
        else:
            row = connection.execute("SELECT owner_id,migration_id FROM security_identity WHERE singleton=1").fetchone()
            if not row or tuple(row) != (owner_id, migration_id):
                raise RuntimeError("Existing jobs database belongs to a different security migration")
            connection.execute("UPDATE security_identity SET migration_complete=0")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


async def migrate(data, root):
    backup = backup_databases(data)
    print(f"SQLite backups: {backup}")
    connection = await aiosqlite.connect(data / "runs.db")
    connection.row_factory = aiosqlite.Row
    try:
        store = RunStore(connection)
        async with connection.execute("PRAGMA user_version") as cursor:
            version = (await cursor.fetchone())[0]
        if version < RUN_SCHEMA:
            await store._migrate_legacy()
            owner_id, migration_id = str(uuid4()), str(uuid4())
            await connection.execute("BEGIN IMMEDIATE")
            try:
                await connection.execute("""CREATE TABLE security_identity(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),owner_id TEXT NOT NULL UNIQUE,
                    migration_id TEXT NOT NULL,password_salt TEXT,password_hash TEXT,
                    migration_complete INTEGER NOT NULL DEFAULT 0)""")
                await connection.execute(
                    "INSERT INTO security_identity(singleton,owner_id,migration_id) VALUES(1,?,?)",
                    (owner_id, migration_id),
                )
                await connection.execute("""CREATE TABLE threads(thread_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES security_identity(owner_id),created_at TEXT NOT NULL)""")
                await connection.execute(
                    "INSERT INTO threads SELECT DISTINCT thread_id,?,? FROM runs",
                    (owner_id, datetime.now(UTC).isoformat()),
                )
                await connection.execute("""CREATE TRIGGER immutable_thread_owner BEFORE UPDATE OF owner_id,thread_id ON threads
                    BEGIN SELECT RAISE(ABORT,'Thread ownership is immutable'); END""")
                await connection.execute("ALTER TABLE runs ADD COLUMN workspace TEXT")
                async with connection.execute("SELECT run_id,thread_id FROM runs") as cursor:
                    runs = await cursor.fetchall()
                for run in runs:
                    validate_identifier(run["thread_id"])
                    validate_identifier(run["run_id"])
                    await connection.execute(
                        "UPDATE runs SET workspace=? WHERE run_id=?",
                        (f"session_{run['thread_id']}/run_{run['run_id']}", run["run_id"]),
                    )
                uploads = root / "updated"
                if uploads.is_dir():
                    for directory in uploads.iterdir():
                        if directory.name.startswith("session_") and directory.is_dir():
                            confined_path(directory.name, uploads)
                            thread_id = validate_identifier(directory.name.removeprefix("session_"))
                            await connection.execute(
                                "INSERT OR IGNORE INTO threads VALUES(?,?,?)",
                                (thread_id, owner_id, datetime.now(UTC).isoformat()),
                            )
                await connection.execute("""CREATE TABLE auth_sessions(token_hash TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL REFERENCES security_identity(owner_id),csrf TEXT NOT NULL,expires_at INTEGER NOT NULL)""")
                await connection.execute(
                    "CREATE TABLE login_throttle(singleton INTEGER PRIMARY KEY CHECK(singleton=1),started INTEGER NOT NULL,attempts INTEGER NOT NULL)"
                )
                await connection.execute("""CREATE TRIGGER require_run_thread BEFORE INSERT ON runs
                    WHEN NOT EXISTS(SELECT 1 FROM threads WHERE thread_id=NEW.thread_id)
                    BEGIN SELECT RAISE(ABORT,'Owned thread required'); END""")
                await connection.execute("""CREATE TRIGGER immutable_run_identity BEFORE UPDATE OF thread_id,run_id ON runs
                    BEGIN SELECT RAISE(ABORT,'Run ownership is immutable'); END""")
                await connection.execute(f"PRAGMA user_version={RUN_SCHEMA}")
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        elif version == RUN_SCHEMA:
            async with connection.execute(
                "SELECT owner_id,migration_id FROM security_identity WHERE singleton=1"
            ) as cursor:
                row = await cursor.fetchone()
            owner_id, migration_id = row["owner_id"], row["migration_id"]
            await connection.execute("UPDATE security_identity SET migration_complete=0")
            await connection.commit()
        else:
            raise RuntimeError("Unsupported runs schema")
    finally:
        await connection.close()
    # Separate transactions, intentionally retryable after an interrupted migration.
    migrate_jobs(data / "jobs.db", owner_id, migration_id)
    connection = sqlite3.connect(data / "runs.db")
    jobs = sqlite3.connect(data / "jobs.db")
    jobs.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        for job in jobs.execute("SELECT * FROM jobs WHERE kind='research'"):
            validate_identifier(job["task_id"])
            connection.execute(
                "UPDATE runs SET workspace=? WHERE thread_id=?", (f"background-jobs/{job['task_id']}", job["task_id"])
            )
        connection.commit()
        jobs.execute("BEGIN IMMEDIATE")
        for job in jobs.execute("SELECT * FROM jobs"):
            row = connection.execute("SELECT thread_id,workspace FROM runs WHERE run_id=?", (job["run_id"],)).fetchone()
            if not row or row[0] != job["thread_id"]:
                raise RuntimeError("Historical job has no matching parent run; restore/repair from the backups")
            work_dir = resource_path(row[1], root / "output")
            payload = json.loads(job["payload"])
            if job["kind"] == "pdf_conversion" and "work_dir" in payload:
                if confined_path(payload.pop("work_dir"), root / "output") != work_dir:
                    raise RuntimeError("Historical conversion output directory is not owned by its parent run")
                resource_path(payload["output_relative"], work_dir)
                payload["conversion_version"] = 1
            result = json.loads(job["result"]) if job["result"] else None
            if result:
                for artifact in result.get("artifacts", []):
                    if "path" in artifact:
                        file = confined_path(artifact.pop("path"), work_dir)
                        artifact["relative_path"] = file.relative_to(work_dir).as_posix()
                    resource_path(artifact["relative_path"], work_dir)
            jobs.execute(
                "UPDATE jobs SET payload=?,result=? WHERE task_id=?",
                (
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    job["task_id"],
                ),
            )
        jobs.commit()
    except BaseException:
        connection.rollback()
        jobs.rollback()
        raise
    finally:
        connection.close()
        jobs.close()
    connection = await aiosqlite.connect(data / "runs.db")
    connection.row_factory = aiosqlite.Row
    store = RunStore(connection)
    store.owner_id, store.migration_id = owner_id, migration_id
    jobs = await aiosqlite.connect(data / "jobs.db")
    jobs.row_factory = aiosqlite.Row
    try:
        await store.verify_ownership()
        await verify_jobs(jobs, store, require_ready=False)
        async with connection.execute("SELECT run_id,thread_id,workspace FROM runs") as cursor:
            runs = [dict(row) for row in await cursor.fetchall()]
        async with connection.execute("SELECT thread_id FROM threads") as cursor:
            threads = [row[0] for row in await cursor.fetchall()]
        # Files are retained in place. Unmapped roots are recorded, never exposed by a global listing.
        report = {
            "owner_id": owner_id,
            "migration_id": migration_id,
            "runs": runs,
            "upload_threads": threads,
            "output_owner_id": owner_id,
            "unmapped_files_policy": "retain_in_place_without_HTTP_access",
        }
        (data / "security-resources.json").write_bytes(
            (json.dumps(report, ensure_ascii=False, indent=2).replace("\n", "\r\n") + "\r\n").encode("utf-8")
        )
        await connection.execute("""CREATE TRIGGER IF NOT EXISTS immutable_run_workspace BEFORE UPDATE OF workspace ON runs
            WHEN (SELECT migration_complete FROM security_identity WHERE singleton=1)=1
            BEGIN SELECT RAISE(ABORT,'Run workspace is immutable outside offline migration'); END""")
        await jobs.execute("UPDATE security_identity SET migration_complete=1")
        await jobs.commit()
        await connection.execute("UPDATE security_identity SET migration_complete=1")
        await connection.commit()
    finally:
        await jobs.close()
        await store.close()
    print("Ownership migration complete. Configure APP_ACCESS_PASSWORD before starting the service.")


def main():
    data = ROOT / "data"
    lease = DataLease(data)
    try:
        asyncio.run(migrate(data, ROOT))
    finally:
        lease.close()


if __name__ == "__main__":
    main()
