"""Fail-closed SQL, filesystem, HTTP entrypoints and retry classification."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
import yaml
from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from mysql.connector import Error as MySQLError
from test_framework_contract import ScriptedModel, call

from agent.runtime.backend import GuardedFilesystemBackend, run_backend
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.hooks import HookToolMiddleware
from agent.runtime.policies import (
    MissingConfiguration,
    PolicyError,
    RuntimePolicy,
    classify_tool_error,
    load_runtime_policy,
    validate_sql,
)
from utils.path_utils import PathDenied, confined_path, resolve_path, resolve_run_path


@pytest.fixture
def context(sandbox_dir):
    context = RunContext(
        "run", "session", sandbox_dir / "work", skill_dir=sandbox_dir / "instructions", policy=load_runtime_policy()
    )
    context.work_dir.mkdir()
    context.skill_dir.mkdir()
    token = set_run_context(context)
    try:
        yield context
    finally:
        reset_run_context(token)


UNSAFE_SQL = [
    "DELETE FROM orders",
    "UPDATE orders SET quantity=0",
    "DROP TABLE orders",
    "SHOW TABLES",
    "CALL bad()",
    "SELECT * FROM orders; DELETE FROM orders",
    "SELECT * FROM hidden",
    "SELECT * FROM mysql.user",
    "SELECT * FROM orders JOIN hidden ON 1=1",
    "SELECT * FROM (SELECT * FROM hidden) x",
    "WITH x AS (SELECT * FROM hidden) SELECT * FROM x",
    "WITH unused AS (SELECT * FROM hidden) SELECT * FROM orders",
    "SELECT * FROM orders UNION ALL SELECT * FROM hidden",
    "SELECT * FROM orders WHERE id IN (SELECT id FROM hidden)",
    "SELECT SLEEP(10)",
    "SELECT BENCHMARK(10000, 1)",
    "SELECT LOAD_FILE('/secret')",
    "SELECT bad_udf()",
    "SELECT GET_LOCK('x', 1)",
    "SELECT RELEASE_LOCK('x')",
    "SELECT @@version",
    "SELECT @x := 1",
    "SELECT * INTO OUTFILE '/tmp/x' FROM orders",
    "SELECT * FROM orders FOR UPDATE",
    "SELECT /*+ MAX_EXECUTION_TIME(0) */ * FROM orders",
    "SELECT 1 /*! INTO OUTFILE '/tmp/x' */",
    "WITH RECURSIVE x AS (SELECT 1 UNION ALL SELECT 1 FROM x) SELECT * FROM x",
    "SELECT * FROM orders LIMIT -1",
    "SELECT * FROM orders LIMIT @x",
    "SELECT",
    "",
    "SELECT * FROM JSON_TABLE('{}', '$' COLUMNS(a INT PATH '$.a')) x",
]


@pytest.mark.parametrize("query", UNSAFE_SQL)
def test_unsafe_sql_rejected_before_database_connect(context, monkeypatch, query):
    from tools import db_tools

    connects = []
    monkeypatch.setattr(db_tools, "connect", lambda **kwargs: connects.append(kwargs))
    with pytest.raises(PolicyError):
        db_tools.run_select(query)
    assert connects == []


@pytest.mark.parametrize(
    "query",
    [
        "SELECT * FROM orders",
        "SELECT category, SUM(total_amount) FROM orders GROUP BY category",
        "SELECT COUNT(*) FROM orders WHERE status='completed'",
        "SELECT * FROM orders LIMIT 1000000",
        "SELECT * FROM (SELECT * FROM orders) x",
        "WITH x AS (SELECT * FROM orders) SELECT * FROM x",
        "SELECT id FROM orders UNION ALL SELECT id FROM orders",
        "SELECT 'delete from orders' AS safe",
        "SELECT * FROM orders WHERE id IN (SELECT id FROM orders)",
        "SELECT YEAR(created_at), COUNT(*) FROM orders GROUP BY YEAR(created_at)",
    ],
)
def test_legitimate_queries_are_bounded(context, query):
    bounded = validate_sql(query)
    assert "LIMIT 100" in bounded
    assert len(bounded) < 2000


@pytest.mark.parametrize("name", ["get_table_data", "execute_sql_query"])
def test_sql_policy_is_in_real_before_tool_hook(context, name):
    executions = []
    args = {"table_name": "orders;DROP TABLE orders"} if name == "get_table_data" else {"query": "DELETE FROM orders"}
    request = SimpleNamespace(tool_call={"name": name, "args": args, "id": "denied"})
    result = HookToolMiddleware().wrap_tool_call(request, lambda req: executions.append(1))
    assert result.status == "error" and executions == [] and context.call_counts["tool"] == 0


@pytest.mark.parametrize(
    "grants",
    [
        ["GRANT ALL PRIVILEGES ON `deep_search`.* TO `reader`@`%`"],
        ["GRANT SELECT, INSERT ON `deep_search`.* TO `reader`@`%`"],
        ["GRANT SELECT ON *.* TO `reader`@`%`"],
        ["GRANT SELECT ON `deep_search`.* TO `reader`@`%` WITH GRANT OPTION"],
        ["GRANT `writer_role`@`%` TO `reader`@`%`"],
        ["GRANT SELECT ON `other`.* TO `reader`@`%`"],
        [],
    ],
)
def test_write_account_or_unknown_grants_rejected(grants):
    from tools.db_tools import validate_readonly_grants

    with pytest.raises(PolicyError):
        validate_readonly_grants([(value,) for value in grants], "deep_search")


def test_real_database_adapter_checks_grants_timeout_rows_and_closes(context, monkeypatch):
    from tools import db_tools

    for key, value in {
        "MYSQL_READONLY_USER": "reader",
        "MYSQL_READONLY_PASSWORD": "fake-password",
        "MYSQL_DATABASE": "deep_search",
    }.items():
        monkeypatch.setenv(key, value)
    queries, options, fetches, lifecycle = [], [], [], []

    class Cursor:
        description = [("id",), ("product",)]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            lifecycle.append("cursor closed")

        def execute(self, query, params=None):
            queries.append((query, params))

        def fetchall(self):
            assert queries[-1][0] == "SHOW GRANTS"
            return [
                ("GRANT USAGE ON *.* TO `reader`@`%`",),
                ("GRANT SELECT ON `deep_search`.`orders` TO `reader`@`%`",),
            ]

        def fetchmany(self, rows):
            fetches.append(rows)
            return [(1, "contains,a comma")]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            lifecycle.append("connection closed")

        def cursor(self):
            return Cursor()

        def start_transaction(self, **kwargs):
            lifecycle.append(kwargs)

        def rollback(self):
            lifecycle.append("rollback")

    def connect(**kwargs):
        options.append(kwargs)
        return Connection()

    monkeypatch.setattr(db_tools, "connect", connect)
    result = db_tools.execute_sql_query.invoke({"query": "SELECT * FROM orders LIMIT 999"})
    assert '"contains,a comma"' in result and fetches == [100]
    assert queries[0][0] == "SHOW GRANTS" and queries[-1][0].endswith("LIMIT 100")
    assert ("SET SESSION MAX_EXECUTION_TIME = %s", (10000,)) in queries
    assert options[0]["user"] == "reader" and options[0]["read_timeout"] == options[0]["write_timeout"] == 10
    assert options[0]["allow_local_infile"] is False
    assert {"readonly": True} in lifecycle and lifecycle[-3:] == ["cursor closed", "rollback", "connection closed"]


def test_missing_readonly_credentials_never_fall_back_to_admin(context, monkeypatch):
    from tools import db_tools

    monkeypatch.setenv("MYSQL_USER", "root")
    monkeypatch.setenv("MYSQL_PASSWORD", "fake-root")
    calls = []
    monkeypatch.setattr(db_tools, "connect", lambda **kwargs: calls.append(1))
    with pytest.raises(MissingConfiguration):
        db_tools.run_select("SELECT * FROM orders")
    assert calls == []


@pytest.mark.parametrize(
    "value",
    [
        "../outside",
        "..\\outside",
        "D:relative.txt",
        "//server/share/file",
        "file.txt:secret",
        "CON",
        "trailing.",
        "NUL.txt",
    ],
)
def test_path_tricks_are_denied(context, value):
    with pytest.raises(PathDenied):
        resolve_run_path(value)


def make_junction(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False)
        assert result.returncode == 0, result.stderr


def test_symlink_and_junction_escape_denied_before_read_and_search(context, sandbox_dir):
    outside = sandbox_dir / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET", encoding="utf-8")
    link = context.work_dir / "linked"
    make_junction(link, outside)
    try:
        with pytest.raises(PathDenied):
            resolve_run_path("linked/secret.txt")
        backend = GuardedFilesystemBackend(context.work_dir)
        assert backend.grep_raw("SECRET") == []
        assert backend.glob_info("**/*") == []
    finally:
        link.unlink() if link.is_symlink() else link.rmdir()


def test_skill_and_upload_directories_readonly_and_separate(context):
    (context.skill_dir / "ecommerce-analysis").mkdir()
    (context.skill_dir / "ecommerce-analysis" / "SKILL.md").write_text("instructions", encoding="utf-8", newline="\r\n")
    (context.work_dir / "uploads").mkdir()
    (context.work_dir / "uploads" / "SKILL.md").write_text("user upload", encoding="utf-8")
    assert resolve_run_path("/skills/ecommerce-analysis/SKILL.md").read_text(encoding="utf-8") == "instructions"
    assert resolve_run_path("uploads/SKILL.md").read_text(encoding="utf-8") == "user upload"
    for value in ("/skills/ecommerce-analysis/SKILL.md", "uploads/SKILL.md"):
        with pytest.raises(PathDenied):
            resolve_run_path(value, write=True, virtual=True)
    backend = run_backend(None)
    assert "instructions" in backend.read("/skills/ecommerce-analysis/SKILL.md")
    assert backend.write("/skills/new.md", "attack").error
    assert backend.upload_files([("/skills/new.md", b"attack")])[0].error == "permission_denied"
    assert backend.default.write("\\uploads\\SKILL.md", "attack").error


@pytest.mark.asyncio
async def test_actual_framework_write_and_cross_round_refusal(context, sandbox_dir):
    executions = []

    @tool
    def generate_markdown(content: str, filename: str):
        """Count actual file business execution."""
        executions.append(filename)
        return content

    other = sandbox_dir / "other" / "secret.txt"
    model = ScriptedModel(
        responses=[
            call("generate_markdown", {"content": "attack", "filename": "../outside"}, "bad-custom"),
            call("write_file", {"file_path": "/skills/new.md", "content": "attack"}, "bad-skill"),
            call("read_file", {"file_path": str(other)}, "bad-other"),
            call("write_file", {"file_path": "/ok.md", "content": "safe"}, "good"),
            AIMessage(content="done"),
        ]
    )
    agent = create_deep_agent(
        model=model, tools=[generate_markdown], backend=run_backend, middleware=[HookToolMiddleware()]
    )
    state = await agent.ainvoke({"messages": [("user", "try file tools")]})
    messages = [message for message in state["messages"] if isinstance(message, ToolMessage)]
    assert [message.status for message in messages] == ["error", "error", "error", "success"]
    assert (
        executions == [] and not other.exists() and (context.work_dir / "ok.md").read_text(encoding="utf-8") == "safe"
    )
    assert context.call_counts["tool"] == 1


@pytest_asyncio.fixture
async def file_client(sandbox_dir, monkeypatch):
    import api.server as server

    monkeypatch.setattr(server, "output_dir", sandbox_dir / "output")
    monkeypatch.setattr(server, "updated_dir", sandbox_dir / "updated")
    monkeypatch.setattr(server.app.state, "runtime_policy", load_runtime_policy(), raising=False)
    server.output_dir.mkdir()
    server.updated_dir.mkdir()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        yield client, server


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename", ["../escape.txt", "..\\escape.txt", "D:escape.txt", "CON", "secret.txt:stream", "trailing."]
)
async def test_upload_rejects_bad_filenames(file_client, filename):
    client, server = file_client
    response = await client.post("/api/upload", data={"thread_id": "session"}, files={"files": (filename, b"private")})
    assert response.status_code == 400 and list(server.updated_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_upload_validation_size_and_valid_output_delivery(file_client):
    client, server = file_client
    bad = await client.post("/api/upload", data={"thread_id": "../escape"}, files={"files": ("safe.txt", b"private")})
    assert bad.status_code == 400
    data = server.app.state.runtime_policy.model_dump()
    data["files"]["max_upload_bytes"] = 4
    server.app.state.runtime_policy = RuntimePolicy.model_validate(data)
    large = await client.post("/api/upload", data={"thread_id": "session"}, files={"files": ("safe.txt", b"12345")})
    assert large.status_code == 413
    good = await client.post("/api/upload", data={"thread_id": "session"}, files={"files": ("safe.txt", b"1234")})
    assert good.status_code == 200 and (server.updated_dir / "session_session" / "safe.txt").read_bytes() == b"1234"
    report = server.output_dir / "report.txt"
    report.write_text("report", encoding="utf-8")
    assert (await client.get("/api/download", params={"path": str(report)})).text == "report"
    assert (await client.get("/output/report.txt")).text == "report"
    assert len((await client.get("/api/files")).json()["files"]) == 1
    assert (
        await client.get("/api/download", params={"path": str(server.updated_dir / "session_session" / "safe.txt")})
    ).status_code == 403
    assert (await client.get("/api/files", params={"path": str(server.updated_dir)})).status_code == 403


@pytest.mark.asyncio
async def test_static_download_listing_and_upload_junction_bypasses_blocked(file_client, sandbox_dir):
    client, server = file_client
    outside = sandbox_dir / "private"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET", encoding="utf-8")
    links = [server.output_dir / "linked", server.updated_dir / "session_link"]
    for link in links:
        make_junction(link, outside)
    try:
        assert (await client.get("/output/linked/secret.txt")).status_code == 403
        assert (await client.get("/api/download", params={"path": str(links[0] / "secret.txt")})).status_code == 403
        assert (await client.get("/api/files")).json()["files"] == []
        response = await client.post("/api/upload", data={"thread_id": "link"}, files={"files": ("new.txt", b"attack")})
        assert response.status_code == 400 and not (outside / "new.txt").exists()
    finally:
        for link in links:
            link.unlink() if link.is_symlink() else link.rmdir()


@pytest.mark.parametrize(
    "change",
    [
        ("budgets", "total", 0),
        ("budgets", "tool", True),
        ("budgets", "review", -1),
        ("sql", "allowed_tables", []),
        ("sql", "allowed_tables", ["orders;DROP"]),
        ("sql", "timeout_seconds", 0),
        ("files", "skill_directory", "../private"),
        ("files", "skill_directory", "output/skills"),
        ("retry", "max_retries", 100),
        ("retry", "tools", ["generate_markdown"]),
    ],
)
def test_invalid_configuration_refused(sandbox_dir, change):
    data = load_runtime_policy().model_dump()
    section, key, value = change
    data[section][key] = value
    config = sandbox_dir / "runtime.yml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(MissingConfiguration):
        load_runtime_policy(config)


@pytest.mark.asyncio
async def test_invalid_config_prevents_startup_before_agent_or_db(sandbox_dir, monkeypatch):
    import agent.main_agent as runtime
    import agent.runtime.policies as policies

    missing = sandbox_dir / "missing.yml"
    monkeypatch.setattr(policies, "RUNTIME_CONFIG_PATH", missing)
    monkeypatch.setattr(runtime, "checkpoint_db_path", sandbox_dir / "checkpoints.db")
    calls = []
    monkeypatch.setattr(runtime, "_build_agent", lambda saver: calls.append(1))
    with pytest.raises(MissingConfiguration):
        await runtime.initialize_agent()
    assert calls == [] and not (sandbox_dir / "checkpoints.db").exists()


@pytest.mark.parametrize(
    "error",
    [
        ValueError("invalid"),
        PermissionError("denied"),
        MissingConfiguration("missing"),
        MySQLError("sql syntax", errno=1064),
    ],
)
def test_non_transient_errors_never_retry(context, error):
    data = context.policy.model_dump()
    data["retry"]["delay_seconds"] = 0.0
    context.policy = RuntimePolicy.model_validate(data)
    executions = []

    def failed(req):
        executions.append(1)
        raise error

    with pytest.raises(type(error)):
        HookToolMiddleware().wrap_tool_call(
            SimpleNamespace(tool_call={"name": "internet_search", "args": {"query": "q"}, "id": "failed"}), failed
        )
    assert executions == [1] and context.call_counts["retry"] == 0


def test_error_classification_and_legacy_unconfined_access_removed(sandbox_dir):
    assert classify_tool_error(httpx.ConnectError("network")) == "transient"
    assert classify_tool_error(MySQLError("bad credentials", errno=1045)) == "permission"
    with pytest.raises(PathDenied):
        resolve_path("outside")
    with pytest.raises(PathDenied):
        confined_path(str(sandbox_dir.parent / "outside"), sandbox_dir)


@pytest.mark.parametrize(
    "args",
    [
        {"query": ""},
        {"query": "q", "max_results": 0},
        {"query": "q", "max_results": True},
        {"query": "q", "topic": "invalid"},
    ],
)
def test_invalid_search_parameters_do_not_execute(context, args):
    calls = []
    result = HookToolMiddleware().wrap_tool_call(
        SimpleNamespace(tool_call={"name": "internet_search", "args": args, "id": "bad"}), lambda req: calls.append(1)
    )
    assert result.status == "error" and calls == []


def test_tavily_client_single_attempt_timeout_and_close(context, monkeypatch):
    from tavily.errors import TimeoutError as TavilyTimeoutError

    from tools import tavily_tool

    monkeypatch.setenv("TAVILY_API_KEY", "fake-key")
    calls = []
    closed = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(1)

        def search(self, **kwargs):
            calls.append(kwargs)
            raise TavilyTimeoutError(kwargs["timeout"])

    monkeypatch.setattr(tavily_tool, "TavilyClient", Client)
    data = context.policy.model_dump()
    data["retry"]["delay_seconds"] = 0.0
    context.policy = RuntimePolicy.model_validate(data)
    with pytest.raises(TavilyTimeoutError):
        HookToolMiddleware().wrap_tool_call(
            SimpleNamespace(tool_call={"name": "internet_search", "args": {"query": "q"}, "id": "tavily"}),
            lambda req: tavily_tool.internet_search.invoke(req.tool_call["args"]),
        )
    assert len(calls) == len(closed) == 2
    assert all(call["timeout"] == 10 for call in calls)
    assert context.call_counts["tool"] == 2 and context.call_counts["retry"] == 1


def test_rejected_database_grants_prevent_select_and_release_connection(context, monkeypatch):
    from tools import db_tools

    for key, value in {
        "MYSQL_READONLY_USER": "reader",
        "MYSQL_READONLY_PASSWORD": "fake",
        "MYSQL_DATABASE": "deep_search",
    }.items():
        monkeypatch.setenv(key, value)
    calls = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append("cursor closed")

        def execute(self, query):
            calls.append(query)

        def fetchall(self):
            return [("GRANT ALL PRIVILEGES ON `deep_search`.* TO `reader`@`%`",)]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append("connection closed")

        def cursor(self):
            return Cursor()

        def rollback(self):
            calls.append("rollback")

    monkeypatch.setattr(db_tools, "connect", lambda **kwargs: Connection())
    with pytest.raises(PolicyError):
        db_tools.run_select("SELECT * FROM orders")
    assert calls == ["SHOW GRANTS", "cursor closed", "rollback", "connection closed"]


def test_pdf_temporary_file_stays_outside_readonly_source_and_com_cleans_up(context, monkeypatch):
    import pythoncom
    import win32com.client

    from utils.word_converter import convert_md_to_pdf_via_word

    source = context.skill_dir / "report-writing" / "references" / "template.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Report", encoding="utf-8", newline="\r\n")
    output = context.work_dir / "report.pdf"
    actions = []

    class Document:
        def SaveAs(self, path, FileFormat):
            Path(path).write_bytes(b"fake-pdf")

        def Close(self, **kwargs):
            actions.append("doc closed")

    class Documents:
        def Open(self, path):
            temp = Path(path)
            assert temp.parent == context.work_dir and temp.exists()
            actions.append(temp)
            return Document()

    word = SimpleNamespace(Documents=Documents(), Quit=lambda: actions.append("word closed"))
    monkeypatch.setattr(win32com.client, "DispatchEx", lambda name: word)
    monkeypatch.setattr(pythoncom, "CoInitialize", lambda: actions.append("initialized"))
    monkeypatch.setattr(pythoncom, "CoUninitialize", lambda: actions.append("uninitialized"))
    assert "成功转换" in convert_md_to_pdf_via_word(
        resolve_run_path("/skills/report-writing/references/template.md"), output
    )
    assert output.exists() and list(context.skill_dir.rglob("*.md")) == [source]
    assert not actions[1].exists()
    assert actions[-3:] == ["doc closed", "word closed", "uninitialized"]


@pytest.mark.parametrize(
    "html",
    [
        '<img src="file:///private/file">',
        '<svg><image href="file:///private/file"/></svg>',
        '<p style="background:url(file:///private/file)">x</p>',
        "<script>evil()</script>",
        '<a href="file:///private/file">x</a>',
    ],
)
def test_pdf_resource_loading_bypass_disabled(html):
    from utils.word_converter import ReportHTMLGuard

    with pytest.raises(PathDenied):
        ReportHTMLGuard().feed(html)
