"""Versioned SQLite run records with atomic admission and terminal transitions."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import aiosqlite

ACTIVE = ("queued", "running")
TERMINAL = ("completed", "failed", "cancelled", "interrupted", "timed_out")


def _now():
    return datetime.now(UTC).isoformat()


class RunConflict(Exception):
    def __init__(self, run_id):
        self.run_id = run_id
        super().__init__(f"Session already has active run {run_id}")


class RunStore:
    def __init__(self, connection):
        self.connection = connection
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(path)
        connection.row_factory = aiosqlite.Row
        store = cls(connection)
        try:
            await store._migrate()
        except BaseException:
            await connection.close()
            raise
        return store

    async def _migrate(self):
        async with self._lock:
            async with self.connection.execute("PRAGMA user_version") as cursor:
                version = (await cursor.fetchone())[0]
            if version not in (0, 1, 2, 3, 4, 5):
                raise RuntimeError(f"Unsupported run database schema version: {version}")
            if version == 0:
                try:
                    await self.connection.executescript("""
                        BEGIN IMMEDIATE;
                        CREATE TABLE runs (
                            run_id TEXT PRIMARY KEY,
                            thread_id TEXT NOT NULL,
                            question TEXT NOT NULL,
                            status TEXT NOT NULL CHECK(status IN
                                ('queued','running','completed','failed','cancelled','interrupted')),
                            answer TEXT,
                            error TEXT,
                            created_at TEXT NOT NULL,
                            ended_at TEXT,
                            CHECK((status IN ('queued','running') AND ended_at IS NULL)
                                OR (status NOT IN ('queued','running') AND ended_at IS NOT NULL))
                        );
                        CREATE UNIQUE INDEX one_active_run_per_thread
                            ON runs(thread_id) WHERE status IN ('queued','running');
                        PRAGMA user_version = 1;
                        COMMIT;
                    """)
                except BaseException:
                    await self.connection.rollback()
                    raise

            if version < 2:
                try:
                    await self.connection.executescript("""
                        BEGIN IMMEDIATE;
                        CREATE TABLE events (
                            run_id TEXT NOT NULL REFERENCES runs(run_id),
                            event_seq INTEGER NOT NULL,
                            thread_id TEXT NOT NULL,
                            event TEXT NOT NULL,
                            call_id TEXT NOT NULL,
                            parent_call_id TEXT,
                            actor TEXT NOT NULL,
                            status TEXT NOT NULL,
                            timestamp TEXT NOT NULL,
                            message TEXT NOT NULL,
                            data TEXT NOT NULL,
                            PRIMARY KEY(run_id,event_seq)
                        );
                        PRAGMA user_version = 2;
                        COMMIT;
                    """)
                except BaseException:
                    await self.connection.rollback()
                    raise

            if version < 3:
                try:
                    await self.connection.executescript("""
                        BEGIN IMMEDIATE;
                        CREATE TABLE subtasks (
                            call_id TEXT PRIMARY KEY,
                            run_id TEXT NOT NULL REFERENCES runs(run_id),
                            thread_id TEXT NOT NULL,
                            request_call_id TEXT,
                            target TEXT NOT NULL,
                            display_name TEXT NOT NULL,
                            description TEXT NOT NULL,
                            description_length INTEGER NOT NULL,
                            description_sha256 TEXT NOT NULL,
                            status TEXT NOT NULL CHECK(status IN
                                ('queued','running','completed','failed','rejected','cancelled','interrupted')),
                            deadline TEXT,
                            created_at TEXT NOT NULL,
                            ended_at TEXT,
                            result TEXT,
                            error TEXT,
                            check_status TEXT NOT NULL,
                            CHECK((status IN ('queued','running') AND ended_at IS NULL)
                                OR (status NOT IN ('queued','running') AND ended_at IS NOT NULL))
                        );
                        CREATE INDEX subtasks_by_run ON subtasks(run_id,created_at);
                        PRAGMA user_version = 3;
                        COMMIT;
                    """)
                except BaseException:
                    await self.connection.rollback()
                    raise

            if version < 4:
                # Keep the original source enum for historical database compatibility.
                # record_result rejects retired sources for all new writes.
                try:
                    await self.connection.executescript("""
                        BEGIN IMMEDIATE;
                        CREATE TABLE results (
                            result_id TEXT PRIMARY KEY,
                            run_id TEXT NOT NULL REFERENCES runs(run_id),
                            thread_id TEXT NOT NULL,
                            call_id TEXT NOT NULL REFERENCES subtasks(call_id),
                            attempt INTEGER NOT NULL CHECK(attempt >= 0),
                            source TEXT NOT NULL CHECK(source IN ('db','knowledge','search')),
                            created_at TEXT NOT NULL,
                            query_object TEXT NOT NULL,
                            time_range TEXT NOT NULL,
                            statistical_scope TEXT NOT NULL,
                            raw_text TEXT,
                            raw_path TEXT,
                            raw_sha256 TEXT NOT NULL,
                            summary TEXT,
                            check_status TEXT NOT NULL,
                            review TEXT NOT NULL,
                            published INTEGER NOT NULL CHECK(published IN (0,1)),
                            UNIQUE(call_id,attempt),
                            CHECK((raw_text IS NULL) != (raw_path IS NULL)),
                            CHECK(published=0 OR (check_status='passed' AND summary IS NOT NULL))
                        );
                        CREATE INDEX results_by_run ON results(run_id,source,created_at);
                        PRAGMA user_version = 4;
                        COMMIT;
                    """)
                except BaseException:
                    await self.connection.rollback()
                    raise

            if version < 5:
                try:
                    await self.connection.executescript("""
                        BEGIN IMMEDIATE;
                        CREATE TABLE runs_v5 (
                            run_id TEXT PRIMARY KEY,
                            thread_id TEXT NOT NULL,
                            question TEXT NOT NULL,
                            status TEXT NOT NULL CHECK(status IN
                                ('queued','running','completed','failed','cancelled','interrupted','timed_out')),
                            answer TEXT,
                            error TEXT,
                            created_at TEXT NOT NULL,
                            ended_at TEXT,
                            CHECK((status IN ('queued','running') AND ended_at IS NULL)
                                OR (status NOT IN ('queued','running') AND ended_at IS NOT NULL))
                        );
                        INSERT INTO runs_v5 SELECT * FROM runs;
                        DROP TABLE runs;
                        ALTER TABLE runs_v5 RENAME TO runs;
                        CREATE UNIQUE INDEX one_active_run_per_thread
                            ON runs(thread_id) WHERE status IN ('queued','running');
                        PRAGMA user_version = 5;
                        COMMIT;
                    """)
                except BaseException:
                    await self.connection.rollback()
                    raise

    @staticmethod
    def _decode_result(row):
        if row is None:
            return None
        record = dict(row)
        for key in ("statistical_scope", "review"):
            record[key] = json.loads(record[key])
        record["published"] = bool(record["published"])
        return record

    async def record_result(self, record, event):
        """Append an immutable attempt and event atomically; late output stays unpublished."""
        if record.get("source") not in ("db", "search"):
            raise ValueError("Unsupported active result source")
        async with self._lock:
            try:
                await self.connection.execute("BEGIN IMMEDIATE")
                run = await self._get(record["run_id"])
                async with self.connection.execute(
                    "SELECT * FROM subtasks WHERE call_id=?", (record["call_id"],)
                ) as cursor:
                    task = await cursor.fetchone()
                if (
                    run is None
                    or task is None
                    or run["thread_id"] != record["thread_id"]
                    or any(task[k] != record[k] for k in ("run_id", "thread_id"))
                    or task["target"] != record["source"]
                    or any(event[k] != record[k] for k in ("run_id", "thread_id", "call_id"))
                ):
                    raise ValueError("Result does not belong to the subtask/run/session")
                if record["review"]["verdict"] != record["check_status"]:
                    raise ValueError("Result review status mismatch")
                if record["published"] and (record["check_status"] != "passed" or not record["summary"]):
                    raise ValueError("Only checked results may publish a summary")
                record = dict(record)
                if run["status"] not in ACTIVE or task["status"] not in ACTIVE:
                    record.update(published=False, summary=None)
                columns = tuple(record)
                values = [
                    json.dumps(record[k], ensure_ascii=False) if k in ("review", "statistical_scope") else record[k]
                    for k in columns
                ]
                await self.connection.execute(
                    "INSERT INTO results (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")",
                    values,
                )
                saved = await self._append_event(dict(event, data=dict(event["data"], published=record["published"])))
                await self.connection.commit()
                return saved
            except BaseException:
                await self.connection.rollback()
                raise

    async def _list_results(self, run_id, thread_id=None, usable_only=False):
        sql = "SELECT r.* FROM results r JOIN subtasks s ON s.call_id=r.call_id WHERE r.run_id=?"
        args = [run_id]
        if thread_id is not None:
            sql += " AND r.thread_id=?"
            args.append(thread_id)
        if usable_only:
            sql += " AND r.published=1 AND r.check_status='passed' AND s.status='completed' AND s.check_status='passed'"
        async with self.connection.execute(sql + " ORDER BY r.created_at,r.result_id", args) as cursor:
            return [self._decode_result(row) for row in await cursor.fetchall()]

    async def list_results(self, run_id, *, thread_id, usable_only=False):
        async with self._lock:
            return await self._list_results(run_id, thread_id, usable_only)

    async def get_result(self, result_id, run_id, thread_id):
        async with self._lock:
            async with self.connection.execute(
                "SELECT * FROM results WHERE result_id=? AND run_id=? AND thread_id=?",
                (result_id, run_id, thread_id),
            ) as cursor:
                return self._decode_result(await cursor.fetchone())

    async def _list_subtasks(self, run_id):
        async with self.connection.execute(
            "SELECT * FROM subtasks WHERE run_id=? ORDER BY created_at,call_id", (run_id,)
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def list_subtasks(self, run_id):
        async with self._lock:
            return await self._list_subtasks(run_id)

    async def record_subtask(self, record, event):
        """Persist the lifecycle row and its event in one transaction."""
        async with self._lock:
            try:
                await self.connection.execute("BEGIN IMMEDIATE")
                run = await self._get(record["run_id"])
                if run is None or run["thread_id"] != record["thread_id"]:
                    raise ValueError("Subtask does not belong to the run/session")
                if (
                    event["run_id"] != record["run_id"]
                    or event["call_id"] != record["call_id"]
                    or event["thread_id"] != record["thread_id"]
                ):
                    raise ValueError("Subtask event identity mismatch")
                async with self.connection.execute(
                    "SELECT * FROM subtasks WHERE call_id=?", (record["call_id"],)
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing and any(
                    existing[key] != record[key]
                    for key in ("run_id", "thread_id", "target", "request_call_id", "description_sha256", "deadline")
                ):
                    raise ValueError("Subtask identity cannot be changed")
                if existing and existing["status"] not in ACTIVE:
                    await self.connection.rollback()
                    return None
                if run["status"] not in ACTIVE:
                    if not existing:
                        await self.connection.rollback()
                        return None
                    record = dict(
                        record,
                        status="cancelled" if run["status"] == "cancelled" else "interrupted",
                        result=None,
                        error="Parent run has already ended",
                        ended_at=_now(),
                    )
                    event = dict(
                        event,
                        status=record["status"],
                        event=f"subagent_{record['status']}",
                        message="主任务已结束，子任务不再发布结果",
                    )
                if existing is None:
                    if record["status"] != "queued":
                        raise ValueError("Subtask must be queued before execution")
                    columns = tuple(record)
                    await self.connection.execute(
                        "INSERT INTO subtasks ("
                        + ",".join(columns)
                        + ") VALUES ("
                        + ",".join("?" for _ in columns)
                        + ")",
                        tuple(record[column] for column in columns),
                    )
                else:
                    if record["status"] == "queued" or (
                        existing["status"] == "queued" and record["status"] == "completed"
                    ):
                        raise ValueError("Invalid subtask state transition")
                    await self.connection.execute(
                        "UPDATE subtasks SET status=?,result=?,error=?,ended_at=?,check_status=? WHERE call_id=?",
                        (
                            record["status"],
                            record["result"],
                            record["error"],
                            record["ended_at"],
                            record["check_status"],
                            record["call_id"],
                        ),
                    )
                saved = await self._append_event(event)
                await self.connection.commit()
                return saved
            except BaseException:
                await self.connection.rollback()
                raise

    async def _append_event(self, event):
        record = await self._get(event["run_id"])
        if record is None or record["thread_id"] != event["thread_id"]:
            raise ValueError("Event does not belong to an existing run/session")
        async with self.connection.execute(
            "SELECT COALESCE(MAX(event_seq),0)+1 FROM events WHERE run_id=?", (event["run_id"],)
        ) as cursor:
            seq = (await cursor.fetchone())[0]
        saved = dict(event, event_seq=seq)
        await self.connection.execute(
            """INSERT INTO events
               (run_id,event_seq,thread_id,event,call_id,parent_call_id,actor,status,timestamp,message,data)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                saved["run_id"],
                seq,
                saved["thread_id"],
                saved["event"],
                saved["call_id"],
                saved["parent_call_id"],
                saved["actor"],
                saved["status"],
                saved["timestamp"],
                saved["message"],
                json.dumps(saved["data"], ensure_ascii=False),
            ),
        )
        return saved

    async def append_event(self, event):
        async with self._lock:
            try:
                await self.connection.execute("BEGIN IMMEDIATE")
                saved = await self._append_event(event)
                await self.connection.commit()
                return saved
            except BaseException:
                await self.connection.rollback()
                raise

    async def _list_events(self, run_id):
        async with self.connection.execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY event_seq", (run_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row, data=json.loads(row["data"])) for row in rows]

    async def list_events(self, run_id: str, *, after_seq=0, limit=None):
        async with self._lock:
            async with self.connection.execute(
                "SELECT * FROM events WHERE run_id=? AND event_seq>? ORDER BY event_seq LIMIT ?",
                (run_id, after_seq, -1 if limit is None else limit),
            ) as cursor:
                return [dict(row, data=json.loads(row["data"])) for row in await cursor.fetchall()]

    async def thread_runs(self, thread_id):
        async with self._lock:
            async with self.connection.execute(
                "SELECT run_id FROM runs WHERE thread_id=? ORDER BY created_at,run_id", (thread_id,)
            ) as cursor:
                return [row["run_id"] for row in await cursor.fetchall()]

    async def history(self, run_id: str):
        async with self._lock:
            record = await self._get(run_id)
            return (
                {
                    "run": record,
                    "events": await self._list_events(run_id),
                    "subtasks": await self._list_subtasks(run_id),
                    "results": await self._list_results(run_id),
                }
                if record
                else None
            )

    async def list_active(self):
        async with self._lock:
            async with self.connection.execute("SELECT * FROM runs WHERE status IN ('queued','running')") as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def create(self, thread_id: str, question: str, *, run_id: str | None = None, event=None) -> dict:
        run_id = run_id or str(uuid4())
        record = dict(
            run_id=run_id,
            thread_id=thread_id,
            question=question,
            status="queued",
            answer=None,
            error=None,
            created_at=_now(),
            ended_at=None,
        )
        async with self._lock:
            try:
                await self.connection.execute(
                    "INSERT INTO runs (run_id,thread_id,question,status,created_at) VALUES (?,?,?,'queued',?)",
                    (run_id, thread_id, question, record["created_at"]),
                )
                if event is not None:
                    record["saved_event"] = await self._append_event(event)
                await self.connection.commit()
            except aiosqlite.IntegrityError:
                await self.connection.rollback()
                async with self.connection.execute(
                    "SELECT run_id FROM runs WHERE thread_id=? AND status IN ('queued','running')", (thread_id,)
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing:
                    raise RunConflict(existing["run_id"]) from None
                raise
            except BaseException:
                await self.connection.rollback()
                raise
            return record

    async def _get(self, run_id):
        async with self.connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def get(self, run_id: str) -> dict | None:
        async with self._lock:
            return await self._get(run_id)

    async def start(self, run_id: str, *, event=None):
        async with self._lock:
            try:
                cursor = await self.connection.execute(
                    "UPDATE runs SET status='running' WHERE run_id=? AND status='queued'", (run_id,)
                )
                changed = cursor.rowcount == 1
                await cursor.close()
                saved = await self._append_event(event) if changed and event is not None else None
                await self.connection.commit()
                return saved if event is not None else changed
            except BaseException:
                await self.connection.rollback()
                raise

    async def finish(self, run_id: str, status: str, *, answer=None, error=None, event=None):
        if status not in TERMINAL:
            raise ValueError(f"Not a terminal status: {status}")
        if status == "completed" and not isinstance(answer, str):
            raise ValueError("A completed run requires an answer")
        async with self._lock:
            try:
                cursor = await self.connection.execute(
                    """UPDATE runs SET status=?,answer=?,error=?,ended_at=?
                       WHERE run_id=? AND status IN ('queued','running')""",
                    (status, answer, error, _now(), run_id),
                )
                changed = cursor.rowcount == 1
                await cursor.close()
                if changed:
                    await self.connection.execute(
                        """UPDATE subtasks SET status=?,result=NULL,error=?,ended_at=?
                           WHERE run_id=? AND status IN ('queued','running')""",
                        (
                            "cancelled" if status == "cancelled" else "interrupted",
                            error or "Parent run ended",
                            _now(),
                            run_id,
                        ),
                    )
                saved = await self._append_event(event) if changed and event is not None else None
                await self.connection.commit()
                return saved if event is not None else changed
            except BaseException:
                await self.connection.rollback()
                raise

    async def interrupt_active(self, reason: str):
        """Settle active runs and children without replacing prior terminal output."""
        for record in await self.list_active():
            await self.finish(record["run_id"], "interrupted", error=reason)

    async def close(self):
        async with self._lock:
            await self.connection.close()
