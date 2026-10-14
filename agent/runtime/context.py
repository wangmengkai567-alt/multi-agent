"""Per-run context shared by async execution and its synchronous tools."""

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class RunContext:
    run_id: str
    thread_id: str
    work_dir: Path
    deadline: datetime | None = None
    call_counts: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(("main_model", "sub_model", "review", "tool", "retry"), 0)
    )


_current_run: ContextVar[RunContext | None] = ContextVar("current_run", default=None)


def get_run_context() -> RunContext | None:
    return _current_run.get()


def set_run_context(context: RunContext) -> Token:
    return _current_run.set(context)


def reset_run_context(token: Token) -> None:
    _current_run.reset(token)
