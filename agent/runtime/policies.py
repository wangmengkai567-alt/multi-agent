# 运行策略集中入口：读取严格配置，校验 SQL 与工具参数，并在调用前核算预算和时限。
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

from agent.runtime.context import get_run_context
from agent.runtime.errors import CollectionEnded, DependencyUnavailable, ExecutionTerminated

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_CONFIG_PATH = PROJECT_ROOT / "config" / "runtime.yml"
KINDS = ("main_model", "sub_model", "tool", "retry")


class PolicyError(ValueError):
    pass


class BudgetExceeded(ExecutionTerminated):
    pass


class MissingConfiguration(ExecutionTerminated):
    """Required execution configuration is invalid; the run cannot safely continue."""


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


# 限制整轮和分类调用次数；tools_per_turn 限制单回合，task_* 限制单个子任务。
class Budgets(StrictConfig):
    total: int = Field(ge=1, le=10000)
    main_model: int = Field(ge=1, le=10000)
    sub_model: int = Field(ge=1, le=10000)
    tool: int = Field(ge=1, le=10000)
    retry: int = Field(ge=0, le=10000)
    tools_per_turn: int = Field(default=8, ge=1, le=100)
    task_model_calls: int = Field(default=12, ge=1, le=1000)
    task_tool_calls: int = Field(default=12, ge=1, le=1000)


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


# concurrency 是并发上限，queue_capacity 是等待上限，requests_per_second 为 0 时不限制速率。
class PoolLimits(StrictConfig):
    concurrency: int = Field(ge=1, le=128)
    queue_capacity: int = Field(ge=0, le=1000)
    requests_per_second: float = Field(default=0.0, ge=0, le=1000)


class SchedulerLimits(StrictConfig):
    model_accounts: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_accounts(self):
        if any(not re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", key) for key in self.model_accounts) or any(
            not value.strip() for value in self.model_accounts.values()
        ):
            raise ValueError("Model account groups must name explicit model profiles with nonempty account IDs")
        return self

    runs: PoolLimits = Field(default_factory=lambda: PoolLimits(concurrency=2, queue_capacity=16))
    children: PoolLimits = Field(default_factory=lambda: PoolLimits(concurrency=4, queue_capacity=64))
    models: PoolLimits = Field(
        default_factory=lambda: PoolLimits(concurrency=2, queue_capacity=32, requests_per_second=2.0)
    )
    search: PoolLimits = Field(
        default_factory=lambda: PoolLimits(concurrency=2, queue_capacity=32, requests_per_second=3.0)
    )


# 按子任务和整轮限制搜索、网页读取次数，以及请求时限和返回字符量。
class SearchLimits(StrictConfig):
    calls_per_task: int = Field(default=2, ge=1, le=20)
    max_tasks_per_run: int = Field(default=8, ge=1, le=32)
    calls_per_run: int = Field(default=12, ge=1, le=100)
    page_reads_per_task: int = Field(default=3, ge=1, le=10)
    timeout_seconds: int = Field(default=20, ge=1, le=120)
    result_chars: int = Field(default=1000, ge=100, le=8000)
    content_chars: int = Field(default=6000, ge=1000, le=40000)

    @model_validator(mode="after")
    def fair_capacity(self):
        if self.calls_per_run < self.calls_per_task:
            raise ValueError("Run search capacity cannot be smaller than a task budget")
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
        permitted = {"internet_search", "read_web_page", "list_sql_tables", "get_table_data", "execute_sql_query"}
        if set(self.tools) - permitted:
            raise ValueError("Only idempotent query tools may be retried")
        return self


# 总运行、单次请求、关机等待和工具调用的时间上限，单位均为秒。
class TimeoutLimits(StrictConfig):
    total_seconds: float = Field(default=300.0, gt=0, le=86400)
    request_seconds: float = Field(default=30.0, gt=0, le=300)
    shutdown_seconds: float = Field(default=5.0, gt=0, le=30)
    tool_seconds: float = Field(default=25.0, gt=0, le=300)
    report_reserve_seconds: float = Field(default=90.0, gt=0, le=86400)

    @model_validator(mode="after")
    def valid_report_reserve(self):
        if self.report_reserve_seconds >= self.total_seconds:
            raise ValueError("Report reserve must be smaller than the total run limit")
        if self.report_reserve_seconds < self.request_seconds + self.shutdown_seconds:
            raise ValueError("Report reserve must cover one model request and collection shutdown")
        return self


class ModelWindow(StrictConfig):
    window_tokens: int = Field(ge=1)
    max_input_tokens: int = Field(ge=1)
    max_thinking_input_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    max_thinking_tokens: int = Field(ge=1)

    @model_validator(mode="after")
    def valid_window(self):
        if self.max_thinking_input_tokens > self.max_input_tokens or self.max_input_tokens > self.window_tokens:
            raise ValueError("Model input limits must fit its context window")
        if max(self.max_output_tokens, self.max_thinking_tokens) >= self.window_tokens:
            raise ValueError("Model output limits must leave space for input")
        return self


# 约束请求字节数、Skill 元数据大小和各模型上下文窗口，并为输入预留安全余量。
class ContextLimits(StrictConfig):
    max_request_bytes: int = Field(ge=1024, le=10485760)
    skill_metadata_bytes: int = Field(ge=1024, le=1048576)
    safety_tokens: int = Field(ge=1)
    models: dict[str, ModelWindow]

    @model_validator(mode="after")
    def valid_models(self):
        if not self.models or any(not key.strip() for key in self.models):
            raise ValueError("Declare context limits for every configured model")
        if any(self.safety_tokens >= model.max_thinking_input_tokens for model in self.models.values()):
            raise ValueError("Context safety reserve must leave space for input")
        return self


class RuntimePolicy(StrictConfig):
    context: ContextLimits
    scheduler: SchedulerLimits = Field(default_factory=SchedulerLimits)
    timeouts: TimeoutLimits = Field(default_factory=TimeoutLimits)
    budgets: Budgets
    sql: SQLLimits
    search: SearchLimits = Field(default_factory=SearchLimits)
    files: FileLimits
    retry: RetryLimits


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
    _reserve_calls(*kinds, charge=True)


def calls_available(*kinds):
    """Use the same admission calculation without charging a future call."""
    try:
        _reserve_calls(*kinds, charge=False)
    except (BudgetExceeded, PolicyError):
        return False
    return True


# 同一锁内先检查所有类别，再一次性扣减，避免部分扣费或并发越过预算上限。
def _reserve_calls(*kinds, charge):
    if not kinds or set(kinds) - set(KINDS):
        raise ValueError("Unknown or empty call category")
    context = get_run_context()
    context.check_active()
    limits = policy_for_run().budgets
    requested = {kind: kinds.count(kind) for kind in set(kinds)}
    with context.budget_lock:
        context.check_active()
        from agent.runtime.hooks import active_call

        call = active_call.get()
        task = context.task_results.get(call.task_id) if call else None
        task_counts = task.setdefault("call_counts", {}) if task is not None else {}
        for kind, setting in (("sub_model", "task_model_calls"), ("tool", "task_tool_calls")):
            if task is not None and task_counts.get(kind, 0) + requested.get(kind, 0) > getattr(limits, setting):
                raise PolicyError(f"Task {kind} call budget exhausted")
        failure = None
        if sum(context.call_counts.values()) + len(kinds) > limits.total:
            failure = BudgetExceeded("Total call budget exhausted")
        for kind, count in requested.items():
            if context.call_counts[kind] + count > getattr(limits, kind):
                failure = BudgetExceeded(f"{kind} call budget exhausted")
        if failure is not None:
            if charge:
                context.terminate(failure)
            raise failure
        if charge:
            for kind, count in requested.items():
                context.call_counts[kind] += count
                if task is not None:
                    task_counts[kind] = task_counts.get(kind, 0) + count


# 单次请求取所有适用时限的最小值，不能超过整轮剩余时间。
def request_timeout(maximum=None, *, model_request=False):
    context = get_run_context()
    context.check_active()
    limits = policy_for_run().timeouts
    from agent.runtime.hooks import active_call

    call = active_call.get()
    admitted = call.workflow_data.get("admitted_timeout") if call and not model_request else None
    remaining = context.remaining() if context.report_started else context.collection_remaining()
    if remaining is not None and remaining <= 0:
        context.check_active()  # The total deadline takes precedence over a phase boundary.
        raise CollectionEnded("取证阶段时限已到；保留已取得的证据并转入最终报告")
    return min(value for value in (limits.request_seconds, maximum, remaining, admitted) if value is not None)


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
        raise DependencyUnavailable(
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
    # 解析 SQL 语法树，逐节点检查只读结构和表白名单，最后重建带行数上限的查询。
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
            if isinstance(node, exp.Select) and not node.expressions:
                raise PolicyError("SELECT must contain a result expression")
            if node.comments or node.key in forbidden:
                raise PolicyError("SQL contains unsupported or unsafe constructs")
            if isinstance(node, exp.With) and node.args.get("recursive"):
                raise PolicyError("Recursive queries are not permitted")
            if isinstance(node, exp.Func):
                name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name().upper()
                if name not in SAFE_FUNCTIONS:
                    raise PolicyError(f"SQL function is not permitted: {name}")
        # CTE 和派生查询由作用域解析；仅真实物理表需要核对数据库与表白名单。
        for scope in traverse_scope(tree):
            for _, source in scope.sources.items():
                if isinstance(source, exp.Table):
                    if source.db or source.catalog or not isinstance(source.this, exp.Identifier):
                        raise PolicyError("Cross-schema and table functions are not permitted")
                    validate_table(source.name, limits)
        # 无法解析作用域时直接拒绝，不能跳过表权限检查。
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
        raise DependencyUnavailable(
            "Read-only database credentials are missing; administrative credentials are not used"
        )
    if os.getenv("MYSQL_READONLY_USER", "").lower() in ("root", "admin"):
        raise PolicyError("An administrative database account is not permitted")


def classify_tool_error(error):
    from agent.runtime.workers import WorkerOperationError

    if isinstance(error, WorkerOperationError):
        return error.category
    if isinstance(error, MissingConfiguration | DependencyUnavailable):
        return "configuration"
    provider_type = type(error).__module__ + "." + type(error).__name__
    if provider_type == "tavily.errors.MissingAPIKeyError":
        return "configuration"
    if isinstance(error, BudgetExceeded):
        return "budget"
    if isinstance(error, CollectionEnded):
        return "timeout"
    if isinstance(error, PermissionError):
        return "permission"
    if provider_type in ("tavily.errors.InvalidAPIKeyError", "tavily.errors.ForbiddenError"):
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
    if (
        isinstance(
            error,
            TimeoutError | ConnectionError | httpx.TransportError | requests.ConnectionError | requests.Timeout,
        )
        or provider_type == "tavily.errors.TimeoutError"
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
    if name in ("internet_search", "read_web_page"):
        if context is None or context.network_policy == "forbidden":
            raise PolicyError("本轮禁止联网")
        from tools.tavily_tool import internet_search, read_web_page

        schema = internet_search if name == "internet_search" else read_web_page
        parsed = schema.tool_call_schema.model_validate(args)
        if not parsed.query.strip():
            raise PolicyError("Search query must not be empty")
        if name == "internet_search" and not 1 <= parsed.max_results <= 20:
            raise PolicyError("max_results must be between 1 and 20")
        if name == "read_web_page":
            from tools.tavily_tool import validate_page_request

            validate_page_request(parsed.url, parsed.query)
    if name in ("execute", "write_file", "edit_file", "generate_markdown"):
        raise PolicyError("Model-side file writes and script execution are disabled")
    if name == "task" and set(args) - {"description", "subagent_type"}:
        raise PolicyError(
            "task 仅接受 description 和 subagent_type；完整任务说明和约束必须写入 description，"
            "不能放在 prompt 等额外字段，否则框架会丢弃这些说明。"
        )
    if name == "execute_sql_query":
        validate_sql(args.get("query"))
        require_db_credentials()
    elif name == "get_table_data":
        validate_table(args.get("table_name"))
        require_db_credentials()
    elif name == "list_sql_tables":
        require_sql_tables()
        require_db_credentials()
    elif name in ("read_file_content", "read_document"):
        file = resolve_run_path(args.get("filename", ""))
        if name == "read_document":
            if not file.is_relative_to((context.work_dir / "uploads").resolve()):
                raise PolicyError("Document evidence accepts original uploads only")
    elif name in ("read_file", "write_file", "edit_file", "ls", "glob", "grep"):
        value = args.get("file_path") if name in ("read_file", "write_file", "edit_file") else args.get("path") or "/"
        resolve_run_path(value, write=name in ("write_file", "edit_file"), virtual=True)
        if name in ("glob", "grep"):
            pattern = args.get("pattern") if name == "glob" else args.get("glob")
            if pattern and (
                ".." in pattern.replace("\\", "/").split("/") or ":" in pattern or pattern.startswith(("/", "\\"))
            ):
                raise PolicyError("Search pattern must stay within its directory")


def search_call_available(name, owner):
    """Use the same search quotas for model disclosure and tool admission."""
    context = get_run_context()
    limits = policy_for_run().search
    with context.budget_lock:
        per_task = limits.calls_per_task if name == "internet_search" else limits.page_reads_per_task
        total = sum(value for (_, tool), value in context.search_counts.items() if tool == name)
        return context.search_counts.get((owner, name), 0) < per_task and total < limits.calls_per_run


def before_run_policy(call):
    policy_for_run()


# 先验证参数和权限，再登记本回合工具次数、搜索次数及重试预算。
def before_tool_policy(call):
    from agent.runtime.hooks import HookRejected

    try:
        policy_for_run()
        validate_tool_call(call)
        context = get_run_context()
        owner = call.parent_call_id or context.run_id
        turn = context.tool_turns.get((owner, call.call_id), owner)
        with context.budget_lock:
            from agent.runtime.capabilities import tool_available

            name = call.actor.rsplit(":", 1)[-1]
            if not tool_available(context, name):
                raise PolicyError("工具不在当前分工允许的执行阶段内；请根据已有证据返回结论和缺口")
            if context.turn_counts.get(turn, 0) >= context.policy.budgets.tools_per_turn:
                raise PolicyError("单次模型回合工具调用次数已达到上限")
            if call.actor.rsplit(":", 1)[-1] != "task":
                call.workflow_data["admitted_timeout"] = request_timeout(context.policy.timeouts.tool_seconds)
            if name in ("internet_search", "read_web_page"):
                task_key = (owner, name)
                if not search_call_available(name, owner):
                    raise PolicyError("Search/page read budget exhausted; use the actual results already obtained")
            from agent.runtime.hooks import active_call

            parent = active_call.get()
            category = "sub_model" if parent and parent.task_id else "main_model"
            kinds = ("tool", "retry") if call.attempt else ("tool",)
            if not calls_available(*kinds, category, *(("main_model",) if category == "sub_model" else ())):
                raise PolicyError(
                    "Remaining call budget is reserved for the final answer; return obtained results and gaps"
                )
            reserve_calls(*kinds)
            if name in ("internet_search", "read_web_page"):
                context.search_counts[task_key] = context.search_counts.get(task_key, 0) + 1
            context.turn_counts[turn] = context.turn_counts.get(turn, 0) + 1
    except (PolicyError, PermissionError, ValidationError) as exc:
        raise HookRejected(str(exc)) from exc
