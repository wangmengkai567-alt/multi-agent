"""Per-run context shared by async execution and its synchronous tools."""

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, RLock
from time import monotonic

from agent.runtime.errors import ExecutionTerminated


class RunStopped(ExecutionTerminated):
    """Execution ended; neither new actions nor late results are allowed."""


@dataclass
class RunContext:
    run_id: str
    thread_id: str
    work_dir: Path
    owner_id: str = field(default="", repr=False, kw_only=True)
    deadline: datetime | None = None
    reference_time: datetime = field(default_factory=lambda: datetime.now().astimezone())
    unavailable_agents: dict[str, str] = field(default_factory=dict, repr=False)
    deadline_clock: float | None = None
    stop_status: str | None = None
    terminal_error: ExecutionTerminated | None = field(default=None, repr=False)
    collection_states: dict = field(default_factory=dict, repr=False)
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
    answer_citation_repair_attempted: bool = False
    coverage: str = "unknown"
    query_version: int = 1
    research_scope_id: str = ""
    resume_source_run_id: str | None = None
    objects: dict = field(default_factory=dict, repr=False)
    requirement_links: dict = field(default_factory=dict, repr=False)
    requirements: dict = field(default_factory=dict, repr=False)
    coverage_rows: dict = field(default_factory=dict, repr=False)
    gaps: dict = field(default_factory=dict, repr=False)
    stop_event: object = field(default_factory=Event, repr=False)
    research_waiters: dict = field(default_factory=dict, repr=False)
    research_cache: dict = field(default_factory=dict, repr=False)
    evidence_snapshots: dict = field(default_factory=dict, repr=False)
    quote_registry: dict = field(default_factory=dict, repr=False)
    request_ledger: dict = field(default_factory=dict, repr=False)
    attempt_ledger: dict = field(default_factory=dict, repr=False)
    outbound: list = field(default_factory=list, repr=False)
    review_cache: dict = field(default_factory=dict, repr=False)
    revision_reservations: dict = field(default_factory=dict, repr=False)
    delivery_draft: dict | None = field(default=None, repr=False)
    original_call_ids: dict = field(default_factory=dict, repr=False)
    approved_internal: dict = field(default_factory=dict, repr=False)
    internal_result_version: int = 0
    user_query: str = field(default="", repr=False)
    input_text: str | None = field(default=None, repr=False)
    network_policy: str = "allowed"
    task_contract: object | None = field(default=None, repr=False)
    expert_contract: dict = field(default_factory=dict, repr=False)
    previous_run_id: str | None = None
    previous_answer: str = field(default="", repr=False)
    latest_message: str = field(default="", repr=False)
    tool_turns: dict = field(default_factory=dict, repr=False)
    turn_counts: dict = field(default_factory=dict, repr=False)
    task_scopes: dict = field(default_factory=dict, repr=False)
    call_labels: dict = field(default_factory=dict, repr=False)
    pending_jobs: dict = field(default_factory=dict, repr=False)
    job_service: object | None = field(default=None, repr=False)
    background_job: bool = False
    job_checkpoint_path: Path | None = field(default=None, repr=False)
    job_checkpoint_closed: bool = False
    collecting: bool = True
    resume_graph: bool = False
    partial_answer: bool = False
    accepted_plan: bool = False
    scheduler_ticket: str | None = None
    plan_version: int = 0
    plan_expansions: int = 0
    plan_closed: bool = False
    scheduler: object | None = field(default=None, repr=False)
    enforce_plan: bool = False
    execution_mode: str = "auto"
    budget_lock: object = field(default_factory=RLock, repr=False)
    delegation_calls: dict[str, list[dict]] = field(default_factory=dict, repr=False)
    call_counts: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(("main_model", "sub_model", "review", "tool", "retry"), 0)
    )

    def __post_init__(self):
        if not self.research_scope_id:
            self.research_scope_id = self.run_id
        if self.scheduler is None:
            from agent.runtime.policies import load_runtime_policy
            from agent.runtime.scheduler import ServiceScheduler

            self.scheduler = ServiceScheduler((self.policy or load_runtime_policy()).scheduler)

    @property
    def task_instruction(self):
        return self.task_contract.instruction_text if self.task_contract is not None else self.user_query

    def stop(self, status):
        with self.budget_lock:
            if self.stop_status is None:
                self.stop_status = status
                self.stop_event.set()
            return self.stop_status

    def terminate(self, error):
        with self.budget_lock:
            if self.terminal_error is None and self.stop_status is None:
                self.terminal_error = error
                self.stop("failed")

    def remaining(self):
        if self.deadline_clock is not None:
            return self.deadline_clock - monotonic()
        if self.deadline is not None:
            return (self.deadline - datetime.now(UTC)).total_seconds()
        return None

    def check_active(self):
        with self.budget_lock:
            if self.terminal_error is not None:
                raise self.terminal_error
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
