"""Validated runtime limits, atomic reservations and fail-closed tool policies."""

import os
import re
from pathlib import Path

import httpx
import requests
import sqlglot
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope
from tavily.errors import ForbiddenError, InvalidAPIKeyError, MissingAPIKeyError
from tavily.errors import TimeoutError as TavilyTimeoutError

from agent.runtime.context import get_run_context

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_CONFIG_PATH = PROJECT_ROOT / "config" / "runtime.yml"
KINDS = ("main_model", "sub_model", "review", "tool", "retry")


class PolicyError(ValueError):
    pass


class SearchLimitReached(PolicyError):
    pass


class BudgetExceeded(PolicyError):
    pass


class MissingConfiguration(PolicyError):
    pass


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Budgets(StrictConfig):
    total: int = Field(ge=1, le=10000)
    main_model: int = Field(ge=1, le=10000)
    sub_model: int = Field(ge=1, le=10000)
    review: int = Field(ge=1, le=10000)
    tool: int = Field(ge=1, le=10000)
    retry: int = Field(ge=0, le=10000)
    tools_per_turn: int = Field(default=8, ge=1, le=100)
    near_ratio: float = Field(default=0.85, gt=0, lt=1)


class SQLLimits(StrictConfig):
    allowed_tables: list[str]
    max_rows: int = Field(ge=1, le=10000)
    timeout_seconds: int = Field(ge=1, le=120)
    max_query_chars: int = Field(ge=1, le=100000)

    @model_validator(mode="after")
    def valid_tables(self):
        if len(set(self.allowed_tables)) != len(self.allowed_tables) or any(
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in self.allowed_tables
        ):
            raise ValueError("allowed_tables must contain unique simple table identifiers")
        return self


class SearchLimits(StrictConfig):
    calls_per_task: int = Field(default=2, ge=1, le=20)
    refinement_calls_per_task: int = Field(default=2, ge=0, le=10)
    research_calls_per_task: int = Field(default=6, ge=1, le=20)
    max_tasks_per_run: int = Field(default=3, ge=1, le=10)
    calls_per_run: int = Field(default=12, ge=1, le=100)
    research_calls_per_run: int = Field(default=24, ge=1, le=100)
    page_reads_per_task: int = Field(default=3, ge=1, le=10)
    timeout_seconds: int = Field(default=20, ge=1, le=120)
    result_chars: int = Field(default=1000, ge=100, le=8000)
    content_chars: int = Field(default=6000, ge=1000, le=40000)
    model_context_chars: int = Field(default=12000, ge=2000, le=80000)

    @model_validator(mode="after")
    def fair_capacity(self):
        for task_limit, run_limit in (
            (self.calls_per_task, self.calls_per_run),
            (self.research_calls_per_task, self.research_calls_per_run),
        ):
            if run_limit < self.max_tasks_per_run * (task_limit + self.refinement_calls_per_task):
                raise ValueError("Run search capacity must cover every task's initial and reserved refinement calls")
        return self


class FileLimits(StrictConfig):
    skill_directory: str
    max_upload_bytes: int = Field(ge=1, le=104857600)

    @model_validator(mode="after")
    def valid_skill_directory(self):
        from utils.path_utils import confined_path

        if Path(self.skill_directory).is_absolute() or ":" in self.skill_directory:
            raise ValueError("skill_directory must be project-relative")
        confined_path(self.skill_directory, PROJECT_ROOT)
        if not self.skill_directory or self.skill_directory in (".", "output", "updated"):
            raise ValueError("Skills must have a separate directory")
        first = Path(self.skill_directory).parts[0]
        if first in ("output", "updated", "data", "config"):
            raise ValueError("Skills must be separate from user files and service data")
        return self


class RetryLimits(StrictConfig):
    max_retries: int = Field(ge=0, le=3)
    delay_seconds: float = Field(ge=0, le=5)
    tools: list[str]
    max_delay_seconds: float = Field(default=20.0, ge=0, le=300)

    @model_validator(mode="after")
    def safe_tools(self):
        permitted = {"read_web_page", "internet_search", "list_sql_tables", "get_table_data", "execute_sql_query"}
        if set(self.tools) - permitted:
            raise ValueError("Only idempotent query tools may be retried")
        return self


class ReviewLimits(StrictConfig):
    json_mode: bool = False
    strict_schema: bool = False
    on_check_failure: str = Field(default="reject", pattern="^(reject|return_unchecked)$")


class TimeoutLimits(StrictConfig):
    total_seconds: float = Field(default=300.0, gt=0, le=86400)
    request_seconds: float = Field(default=30.0, gt=0, le=300)
    shutdown_seconds: float = Field(default=5.0, gt=0, le=30)
    tool_seconds: float = Field(default=25.0, gt=0, le=300)
    finalize_seconds: float = Field(default=45.0, ge=0, le=300)
    job_seconds: float = Field(default=1800.0, gt=0, le=86400)


class RuntimePolicy(StrictConfig):
    timeouts: TimeoutLimits = Field(default_factory=TimeoutLimits)
    budgets: Budgets
    job_budgets: Budgets = Field(
        default_factory=lambda: Budgets(total=240, main_model=40, sub_model=90, review=24, tool=120, retry=24)
    )
    sql: SQLLimits
    search: SearchLimits = Field(default_factory=SearchLimits)
    files: FileLimits
    retry: RetryLimits
    review: ReviewLimits = Field(default_factory=ReviewLimits)


def load_runtime_policy(path=None):
    try:
        with Path(path or RUNTIME_CONFIG_PATH).open(encoding="utf-8") as source:
            return RuntimePolicy.model_validate(yaml.safe_load(source))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise MissingConfiguration(f"Invalid runtime configuration: {exc}") from exc


def policy_for_run():
    context = get_run_context()
    if context is None:
        raise MissingConfiguration("A run context is required for controlled execution")
    with context.budget_lock:
        if context.policy is None:
            context.policy = load_runtime_policy()
        return context.policy


def reserve_calls(*kinds):
    """Reserve all categories or none, before performing any external action."""
    if not kinds or set(kinds) - set(KINDS):
        raise ValueError("Unknown or empty call category")
    context = get_run_context()
    context.check_active()
    limits = policy_for_run().budgets
    requested = {kind: kinds.count(kind) for kind in set(kinds)}
    with context.budget_lock:
        context.check_active()
        if sum(context.call_counts.values()) + len(kinds) > limits.total:
            raise BudgetExceeded("Total run call budget exhausted")
        for kind, count in requested.items():
            if context.call_counts[kind] + count > getattr(limits, kind):
                raise BudgetExceeded(f"{kind} call budget exhausted")
        for kind, count in requested.items():
            context.call_counts[kind] += count
        from agent.runtime.checkpoints import save_job_checkpoint

        save_job_checkpoint(context)


def request_timeout(maximum=None):
    context = get_run_context()
    limits = policy_for_run().timeouts
    context.check_active()
    remaining = context.remaining()
    return min(value for value in (limits.request_seconds, maximum, remaining) if value is not None)


SAFE_FUNCTIONS = frozenset(
    {
        "ABS",
        "AVG",
        "CEIL",
        "CEILING",
        "COALESCE",
        "CONCAT",
        "CONCAT_WS",
        "COUNT",
        "DATE",
        "DATEDIFF",
        "DATE_ADD",
        "DATE_SUB",
        "DATE_FORMAT",
        "DAY",
        "DAYOFMONTH",
        "EXTRACT",
        "FLOOR",
        "IF",
        "IFNULL",
        "LENGTH",
        "CHAR_LENGTH",
        "LOWER",
        "MAX",
        "MIN",
        "MONTH",
        "NULLIF",
        "ROUND",
        "SUM",
        "TRIM",
        "UPPER",
        "YEAR",
        "ROW_NUMBER",
        "RANK",
        "DENSE_RANK",
        "LAG",
        "LEAD",
        "SUBSTRING",
        "CAST",
        "CASE",
        "CURRENT_DATE",
        "CURRENT_TIMESTAMP",
        "STR_TO_DATE",
        "TIME_TO_STR",
        "TS_OR_DS_TO_DATE",
        "DATE_DIFF",
        "DATE_STR_TO_DATE",
    }
)


def require_sql_tables(limits=None):
    limits = limits or policy_for_run().sql
    if not limits.allowed_tables:
        raise MissingConfiguration(
            "MySQL queries are disabled: configure sql.allowed_tables with actual business tables"
        )
    return limits


def validate_table(table, limits=None):
    limits = require_sql_tables(limits)
    if not isinstance(table, str) or table not in limits.allowed_tables:
        raise PolicyError("Table is not in the allowed table list")
    return table


def validate_sql(query, limits=None):
    """Parse MySQL AST; validate every scope, then rebuild bounded SELECT SQL.

    Comments, variables, locks, recursive CTEs, external schemas and unapproved
    functions are deliberately outside the first version's SQL contract.
    """
    limits = require_sql_tables(limits)
    if not isinstance(query, str) or not query.strip() or len(query) > limits.max_query_chars:
        raise PolicyError("Invalid SQL query length")
    try:
        trees = sqlglot.parse(query, read="mysql", error_level=sqlglot.ErrorLevel.RAISE)
        if len(trees) != 1 or not isinstance(trees[0], exp.Select | exp.Union):
            raise PolicyError("Only a single read-only SELECT query is allowed")
        tree = trees[0]
        forbidden = {
            "into",
            "lock",
            "command",
            "insert",
            "update",
            "delete",
            "create",
            "drop",
            "alter",
            "grant",
            "set",
            "use",
            "transaction",
            "commit",
            "rollback",
            "parameter",
            "sessionparameter",
            "placeholder",
            "propertyeq",
            "assignment",
            "var",
            "dot",
        }
        for node in tree.walk():
            if node.comments or node.key in forbidden:
                raise PolicyError("SQL contains unsupported or unsafe constructs")
            if isinstance(node, exp.With) and node.args.get("recursive"):
                raise PolicyError("Recursive queries are not permitted")
            if isinstance(node, exp.Func):
                name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name().upper()
                if name not in SAFE_FUNCTIONS:
                    raise PolicyError(f"SQL function is not permitted: {name}")
        # Sources resolved as Scope are CTEs/derived queries, never physical tables.
        for scope in traverse_scope(tree):
            for _, source in scope.sources.items():
                if isinstance(source, exp.Table):
                    if source.db or source.catalog or not isinstance(source.this, exp.Identifier):
                        raise PolicyError("Cross-schema and table functions are not permitted")
                    validate_table(source.name, limits)
        # Direct table references must all be scope-resolved (no parser fallback).
        if not traverse_scope(tree):
            raise PolicyError("SQL scope could not be validated")
        limit = tree.args.get("limit")
        if limit is not None:
            value = limit.expression
            if not isinstance(value, exp.Literal) or not value.is_int or int(value.this) < 0:
                raise PolicyError("LIMIT must be a non-negative integer")
            rows = min(int(value.this), limits.max_rows)
        else:
            rows = limits.max_rows
        return tree.limit(rows).sql(dialect="mysql")
    except PolicyError:
        raise
    except (sqlglot.errors.SqlglotError, ValueError, TypeError) as exc:
        raise PolicyError("SQL could not be safely parsed") from exc


def require_db_credentials():
    required = ("MYSQL_READONLY_USER", "MYSQL_READONLY_PASSWORD", "MYSQL_DATABASE")
    if any(not os.getenv(key) for key in required):
        raise MissingConfiguration(
            "Read-only database credentials are missing; administrative credentials are not used"
        )
    if os.getenv("MYSQL_READONLY_USER", "").lower() in ("root", "admin"):
        raise PolicyError("An administrative database account is not permitted")


def classify_tool_error(error):
    if isinstance(error, MissingConfiguration):
        return "configuration"
    if isinstance(error, MissingAPIKeyError):
        return "configuration"
    if isinstance(error, SearchLimitReached):
        return "search_limit"
    if isinstance(error, BudgetExceeded):
        return "budget"
    if isinstance(error, PermissionError):
        return "permission"
    if isinstance(error, InvalidAPIKeyError | ForbiddenError):
        return "permission"
    if isinstance(error, PolicyError | ValidationError | ValueError | TypeError):
        return "invalid_argument"
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None) or getattr(error, "status_code", None)
    if status in (401, 403) or getattr(error, "errno", None) in (1044, 1045, 1142):
        return "permission"
    if status == 429:
        return "rate_limited"
    if status == 408 or status is not None and 500 <= status < 600:
        return "transient"
    if status is not None and 400 <= status < 500:
        return "permanent"
    if isinstance(
        error,
        TimeoutError
        | ConnectionError
        | httpx.TransportError
        | requests.ConnectionError
        | requests.Timeout
        | TavilyTimeoutError,
    ):
        return "transient"
    if getattr(error, "errno", None) in (1205, 2003, 2006, 2013):
        return "transient"
    return "execution"


def validate_tool_call(call):
    from utils.path_utils import resolve_run_path

    name = call.actor.rsplit(":", 1)[-1]
    args = call.args
    context = get_run_context()
    if (
        context
        and context.background_job
        and name in ("generate_markdown", "convert_md_to_pdf", "write_file", "edit_file")
    ):
        raise PolicyError("当前持久化研究任务仅执行只读查询；文件交付由交互轮次单独执行")
    if (
        context
        and not context.collecting
        and name in ("task", "internet_search", "read_web_page", "submit_research_job")
    ):
        raise BudgetExceeded("本轮已进入交付阶段，不再启动调查")
    if name == "execute":
        raise PolicyError("Script execution is disabled")
    if name == "task" and set(args) - {"description", "subagent_type"}:
        raise PolicyError(
            "task 仅接受 description 和 subagent_type；完整任务说明和约束必须写入 description，"
            "不能放在 prompt 等额外字段，否则框架会丢弃这些说明。"
        )
    if name == "read_web_page":
        from tools.tavily_tool import validate_page_request

        validate_page_request(args.get("url", ""), args.get("query", ""))
    if name == "internet_search":
        if not isinstance(args.get("query"), str) or not args["query"].strip():
            raise PolicyError("Search query must not be empty")
        maximum = args.get("max_results", 5)
        if (
            type(maximum) is not int
            or not 1 <= maximum <= 20
            or args.get("topic", "general") not in ("news", "finance", "general")
        ):
            raise PolicyError("Invalid search parameters")
    if name == "execute_sql_query":
        validate_sql(args.get("query"))
        require_db_credentials()
    elif name == "get_table_data":
        validate_table(args.get("table_name"))
        require_db_credentials()
    elif name == "list_sql_tables":
        require_sql_tables()
        require_db_credentials()
    elif name == "generate_markdown":
        filename = args.get("filename", "")
        if not filename.endswith(".md"):
            filename += ".md"
        resolve_run_path(str(Path(args.get("path") or "") / filename), write=True)
    elif name == "convert_md_to_pdf":
        source = resolve_run_path(str(Path(args.get("md_filename", "")).with_suffix(".md")))
        resolve_run_path(
            str(Path(args["pdf_filename"]).with_suffix(".pdf"))
            if args.get("pdf_filename")
            else str(source.with_suffix(".pdf")),
            write=True,
        )
    elif name == "read_file_content":
        resolve_run_path(args.get("filename", ""))
    elif name in ("read_file", "write_file", "edit_file", "ls", "glob", "grep"):
        value = args.get("file_path") if name in ("read_file", "write_file", "edit_file") else args.get("path") or "/"
        resolve_run_path(value, write=name in ("write_file", "edit_file"), virtual=True)
        if name in ("glob", "grep"):
            pattern = args.get("pattern") if name == "glob" else args.get("glob")
            if pattern and (
                ".." in pattern.replace("\\", "/").split("/") or ":" in pattern or pattern.startswith(("/", "\\"))
            ):
                raise PolicyError("Search pattern must stay within its directory")


def before_run_policy(call):
    policy_for_run()


def before_tool_policy(call):
    from agent.runtime.hooks import HookRejected

    try:
        policy_for_run()
        validate_tool_call(call)
        if call.actor.rsplit(":", 1)[-1] == "internet_search":
            from agent.runtime.search import reserve_search

            call.search_budget = reserve_search(call)
        elif call.actor.rsplit(":", 1)[-1] == "read_web_page":
            from agent.runtime.search import search_allowance

            if allowance := search_allowance.get():
                allowance.reserve_page(call.call_id, retry=call.attempt > 0)
        context = get_run_context()
        owner = call.parent_call_id or context.run_id
        turn = context.tool_turns.get((owner, call.call_id), owner)
        with context.budget_lock:
            if context.turn_counts.get(turn, 0) >= context.policy.budgets.tools_per_turn:
                raise BudgetExceeded("单次模型回合工具调用次数已达到上限")
            reserve_calls(*(("tool", "retry") if call.attempt else ("tool",)))
            context.turn_counts[turn] = context.turn_counts.get(turn, 0) + 1
            from agent.runtime.checkpoints import save_job_checkpoint

            save_job_checkpoint(context)
        from agent.runtime.workflows import before_tool_workflow

        before_tool_workflow(call)
    except (PolicyError, PermissionError) as exc:
        raise HookRejected(str(exc)) from exc
