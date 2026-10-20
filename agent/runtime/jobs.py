"""Durable read-only jobs with idempotent submission, ownership, recovery and explicit states."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import aiosqlite

from agent.runtime.events import redact_text

TERMINAL = frozenset({"completed", "failed", "cancelled"})


def now():
    return datetime.now(UTC).isoformat()


class InputRequired(Exception):
    """A registered handler requests human input; it must not approve itself."""


class JobFailed(RuntimeError):
    def __init__(self, error, partial_result=None):
        super().__init__(error)
        self.partial_result = partial_result


class JobService:
    def __init__(self, connection, handlers, directory):
        self.connection, self.handlers, self.directory = connection, handlers, Path(directory)
        self.lock, self.tasks, self.closing = asyncio.Lock(), {}, False
        self.slots = asyncio.Semaphore(2)
        self.owner = str(uuid4())
        self.contexts = {}
        self.closed = False
        self.close_lock = asyncio.Lock()

    @classmethod
    async def open(cls, path, handlers, directory):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(path)
        connection.row_factory = aiosqlite.Row
        await connection.execute("""CREATE TABLE IF NOT EXISTS jobs (
            task_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, run_id TEXT NOT NULL,
            kind TEXT NOT NULL, request_key TEXT NOT NULL, payload TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('working','completed','failed','cancelled','input_required')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, result TEXT, error TEXT,
            execution_run_id TEXT, resume_input TEXT,
            UNIQUE(thread_id,request_key))""")
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS job_subscriptions (run_id TEXT NOT NULL,task_id TEXT NOT NULL,PRIMARY KEY(run_id,task_id))"
        )
        async with connection.execute("PRAGMA table_info(jobs)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        for column in ("owner", "lease_until"):
            if column not in columns:
                await connection.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
        if "recovery_count" not in columns:
            await connection.execute("ALTER TABLE jobs ADD COLUMN recovery_count INTEGER NOT NULL DEFAULT 0")
        await connection.commit()
        return cls(connection, handlers, directory)

    @staticmethod
    def decode(row):
        if row is None:
            return None
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        value["result"] = json.loads(value["result"]) if value["result"] else None
        value["poll_after_seconds"] = 3 if value["status"] == "working" else 0
        return value

    async def get(self, task_id, thread_id):
        async with self.lock:
            async with self.connection.execute(
                "SELECT * FROM jobs WHERE task_id=? AND thread_id=?", (task_id, thread_id)
            ) as cursor:
                return self.decode(await cursor.fetchone())

    async def list_run(self, run_id):
        async with self.lock:
            async with self.connection.execute(
                "SELECT * FROM jobs WHERE run_id=? OR task_id IN (SELECT task_id FROM job_subscriptions WHERE run_id=?) ORDER BY created_at",
                (run_id, run_id),
            ) as cursor:
                return [self.decode(row) for row in await cursor.fetchall()]

    async def subscribe(self, task_id, run_id):
        async with self.lock:
            await self.connection.execute("INSERT OR IGNORE INTO job_subscriptions VALUES(?,?)", (run_id, task_id))
            await self.connection.commit()

    async def list_thread(self, thread_id):
        async with self.lock:
            async with self.connection.execute(
                "SELECT * FROM jobs WHERE thread_id=? ORDER BY created_at DESC LIMIT 20", (thread_id,)
            ) as cursor:
                return [self.decode(row) for row in await cursor.fetchall()]

    async def submit(self, thread_id, run_id, kind, payload, request_key, seconds):
        if self.closing or kind not in self.handlers:
            raise ValueError("Background job handler unavailable")
        from datetime import timedelta

        task_id = str(uuid4())
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        async with self.lock:
            await self.connection.execute(
                """INSERT OR IGNORE INTO jobs
                (task_id,thread_id,run_id,kind,request_key,payload,status,created_at,updated_at,expires_at)
                VALUES(?,?,?,?,?,?,'working',?,?,?)""",
                (
                    task_id,
                    thread_id,
                    run_id,
                    kind,
                    request_key,
                    canonical,
                    now(),
                    now(),
                    (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(),
                ),
            )
            await self.connection.commit()
            async with self.connection.execute(
                "SELECT * FROM jobs WHERE thread_id=? AND request_key=?", (thread_id, request_key)
            ) as cursor:
                job = self.decode(await cursor.fetchone())
        if job["payload"] != payload or job["kind"] != kind:
            raise ValueError("幂等键已用于不同任务")
        if job["status"] == "working":
            self.schedule(job)
        return job

    def schedule(self, job, *, recovering=False):
        if self.closing or job["task_id"] in self.tasks:
            return
        task = asyncio.create_task(self.run_slot(job, recovering=recovering), name="persistent-job:" + job["task_id"])
        self.tasks[job["task_id"]] = task
        task.add_done_callback(lambda t: self.tasks.pop(job["task_id"], None))

    async def run_slot(self, job, *, recovering=False):
        async with self.slots:
            while not self.closing:
                current = await self.get(job["task_id"], job["thread_id"])
                if current is None or current["status"] != "working":
                    return
                lease = current.get("lease_until")
                if current.get("owner") and current["owner"] != self.owner and lease:
                    wait = (datetime.fromisoformat(lease) - datetime.now(UTC)).total_seconds()
                    if wait > 0:
                        await asyncio.sleep(min(wait, 5))
                        continue
                async with self.lock:
                    cursor = await self.connection.execute(
                        """UPDATE jobs SET owner=?,lease_until=?
                        WHERE task_id=? AND status='working' AND (owner IS NULL OR owner=? OR lease_until<=?)""",
                        (
                            self.owner,
                            (datetime.now(UTC) + timedelta(seconds=15)).isoformat(),
                            job["task_id"],
                            self.owner,
                            now(),
                        ),
                    )
                    claimed = cursor.rowcount == 1
                    await cursor.close()
                    await self.connection.commit()
                if not claimed:
                    await asyncio.sleep(0.05)
                    continue
                if recovering:
                    async with self.lock:
                        await self.connection.execute(
                            "UPDATE jobs SET recovery_count=recovery_count+1 WHERE task_id=? AND owner=?",
                            (job["task_id"], self.owner),
                        )
                        await self.connection.commit()
                    current["recovery_count"] = current.get("recovery_count", 0) + 1
                heartbeat = asyncio.create_task(self.heartbeat(job["task_id"]))
                try:
                    await self.execute(current)
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                return

    async def heartbeat(self, task_id):
        while True:
            await asyncio.sleep(5)
            async with self.lock:
                await self.connection.execute(
                    "UPDATE jobs SET lease_until=? WHERE task_id=? AND owner=? AND status='working'",
                    ((datetime.now(UTC) + timedelta(seconds=15)).isoformat(), task_id, self.owner),
                )
                await self.connection.commit()

    async def recover(self):
        async with self.lock:
            async with self.connection.execute("SELECT * FROM jobs WHERE status='working'") as cursor:
                jobs = [self.decode(row) for row in await cursor.fetchall()]
        for job in jobs:
            self.schedule(job, recovering=True)

    async def transition(self, task_id, status, *, result=None, error=None, expected="working", expected_owner=None):
        if status not in TERMINAL | {"working", "input_required"}:
            raise ValueError("Invalid job state")
        async with self.lock:
            await self.connection.execute(
                """UPDATE jobs SET status=?,result=?,error=?,updated_at=?,owner=NULL,lease_until=NULL
                WHERE task_id=? AND status=? AND (? IS NULL OR owner=?)""",
                (
                    status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    redact_text(error) if error else None,
                    now(),
                    task_id,
                    expected,
                    expected_owner,
                    expected_owner,
                ),
            )
            await self.connection.commit()

    async def execute(self, job):
        task_id = job["task_id"]
        try:
            remaining = (datetime.fromisoformat(job["expires_at"]) - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                await self.transition(task_id, "failed", expected_owner=self.owner, error="任务总时长预算耗尽")
                return
            if job.get("recovery_count", 0) > 3:
                await self.transition(task_id, "failed", expected_owner=self.owner, error="任务恢复预算耗尽")
                return
            async with self.lock:
                await self.connection.execute(
                    "UPDATE jobs SET attempts=attempts+1,updated_at=? WHERE task_id=? AND status='working'",
                    (now(), task_id),
                )
                await self.connection.commit()
            async with asyncio.timeout(remaining):
                result = await self.handlers[job["kind"]](job)
            if (datetime.fromisoformat(job["expires_at"]) - datetime.now(UTC)).total_seconds() <= 0:
                await self.transition(
                    task_id, "failed", expected_owner=self.owner, error="任务总时长预算耗尽，未采纳迟到结果"
                )
            elif result is None:
                await self.transition(task_id, "failed", expected_owner=self.owner, error="任务未返回有效结果")
            else:
                await self.transition(task_id, "completed", expected_owner=self.owner, result=result)
        except InputRequired as error:
            await self.transition(task_id, "input_required", expected_owner=self.owner, error=str(error))
        except asyncio.CancelledError:
            # Shutdown preserves durable working state; explicit cancellation was committed first.
            raise
        except TimeoutError:
            await self.transition(task_id, "failed", expected_owner=self.owner, error="任务总时长预算耗尽")
        except Exception as error:
            await self.transition(
                task_id,
                "failed",
                expected_owner=self.owner,
                result=getattr(error, "partial_result", None),
                error=str(error) or type(error).__name__,
            )

    async def cancel(self, task_id, thread_id):
        job = await self.get(task_id, thread_id)
        if job is None:
            return None
        if job["status"] not in TERMINAL:
            await self.transition(task_id, "cancelled", error="用户取消后台任务", expected=job["status"])
            task = self.tasks.get(task_id)
            if task:
                task.cancel()
                await asyncio.sleep(0)
        return await self.get(task_id, thread_id)

    async def resume(self, task_id, thread_id, user_input):
        job = await self.get(task_id, thread_id)
        if job is None or job["status"] != "input_required":
            raise ValueError("任务没有等待输入")
        paused = datetime.now(UTC) - datetime.fromisoformat(job["updated_at"])
        expires = datetime.fromisoformat(job["expires_at"]) + paused
        async with self.lock:
            await self.connection.execute(
                "UPDATE jobs SET status='working',resume_input=?,updated_at=?,expires_at=? WHERE task_id=? AND status='input_required'",
                (user_input, now(), expires.isoformat(), task_id),
            )
            await self.connection.commit()
        job = await self.get(task_id, thread_id)
        current = self.tasks.get(task_id)
        if current:
            await asyncio.gather(current, return_exceptions=True)
        self.schedule(job)
        return job

    async def close(self):
        async with self.close_lock:
            if self.closed:
                return
            await self._close()
            self.closed = True

    async def _close(self):
        self.closing = True
        for context in self.contexts.values():
            context.stop("interrupted")
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=5)
        async with self.lock:
            await self.connection.execute("UPDATE jobs SET owner=NULL,lease_until=NULL WHERE owner=?", (self.owner,))
            await self.connection.commit()
        await self.connection.close()
