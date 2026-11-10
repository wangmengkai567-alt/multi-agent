# 本轮共享状态：保存身份、工作目录、权限、预算、停止信号和各专家返回结果。
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
    # thread_id 标识可重复提问的会话；run_id 标识其中一次执行，work_dir 只属于这一轮。
    run_id: str
    thread_id: str
    work_dir: Path
    owner_id: str = field(default="", repr=False, kw_only=True)
    deadline: datetime | None = None
    reference_time: datetime = field(default_factory=lambda: datetime.now().astimezone())
    deadline_clock: float | None = None
    stop_status: str | None = None
    terminal_error: ExecutionTerminated | None = field(default=None, repr=False)
    event_sink: object | None = field(default=None, repr=False)
    hook_failures: list[str] = field(default_factory=list, repr=False)
    policy: object | None = field(default=None, repr=False)
    skill_dir: Path | None = None
    skill_ids: tuple[str, ...] = ()
    business_mode: str = "ordinary"
    required_sources: tuple[str, ...] = ()
    required_uploads: tuple[str, ...] = ()
    upload_reads: dict = field(default_factory=dict, repr=False)
    upload_coverage: dict = field(default_factory=dict, repr=False)
    document_coverage: dict = field(default_factory=dict, repr=False)
    skill_states: set = field(default_factory=set, repr=False)
    skill_reads: dict = field(default_factory=dict, repr=False)
    skill_versions: dict = field(default_factory=dict, repr=False)
    skill_bodies: dict = field(default_factory=dict, repr=False)
    skill_resources: dict = field(default_factory=dict, repr=False)
    coverage: str = "unknown"
    stop_event: object = field(default_factory=Event, repr=False)
    request_ledger: dict = field(default_factory=dict, repr=False)
    delivery_draft: dict | None = field(default=None, repr=False)
    original_call_ids: dict = field(default_factory=dict, repr=False)
    user_query: str = field(default="", repr=False)
    input_text: str | None = field(default=None, repr=False)
    network_policy: str = "allowed"
    task_contract: object | None = field(default=None, repr=False)
    expert_contract: dict = field(default_factory=dict, repr=False)
    tool_turns: dict = field(default_factory=dict, repr=False)
    turn_counts: dict = field(default_factory=dict, repr=False)
    call_labels: dict = field(default_factory=dict, repr=False)
    scheduler_ticket: str | None = None
    scheduler: object | None = field(default=None, repr=False)
    # 异步任务及同步工具线程共用可重入锁，统一保护预算和停止状态。
    budget_lock: object = field(default_factory=RLock, repr=False)
    task_rejections: list = field(default_factory=list, repr=False)
    search_counts: dict = field(default_factory=dict, repr=False)
    task_results: dict = field(default_factory=dict, repr=False)
    delegation_calls: dict[str, list[dict]] = field(default_factory=dict, repr=False)
    call_counts: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(("main_model", "sub_model", "tool", "retry"), 0)
    )

    def __post_init__(self):
        if self.scheduler is None:
            from agent.runtime.policies import load_runtime_policy
            from agent.runtime.scheduler import ServiceScheduler

            self.scheduler = ServiceScheduler((self.policy or load_runtime_policy()).scheduler)

    @property
    def task_instruction(self):
        return self.task_contract.instruction_text if self.task_contract is not None else self.user_query

    # 首次停止原因生效；后续取消、超时或完成操作不能覆盖已确定的状态。
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

    # 单调时钟不受系统校时影响，优先用于计算剩余执行时间。
    def remaining(self):
        if self.deadline_clock is not None:
            return self.deadline_clock - monotonic()
        if self.deadline is not None:
            return (self.deadline - datetime.now(UTC)).total_seconds()
        return None

    # 外部调用前、结果返回后都要检查，阻止已停止任务继续执行或接收迟到结果。
    def check_active(self):
        with self.budget_lock:
            if self.terminal_error is not None:
                raise self.terminal_error
            remaining = self.remaining()
            if self.stop_status is None and remaining is not None and remaining <= 0:
                self.stop_status = "timed_out"
            if self.stop_status is not None:
                raise RunStopped(f"Run has ended: {self.stop_status}")


# ContextVar 按当前执行上下文取值，让深层工具无需逐层传参即可找到所属运行。
_current_run: ContextVar[RunContext | None] = ContextVar("current_run", default=None)


def get_run_context() -> RunContext | None:
    return _current_run.get()


def set_run_context(context: RunContext) -> Token:
    return _current_run.set(context)


def reset_run_context(token: Token) -> None:
    _current_run.reset(token)
