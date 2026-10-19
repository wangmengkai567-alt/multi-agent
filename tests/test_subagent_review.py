"""Step 09: actual task routing, tool evidence, bounded review and refinement."""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from pydantic import Field
from test_delegation import build
from test_framework_contract import ScriptedModel, call
from test_review_contract import finding, rejected

from agent.reflection import ResultReviewer, ReviewResult, rule_review
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.delegation import DelegationRunnable
from agent.runtime.events import EventSink
from agent.runtime.hooks import HookToolMiddleware
from agent.runtime.policies import MissingConfiguration, RuntimePolicy, load_runtime_policy
from agent.subagent_registry import DISPLAY_NAMES, ReviewConfig, SubagentRegistry, SubagentSpec
from api.run_store import RunStore


@pytest_asyncio.fixture
async def runtime(sandbox_dir):
    store = await RunStore.open(sandbox_dir / "reviews.db")
    sink = EventSink(store, lambda *args: None)
    policy = load_runtime_policy().model_dump()
    policy["sql"]["allowed_tables"] = ["orders"]
    context = RunContext(
        "round", "session", sandbox_dir / "work", event_sink=sink, policy=RuntimePolicy.model_validate(policy)
    )
    context.work_dir.mkdir()
    token = set_run_context(context)
    await sink.create(context.thread_id, "question", context.run_id)
    await sink.start(context)
    try:
        yield context, store, sink, []
    finally:
        reset_run_context(token)
        await sink.close()
        await store.close()


def spec(settings, tools=(), target="search"):
    return SubagentSpec(
        target, DISPLAY_NAMES[target], "Find evidence", "Return evidence", tools, review_config=settings
    )


def approved():
    return AIMessage(content=ReviewResult(verdict="passed").model_dump_json())


@pytest.mark.asyncio
async def test_search_review_keeps_actual_request_separate_from_normalized_response(runtime):
    import json

    context, store, sink, _ = runtime
    actual = []
    requested = "site:docs.tavily.com search"
    url = "https://docs.tavily.com/documentation/api-reference/endpoint/search"

    @tool
    def internet_search(query: str, max_results: int, include_raw_content: bool):
        """Return a normalized response echo with a genuine source URL."""
        actual.append((query, max_results, include_raw_content))
        return json.dumps({"query": "search", "results": [{"url": url, "content": "official search documentation"}]})

    settings = ReviewConfig(enabled=True, model_check=True, required_tools=("internet_search",), require_sources=True)
    child = ScriptedModel(
        responses=[
            call(
                "internet_search", {"query": requested, "max_results": 2, "include_raw_content": False}, "search-tool"
            ),
            AIMessage(content="官方文档：" + url),
            approved(),
        ]
    )
    parent = ScriptedModel(
        responses=[call("task", {"subagent_type": "search", "description": requested}), AIMessage(content="done")]
    )
    await build(SubagentRegistry([spec(settings, (internet_search,))]), parent, {"search": child}).ainvoke(
        {"messages": [("user", requested)]}
    )
    assert actual == [(requested, 2, False)]
    review_input = json.loads(child.seen_messages[-1][-1].text)
    evidence = review_input["tools"][0]
    assert evidence["executed"] and evidence["status"] == "completed"
    assert evidence["request_parameters"] == {"query": requested, "max_results": 2, "include_raw_content": False}
    assert evidence["query_reference"] == {}
    assert evidence["content"]["query"] == "search"
    await sink.flush(context.run_id)
    assert (await store.list_subtasks(context.run_id))[0]["check_status"] == "passed"


@pytest.mark.asyncio
@pytest.mark.parametrize("sync", [False, True])
async def test_empty_result_refined_once_and_durable(runtime, sync):
    context, store, sink, _ = runtime
    inputs = []

    def execute(state):
        inputs.append(state)
        return {"messages": [AIMessage(content="" if len(inputs) == 1 else "complete facts")]}

    wrapper = DelegationRunnable(spec(ReviewConfig(enabled=True)), RunnableLambda(execute))
    initial = {"messages": [HumanMessage(content="original task")]}
    result = await asyncio.to_thread(wrapper.invoke, initial) if sync else await wrapper.ainvoke(initial)
    assert result["messages"][-1].text == "complete facts" and len(inputs) == 2
    assert "结果为空" in inputs[1]["messages"][-1].text
    assert initial["messages"] == [HumanMessage(content="original task")]
    await sink.flush(context.run_id)
    task = (await store.list_subtasks(context.run_id))[0]
    assert task["check_status"] == "passed" and task["status"] == "completed"
    events = await store.list_events(context.run_id)
    assert [e["status"] for e in events if e["event"] == "subagent_review"] == ["insufficient", "passed"]
    assert sum(e["event"] == "after_subagent" for e in events) == 1
    assert context.call_counts["retry"] == 1


@pytest.mark.asyncio
async def test_repeated_empty_result_never_runs_third_attempt(runtime):
    context, store, sink, _ = runtime
    executions = []

    def execute(state):
        executions.append(1)
        return {"messages": [AIMessage(content="")]}

    result = await DelegationRunnable(spec(ReviewConfig(enabled=True)), RunnableLambda(execute)).ainvoke(
        {"messages": [HumanMessage(content="task")]}
    )
    assert len(executions) == 2 and "insufficient" in result["messages"][-1].text
    await sink.flush(context.run_id)
    assert (await store.list_subtasks(context.run_id))[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_actual_framework_search_sources_are_reused_on_citation_revision(runtime):
    context, store, sink, _ = runtime
    queries = []

    @tool
    def internet_search(query: str):
        """Offline actual web search business function."""
        queries.append(query)
        return {"results": [{"url": "https://example.org/evidence", "title": "Source"}]}

    settings = ReviewConfig(enabled=True, model_check=True, required_tools=("internet_search",), require_sources=True)
    child = ScriptedModel(
        responses=[
            call("internet_search", {"query": "first"}, "web-first"),
            AIMessage(content="no citations"),
            AIMessage(content="facts https://example.org/evidence"),
            approved(),
        ]
    )
    parent = ScriptedModel(
        responses=[call("task", {"subagent_type": "search", "description": "search facts"}), AIMessage(content="done")]
    )
    result = await build(SubagentRegistry([spec(settings, (internet_search,))]), parent, {"search": child}).ainvoke(
        {"messages": [("user", "search")]}
    )
    assert result["messages"][-1].text == "done" and queries == ["first"]
    assert "来源 URL" in str(child.seen_messages[2])
    assert context.call_counts == dict(main_model=2, sub_model=3, review=1, tool=2, retry=1)
    await sink.flush(context.run_id)
    task = (await store.list_subtasks(context.run_id))[0]
    assert task["check_status"] == "passed"
    assert "web-first" in str(child.seen_messages[-1])


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [MissingConfiguration("service missing"), ConnectionError("offline")])
async def test_dependency_unavailable_not_refined_or_fabricated(runtime, error):
    context, store, sink, _ = runtime
    executions = []

    def execute(state):
        executions.append(1)
        raise error

    result = await DelegationRunnable(spec(ReviewConfig(enabled=True)), RunnableLambda(execute)).ainvoke(
        {"messages": [HumanMessage(content="query")]}
    )
    assert len(executions) == 1 and "dependency_unavailable" in result["messages"][-1].text
    assert context.call_counts["retry"] == 0
    await sink.flush(context.run_id)
    assert (await store.list_subtasks(context.run_id))[0]["check_status"] == "dependency_unavailable"


@pytest.mark.asyncio
async def test_standard_tool_error_uses_actual_hook_records(runtime):
    _, store, sink, _ = runtime
    executions = []

    def execute(state):
        executions.append(1)
        HookToolMiddleware("search").wrap_tool_call(
            SimpleNamespace(tool_call={"name": "probe", "args": {}, "id": "failed"}),
            lambda req: ToolMessage(content="error payload", status="error", tool_call_id="failed"),
        )
        return {"messages": [AIMessage(content="I succeeded")]}

    result = await DelegationRunnable(spec(ReviewConfig(enabled=True)), RunnableLambda(execute)).ainvoke(
        {"messages": [HumanMessage(content="query")]}
    )
    assert len(executions) == 1 and "tool_failed" in result["messages"][-1].text
    assert "I succeeded" not in result["messages"][-1].text
    await sink.flush()
    assert (await store.list_subtasks("round"))[0]["check_status"] == "tool_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["exception", "malformed", "unchecked", "missing"])
async def test_check_failure_explicit_configured_degradation(runtime, mode):
    context, store, sink, _ = runtime
    if mode == "unchecked":
        from agent.runtime.policies import policy_for_run

        data = policy_for_run().model_dump()
        data["review"]["on_check_failure"] = "return_unchecked"
        context.policy = RuntimePolicy.model_validate(data)
    model = ScriptedModel(responses=[] if mode == "exception" else [AIMessage(content="invalid JSON")] * 2)
    settings = ReviewConfig(enabled=True, model_check=True)
    reviewer = ResultReviewer(settings, None if mode == "missing" else model)
    result = await DelegationRunnable(
        spec(settings),
        RunnableLambda(lambda state: {"messages": [AIMessage(content="unchecked facts")]}),
        reviewer=reviewer,
    ).ainvoke({"messages": [HumanMessage(content="query")]})
    assert "check_incomplete" in result["messages"][-1].text
    assert ("unchecked facts" in result["messages"][-1].text) == (mode == "unchecked")
    assert context.call_counts["retry"] == (1 if mode in ("malformed", "unchecked") else 0)
    await sink.flush()
    task = (await store.list_subtasks("round"))[0]
    assert task["check_status"] == "check_incomplete"
    assert task["status"] == ("completed" if mode == "unchecked" else "failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["review", "retry", "total"])
async def test_budget_exhaustion_stops_new_actions(runtime, kind):
    context, store, sink, _ = runtime
    from agent.runtime.policies import policy_for_run

    limits = policy_for_run().budgets
    context.call_counts["review" if kind == "total" else kind] = (
        limits.total if kind == "total" else getattr(limits, kind)
    )
    settings = ReviewConfig(enabled=True, model_check=kind != "retry")
    model = ScriptedModel(responses=[approved()])
    executions = []

    def execute(state):
        executions.append(1)
        return {"messages": [AIMessage(content="" if kind == "retry" else "facts")]}

    result = await DelegationRunnable(
        spec(settings), RunnableLambda(execute), reviewer=ResultReviewer(settings, model)
    ).ainvoke({"messages": [HumanMessage(content="query")]})
    assert len(executions) == 1 and not model.seen_messages
    assert "budget_exhausted" in result["messages"][-1].text
    await sink.flush()
    assert (await store.list_subtasks("round"))[0]["check_status"] == "budget_exhausted"


def test_rules_require_real_schema_web_records_and_fields():
    settings = ReviewConfig(enabled=True, required_tools=("get_table_data",), required_fields=("统计口径",))
    assert rule_review("已查询表结构，统计口径", [], settings).verdict == "insufficient"
    evidence = [
        dict(
            name="get_table_data",
            call_id="schema",
            executed=True,
            status="completed",
            content="id,amount",
            error_category=None,
        )
    ]
    assert rule_review("missing fields", evidence, settings).verdict == "insufficient"
    assert rule_review("统计口径：按天", evidence, settings).verdict == "passed"
    evidence[0]["executed"] = False
    assert rule_review("统计口径", evidence, settings).verdict == "insufficient"


@pytest.mark.asyncio
async def test_model_insufficient_produces_specific_revision(runtime):
    context, _, _, _ = runtime
    settings = ReviewConfig(enabled=True, model_check=True)
    model = ScriptedModel(
        responses=[
            rejected(finding(requirement_quote="2026 年日期范围", scope_quote="2026 年日期范围")),
            approved(),
        ]
    )
    received = []

    def execute(state):
        received.append(state["messages"][-1].text)
        return {"messages": [AIMessage(content="facts")]}

    await DelegationRunnable(spec(settings), RunnableLambda(execute), reviewer=ResultReviewer(settings, model)).ainvoke(
        {"messages": [HumanMessage(content="query: 核实 2026 年日期范围")]}
    )
    assert len(received) == 2 and received[0] == "query: 核实 2026 年日期范围"
    assert "2026 年日期范围" in received[1] and "保持对象、工具权限和证据隔离" in received[1]
    assert context.call_counts["review"] == 2 and context.call_counts["retry"] == 1


def test_review_configuration_limits_and_default_production_enabled():
    from agent.subagent_registry import load_default_registry

    assert all(s.review_config.enabled for s in load_default_registry().specs.values())
    with pytest.raises(ValueError):
        ReviewConfig(max_refinements=2)
    with pytest.raises(ValueError):
        ReviewConfig(required_tools=["internet_search"])
    with pytest.raises(ValueError):
        ReviewResult(verdict="passed", issues=["unresolved"])


@pytest.mark.asyncio
async def test_parallel_delegations_do_not_share_tool_evidence(runtime):
    context, store, sink, _ = runtime

    async def execute(state):
        name = state["messages"][-1].text
        await asyncio.sleep(0)
        if name == "with evidence":
            await HookToolMiddleware("search").awrap_tool_call(
                SimpleNamespace(tool_call={"name": "probe", "args": {}, "id": "proof"}),
                RunnableLambda(lambda req: ToolMessage(content="facts", tool_call_id="proof")).ainvoke,
            )
        return {"messages": [AIMessage(content="facts")]}

    settings = ReviewConfig(enabled=True, max_refinements=0, required_tools=("probe",))
    wrapper = DelegationRunnable(spec(settings), RunnableLambda(execute))
    good, bad = await asyncio.gather(
        *(wrapper.ainvoke({"messages": [HumanMessage(content=task)]}) for task in ("with evidence", "without evidence"))
    )
    assert good["messages"][-1].text == "facts" and "insufficient" in bad["messages"][-1].text
    await sink.flush()
    assert {t["check_status"] for t in await store.list_subtasks(context.run_id)} == {"passed", "insufficient"}


@pytest.mark.asyncio
async def test_review_cancellation_propagates_without_refinement(runtime):
    context, store, sink, _ = runtime
    entered = asyncio.Event()

    async def check(messages, config=None):
        entered.set()
        await asyncio.Event().wait()

    settings = ReviewConfig(enabled=True, model_check=True)
    wrapper = DelegationRunnable(
        spec(settings),
        RunnableLambda(lambda state: {"messages": [AIMessage(content="facts")]}),
        reviewer=ResultReviewer(settings, RunnableLambda(check)),
    )
    task = asyncio.create_task(wrapper.ainvoke({"messages": [HumanMessage(content="query")]}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await sink.flush()
    assert (await store.list_subtasks(context.run_id))[0]["status"] == "cancelled"
    assert context.call_counts["retry"] == 0


@pytest.mark.asyncio
async def test_raised_tool_error_is_not_content_refinement(runtime):
    context, store, sink, _ = runtime

    def execute(state):
        raise ValueError("invalid business argument")

    result = await DelegationRunnable(spec(ReviewConfig(enabled=True)), RunnableLambda(execute)).ainvoke(
        {"messages": [HumanMessage(content="task")]}
    )
    assert "tool_failed" in result["messages"][-1].text and context.call_counts["retry"] == 0
    await sink.flush()
    assert (await store.list_subtasks(context.run_id))[0]["check_status"] == "tool_failed"


def test_recovered_tool_attempt_and_empty_evidence():
    settings = ReviewConfig(enabled=True, required_tools=("probe",))
    failed = dict(
        name="probe", call_id="query", executed=True, status="failed", error_category="transient", content=None
    )
    success = dict(failed, status="completed", error_category=None, content="facts")
    assert rule_review("facts", [failed, success], settings).verdict == "passed"
    assert rule_review("facts", [dict(success, content="")], settings).verdict == "insufficient"


@pytest.mark.asyncio
async def test_sync_model_review_budget_and_structure(runtime):
    context, _, _, _ = runtime
    settings = ReviewConfig(enabled=True, model_check=True)
    model = ScriptedModel(responses=[approved()])
    wrapper = DelegationRunnable(
        spec(settings),
        RunnableLambda(lambda state: {"messages": [AIMessage(content="facts")]}),
        reviewer=ResultReviewer(settings, model),
    )
    result = await asyncio.to_thread(wrapper.invoke, {"messages": [HumanMessage(content="query")]})
    assert result["messages"][-1].text == "facts" and context.call_counts["review"] == 1


@pytest.mark.asyncio
async def test_real_db_task_cannot_claim_schema_without_execution(runtime, monkeypatch):
    context, store, sink, _ = runtime
    for key, value in {
        "MYSQL_READONLY_USER": "reader",
        "MYSQL_READONLY_PASSWORD": "fake",
        "MYSQL_DATABASE": "test",
    }.items():
        monkeypatch.setenv(key, value)
    executed = []

    @tool
    def get_table_data(table_name: str):
        """Return real fake-tool column evidence."""
        executed.append("schema")
        return "id,amount\n1,20"

    @tool
    def execute_sql_query(query: str):
        """Return fake read-only query evidence."""
        executed.append("query")
        return "total\n20"

    settings = ReviewConfig(enabled=True, required_tools=("get_table_data", "execute_sql_query"))
    child = ScriptedModel(
        responses=[
            AIMessage(content="已查询表结构和数据库，合计20"),
            call("get_table_data", {"table_name": "orders"}, "schema"),
            call("execute_sql_query", {"query": "SELECT SUM(amount) AS total FROM orders"}, "sql"),
            AIMessage(content="基于实际字段查询，合计20"),
        ]
    )
    parent = ScriptedModel(
        responses=[call("task", {"subagent_type": "db", "description": "统计金额"}), AIMessage(content="done")]
    )
    await build(
        SubagentRegistry([spec(settings, (get_table_data, execute_sql_query), "db")]), parent, {"db": child}
    ).ainvoke({"messages": [("user", "金额")]})
    assert executed == ["schema", "query"] and "get_table_data" in str(child.seen_messages[1])
    await sink.flush()
    task = (await store.list_subtasks(context.run_id))[0]
    assert task["check_status"] == "passed"
    reviews = [e for e in await store.list_events(context.run_id) if e["event"] == "subagent_review"]
    assert [e["status"] for e in reviews] == ["insufficient", "passed"]


@pytest.mark.asyncio
async def test_model_review_failure_reason_is_redacted_in_subtask(runtime):
    context, store, sink, _ = runtime
    settings = ReviewConfig(enabled=True, model_check=True, max_refinements=0)
    model = ScriptedModel(responses=[rejected(finding(requirement_quote="password=secret-value", scope_quote="日期"))])
    wrapper = DelegationRunnable(
        spec(settings),
        RunnableLambda(lambda state: {"messages": [AIMessage(content="facts")]}),
        reviewer=ResultReviewer(settings, model),
    )
    await wrapper.ainvoke({"messages": [HumanMessage(content="核实 password=secret-value 日期")]})
    await sink.flush()
    assert "secret-value" not in str(await store.history(context.run_id))
    assert (await store.list_subtasks(context.run_id))[0]["check_status"] == "insufficient"


@pytest.mark.asyncio
async def test_exhausted_search_refinement_retains_real_source_evidence(runtime):
    context, store, sink, _ = runtime
    actual = []
    url = "https://example.test/actual-source"

    @tool
    def internet_search(query: str):
        """Return a genuine tool response for the scripted framework run."""
        actual.append(query)
        return {"results": [{"url": url, "content": "forecast"}]}

    child = ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "internet_search", "args": {"query": "first"}, "id": "one", "type": "tool_call"},
                    {"name": "internet_search", "args": {"query": "second"}, "id": "two", "type": "tool_call"},
                ],
            ),
            AIMessage(content="draft without citations"),
            AIMessage(content="facts " + url),
        ]
    )
    parent = ScriptedModel(
        responses=[
            call("task", {"subagent_type": "search", "description": "original forecast task"}),
            AIMessage(content="done"),
        ]
    )
    settings = ReviewConfig(enabled=True, required_tools=("internet_search",), require_sources=True)
    await build(SubagentRegistry([spec(settings, (internet_search,))]), parent, {"search": child}).ainvoke(
        {"messages": [("user", "forecast")]}
    )
    assert sorted(actual) == ["first", "second"] and len(context.search_calls) == 2
    final_context = child.seen_messages[-1]
    final_payload = __import__("json").loads(final_context[-1].text)
    assert {tool["call_id"] for tool in final_payload["tools"] if tool["executed"]} == {"one", "two"}
    assert all(tool["content"]["results"][0]["url"] == url for tool in final_payload["tools"] if tool["executed"])
    assert "original forecast task" in str(final_context)
    await sink.flush()
    assert (await store.list_subtasks(context.run_id))[0]["check_status"] == "passed"


@pytest.mark.asyncio
async def test_actual_framework_binds_none_after_initial_search_slots(runtime):
    class ChoiceModel(ScriptedModel):
        choices: list = Field(default_factory=list)

        def bind_tools(self, tools, **kwargs):
            self.choices.append(kwargs.get("tool_choice"))
            return super().bind_tools(tools, **kwargs)

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if "tool_choice" in kwargs:
                self.choices.append(kwargs["tool_choice"])
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    @tool
    def internet_search(query: str):
        """Return verified source data through the real framework."""
        return {"results": [{"url": "https://example.test/source", "content": "facts"}]}

    child = ChoiceModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "internet_search", "args": {"query": "one"}, "id": "one", "type": "tool_call"},
                    {"name": "internet_search", "args": {"query": "two"}, "id": "two", "type": "tool_call"},
                ],
            ),
            AIMessage(content="facts https://example.test/source"),
        ]
    )
    parent = ScriptedModel(
        responses=[call("task", {"subagent_type": "search", "description": "lookup"}), AIMessage(content="done")]
    )
    settings = ReviewConfig(enabled=True, required_tools=("internet_search",), require_sources=True)
    await build(SubagentRegistry([spec(settings, (internet_search,))]), parent, {"search": child}).ainvoke(
        {"messages": [("user", "lookup")]}
    )
    assert child.choices[0] == {"type": "function", "function": {"name": "internet_search"}}
    assert child.choices[-1] == "none"
