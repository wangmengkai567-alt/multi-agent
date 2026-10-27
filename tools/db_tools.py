"""Read-only MySQL tools: validated SQL, verified grants and bounded reads."""

import csv
import io
import os
import re
from contextlib import contextmanager

from dotenv import load_dotenv
from langchain_core.tools import tool

from agent.runtime.evidence_protocol import evidence_tool
from agent.runtime.policies import (
    PolicyError,
    SQLLimits,
    policy_for_run,
    request_timeout,
    require_db_credentials,
    require_sql_tables,
    validate_sql,
    validate_table,
)
from api.monitor import monitor


def get_db_config():
    limits = require_sql_tables()
    load_dotenv()
    require_db_credentials()
    timeout = max(1, int(request_timeout(limits.timeout_seconds)))
    database = os.getenv("MYSQL_DATABASE")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database):
        raise PolicyError("Database name must be a simple identifier")
    return {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.environ["MYSQL_READONLY_USER"],
        "password": os.environ["MYSQL_READONLY_PASSWORD"],
        "database": database,
        "charset": "utf8mb4",
        "autocommit": False,
        "connection_timeout": timeout,
        "read_timeout": timeout,
        "write_timeout": timeout,
        "allow_local_infile": False,
        "use_pure": True,
    }


def validate_readonly_grants(grants, database):
    """Fail closed on write privileges, roles, global SELECT or unknown grants."""
    if not grants:
        raise PolicyError("Could not verify database account grants")
    selectable = False
    for row in grants:
        grant = row[0]
        match = re.fullmatch(
            r"GRANT (.+?) ON (`[^`]+`|\*|[A-Za-z0-9_]+)\.(`[^`]+`|\*|[A-Za-z0-9_]+) TO (?:`[^`]+`|'[^']+')@(?:`[^`]+`|'[^']+')",
            grant,
            re.IGNORECASE,
        )
        if not match:
            raise PolicyError("Database account has unsupported grants or roles")
        privileges = {part.strip().upper() for part in match[1].split(",")}
        schema, table = match[2].strip("`"), match[3].strip("`")
        if privileges == {"USAGE"} and schema == "*" and table == "*":
            continue
        if not privileges <= {"SELECT", "SHOW VIEW"} or schema != database:
            raise PolicyError("Database account must have only read-only privileges in the configured database")
        if "SELECT" in privileges:
            selectable = True
    if not selectable:
        raise PolicyError("Database account has no SELECT privilege")


@contextmanager
def readonly_cursor(config, limits):
    from mysql.connector import connect

    with connect(**config) as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SHOW GRANTS")
                validate_readonly_grants(cursor.fetchall(), config["database"])
                cursor.execute("SET SESSION MAX_EXECUTION_TIME = %s", (limits.timeout_seconds * 1000,))
                cursor.execute("SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION'")
                connection.start_transaction(readonly=True)
                yield cursor
        finally:
            connection.rollback()


def execute_worker(payload):
    limits = SQLLimits.model_validate(payload["limits"])
    with readonly_cursor(payload["config"], limits) as cursor:
        if payload["action"] in ("probe", "list"):
            names = []
            for name in limits.allowed_tables:
                cursor.execute("SHOW TABLES LIKE %s", (name,))
                row = cursor.fetchone()
                if row and row[0] == name:
                    names.append(name)
            if payload["action"] == "probe":
                if set(names) != set(limits.allowed_tables):
                    from agent.runtime.errors import DependencyUnavailable

                    raise DependencyUnavailable("Configured database tables are missing or inaccessible")
                return {"tables_verified": len(names)}
            return "可用的表有：" + ", ".join(names) if names else "没有可用的表"
        if payload["action"] != "select":
            raise ValueError("Unknown read-only database operation")
        cursor.execute(validate_sql(payload["query"], limits))
        if not cursor.description:
            raise PolicyError("Query did not return a read-only result set")
        rows = cursor.fetchmany(limits.max_rows)
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow([item[0] for item in cursor.description])
        writer.writerows(rows)
        return output.getvalue()


def database_operation(action, **parameters):
    from agent.runtime.workers import run_operation

    limits = policy_for_run().sql
    return run_operation(
        "database",
        dict(action=action, config=get_db_config(), limits=limits.model_dump(mode="json"), **parameters),
        seconds=request_timeout(limits.timeout_seconds),
    )


def run_select(query):
    return database_operation("select", query=validate_sql(query))


@evidence_tool("sql.v1")
@tool
def list_sql_tables() -> str:
    """List only configured allowed tables in the current read-only database."""
    monitor.report_tool("数据库表名查询工具：list_sql_tables", {})
    return database_operation("list")


@evidence_tool("sql.v1")
@tool
def get_table_data(table_name: str) -> str:
    """Read columns and a bounded sample from a configured allowed table."""
    validate_table(table_name)
    monitor.report_tool("数据库表数据查询工具：get_table_data", {"table_name": table_name})
    return run_select(f"SELECT * FROM `{table_name}`")


@evidence_tool("sql.v1")
@tool
def execute_sql_query(query: str) -> str:
    """Execute one validated SELECT, with allowed tables, row limit and timeout."""
    validate_sql(query)
    monitor.report_tool("数据库表数据查询工具：execute_sql_query", {"query": query})
    return run_select(query)
