"""Per-run context shared by async execution and its synchronous tools."""

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from time import monotonic


class RunStopped(RuntimeError):
    """Execution ended; neither new actions nor late results are allowed."""


@dataclass
class RunContext:
    run_id: str
    thread_id: str
    work_dir: Path
    deadline: datetime | None = None
    reference_time: datetime = field(default_factory=lambda: datetime.now().astimezone())
    unavailable_agents: dict[str, str] = field(default_factory=dict, repr=False)
    deadline_clock: float | None = None
    stop_status: str | None = None
    event_sink: object | None = field(default=None, repr=False)
    hook_failures: list[str] = field(default_factory=list, repr=False)
    policy: object | None = field(default=None, repr=False)
    skill_dir: Path | None = None
    skill_ids: tuple[str, ...] = ()
    business_mode: str = "ordinary"
    required_sources: tuple[str, ...] = ()
    required_uploads: tuple[str, ...] = ()
    workflow: dict | None = field(default=None, repr=False)
    upload_reads: dict = field(default_factory=dict, repr=False)
    upload_coverage: dict = field(default_factory=dict, repr=False)
    skill_states: set = field(default_factory=set, repr=False)
    skill_reads: dict = field(default_factory=dict, repr=False)
    skill_bodies: dict = field(default_factory=dict, repr=False)
    search_calls: set[tuple[str, str]] = field(default_factory=set, repr=False)
    search_tasks: dict = field(default_factory=dict, repr=False)
    search_finalization_announced: bool = False
    answer_repair_attempted: bool = False
    approved_internal: dict = field(default_factory=dict, repr=False)
    internal_result_version: int = 0
    user_query: str = field(default="", repr=False)
    budget_lock: object = field(default_factory=RLock, repr=False)
    delegation_calls: dict[str, list[dict]] = field(default_factory=dict, repr=False)
    call_counts: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(("main_model", "sub_model", "review", "tool", "retry"), 0)
    )

    def stop(self, status):
        with self.budget_lock:
            if self.stop_status is None:
                self.stop_status = status
            return self.stop_status

    def remaining(self):
        if self.deadline_clock is not None:
            return self.deadline_clock - monotonic()
        if self.deadline is not None:
            return (self.deadline - datetime.now(UTC)).total_seconds()
        return None

    def check_active(self):
        with self.budget_lock:
            remaining = self.remaining()
            if self.stop_status is None and remaining is not None and remaining <= 0:
                self.stop_status = "timed_out"
            if self.stop_status is not None:
                raise RunStopped(f"Run has ended: {self.stop_status}")


_current_run: ContextVar[RunContext | None] = ContextVar("current_run", default=None)


def get_run_context() -> RunContext | None:
    return _current_run.get()


def set_run_context(context: RunContext) -> Token:
    return _current_run.set(context)


def reset_run_context(token: Token) -> None:
    _current_run.reset(token)
