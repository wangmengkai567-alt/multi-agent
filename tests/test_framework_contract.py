"""Offline contracts against the installed framework, not application clients."""

import inspect
from importlib.metadata import version

import pytest
from deepagents import CompiledSubAgent, create_deep_agent
from deepagents.backends import FilesystemBackend, StateBackend
from deepagents.backends.utils import create_file_data
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.skills import SkillsMiddleware
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from pydantic import Field

SKILL_PATH = "/skills/contract-skill/SKILL.md"
SKILL_BODY = """---
name: contract-skill
description: A local contract test procedure.
---
Read the test evidence before returning CONTRACT_BODY_5821.
"""
DEFAULT_TOOLS = {"write_todos", "ls", "read_file", "write_file", "edit_file", "glob", "grep", "task"}


class ScriptedModel(BaseChatModel):
    """Consume exact responses and fail if the framework calls unexpectedly."""

    responses: list[AIMessage]
    seen_messages: list[list] = Field(default_factory=list)
    tool_names: set[str] = Field(default_factory=set)
    tool_schemas: dict = Field(default_factory=dict)

    @property
    def _llm_type(self):
        return "offline-framework-contract"

    def bind_tools(self, tools, **kwargs):
        for item in tools:
            self.tool_names.add(item.name)
            self.tool_schemas[item.name] = convert_to_openai_tool(item)["function"]["parameters"]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen_messages.append(list(messages))
        if not self.responses:
            raise AssertionError("Unexpected extra model invocation")
        return ChatResult(generations=[ChatGeneration(message=self.responses.pop(0))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop, run_manager, **kwargs)


class ToolRecorder(AgentMiddleware):
    """Real middleware around the handler; optionally refuses without executing."""

    def __init__(self, events, deny=None):
        self.events = events
        self.deny = deny

    def _before(self, request):
        name = request.tool_call["name"]
        self.events.append(("before", name))
        if name == self.deny:
            self.events.append(("denied", name))
            return ToolMessage(content="DENIED_BY_CONTRACT", tool_call_id=request.tool_call["id"])
        return None

    def wrap_tool_call(self, request, handler):
        denied = self._before(request)
        if denied is not None:
            return denied
        response = handler(request)
        self.events.append(("after", request.tool_call["name"]))
        return response

    async def awrap_tool_call(self, request, handler):
        denied = self._before(request)
        if denied is not None:
            return denied
        response = await handler(request)
        self.events.append(("after", request.tool_call["name"]))
        return response


def call(name, args, call_id="contract-call"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def file_backend(sandbox_dir):
    path = sandbox_dir / "skills" / "contract-skill" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(SKILL_BODY, encoding="utf-8")
    return FilesystemBackend(root_dir=sandbox_dir, virtual_mode=True)


def text(messages):
    return "\n".join(str(message.content) for message in messages)


def tool_results(messages):
    return [message for message in messages if isinstance(message, ToolMessage)]


def test_current_versions_and_configuration_interfaces():
    expected = {
        "deepagents": "0.4.3",
        "langchain": "1.2.10",
        "langgraph": "1.0.9",
        "langgraph-checkpoint-sqlite": "3.0.3",
    }
    assert {name: version(name) for name in expected} == expected
    assert {"skills", "middleware", "subagents", "backend", "checkpointer"} <= set(
        inspect.signature(create_deep_agent).parameters
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_kind", ["filesystem", "state"])
async def test_skill_discovery_then_real_read_file_returns_body(sandbox_dir, backend_kind):
    model = ScriptedModel(responses=[call("read_file", {"file_path": SKILL_PATH}), AIMessage(content="read complete")])
    events = []
    backend = file_backend(sandbox_dir) if backend_kind == "filesystem" else StateBackend
    agent = create_deep_agent(model=model, backend=backend, skills=["/skills/"], middleware=[ToolRecorder(events)])
    inputs = {"messages": [("user", "Read the local skill.")]}
    if backend_kind == "state":
        inputs["files"] = {SKILL_PATH: create_file_data(SKILL_BODY)}
    result = await agent.ainvoke(inputs)

    first_prompt = text(model.seen_messages[0])
    assert "contract-skill" in first_prompt
    assert SKILL_PATH in first_prompt
    assert "CONTRACT_BODY_5821" not in first_prompt  # Only metadata is loaded initially.
    assert any("CONTRACT_BODY_5821" in item.content for item in tool_results(result["messages"]))
    assert "CONTRACT_BODY_5821" in text(model.seen_messages[1])
    assert events == [("before", "read_file"), ("after", "read_file")]
    assert DEFAULT_TOOLS <= model.tool_names
    assert "execute" not in model.tool_names  # These backends do not provide shell execution.
    assert not model.responses


def test_sync_tool_handler_runs_between_before_and_after():
    events = []

    @tool
    def fake_query(item: str) -> str:
        """Return local evidence without any service request."""
        events.append(("execute", item))
        return "LOCAL_QUERY_EVIDENCE"

    model = ScriptedModel(responses=[call("fake_query", {"item": "sample"}), AIMessage(content="done")])
    result = create_deep_agent(model=model, tools=[fake_query], middleware=[ToolRecorder(events)]).invoke(
        {"messages": [("user", "Query the sample.")]}
    )
    assert events == [("before", "fake_query"), ("execute", "sample"), ("after", "fake_query")]
    assert tool_results(result["messages"])[0].content == "LOCAL_QUERY_EVIDENCE"


@pytest.mark.asyncio
async def test_async_tool_wrapper_can_deny_before_business_function():
    executions = []
    events = []

    @tool
    async def fake_query(item: str) -> str:
        """An offline function that must never be executed when denied."""
        executions.append(item)
        return "should not execute"

    model = ScriptedModel(responses=[call("fake_query", {"item": "sample"}), AIMessage(content="denied")])
    agent = create_deep_agent(model=model, tools=[fake_query], middleware=[ToolRecorder(events, deny="fake_query")])
    result = await agent.ainvoke({"messages": [("user", "Attempt the query.")]})
    assert executions == []
    assert events == [("before", "fake_query"), ("denied", "fake_query")]
    assert tool_results(result["messages"])[0].content == "DENIED_BY_CONTRACT"


@pytest.mark.asyncio
async def test_task_routes_to_custom_graph_with_explicit_child_skill_and_middleware(sandbox_dir):
    backend = file_backend(sandbox_dir)
    child_events = []
    child = ScriptedModel(
        responses=[call("read_file", {"file_path": SKILL_PATH}), AIMessage(content="CHILD_RESULT_5821")]
    )
    inner = create_agent(
        model=child,
        middleware=[
            SkillsMiddleware(backend=backend, sources=["/skills/"]),
            FilesystemMiddleware(backend=backend),
            ToolRecorder(child_events),
        ],
    )
    received_tasks = []

    async def run_child(state, config):
        received_tasks.append(state["messages"][0].content)
        return await inner.ainvoke(state, config)

    graph = StateGraph(MessagesState)
    graph.add_node("contract_child", run_child)
    graph.add_edge(START, "contract_child")
    graph.add_edge("contract_child", END)
    parent = ScriptedModel(
        responses=[
            call("task", {"subagent_type": "contract-child", "description": "Read local evidence."}, "dispatch-5821"),
            AIMessage(content="PARENT_COMPLETE"),
        ]
    )
    parent_events = []
    agent = create_deep_agent(
        model=parent,
        backend=backend,
        middleware=[ToolRecorder(parent_events)],
        subagents=[CompiledSubAgent(name="contract-child", description="Offline child.", runnable=graph.compile())],
    )
    result = await agent.ainvoke({"messages": [("user", "Delegate this task.")]})
    assert received_tasks == ["Read local evidence."]
    results = tool_results(result["messages"])
    assert len(results) == 1
    assert results[0].tool_call_id == "dispatch-5821"
    assert results[0].content == "CHILD_RESULT_5821"
    assert "CHILD_RESULT_5821" in text(parent.seen_messages[1])
    assert "CONTRACT_BODY_5821" in text(child.seen_messages[1])
    assert "CONTRACT_BODY_5821" not in text(parent.seen_messages[1])
    assert parent_events == [("before", "task"), ("after", "task")]
    assert child_events == [("before", "read_file"), ("after", "read_file")]
    schema = parent.tool_schemas["task"]["properties"]
    assert {"subagent_type", "description"} <= set(schema)
    assert "runtime" not in schema  # Injected runtime is not a model argument.
    assert result["messages"][-1].content == "PARENT_COMPLETE"


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_child_skills", [False, True])
async def test_named_child_does_not_implicitly_receive_parent_skill_or_custom_hook(sandbox_dir, explicit_child_skills):
    backend = file_backend(sandbox_dir)
    parent_events = []
    child_executions = []

    @tool
    def child_query() -> str:
        """Produce local child evidence to check middleware propagation."""
        child_executions.append("executed")
        return "LOCAL_CHILD_EVIDENCE"

    child = ScriptedModel(responses=[call("child_query", {}), AIMessage(content="NAMED_CHILD_DONE")])
    spec = {
        "name": "named-child",
        "description": "A named offline child.",
        "system_prompt": "Return a local result.",
        "model": child,
        "tools": [child_query],
    }
    if explicit_child_skills:
        spec["skills"] = ["/skills/"]
    parent = ScriptedModel(
        responses=[
            call("task", {"subagent_type": "named-child", "description": "Do the local task."}),
            AIMessage(content="done"),
        ]
    )
    agent = create_deep_agent(
        model=parent,
        backend=backend,
        skills=["/skills/"],
        middleware=[ToolRecorder(parent_events)],
        subagents=[spec],
    )
    await agent.ainvoke({"messages": [("user", "Delegate to the named child.")]})
    assert "contract-skill" in text(parent.seen_messages[0])
    assert ("contract-skill" in text(child.seen_messages[0])) is explicit_child_skills
    assert parent_events == [("before", "task"), ("after", "task")]
    assert child_executions == ["executed"]


@pytest.mark.asyncio
async def test_compiled_child_is_used_as_is_without_parent_defaults(sandbox_dir):
    backend = file_backend(sandbox_dir)
    executions = []

    @tool
    def child_query() -> str:
        """Return an offline result in a minimal compiled child."""
        executions.append("child")
        return "COMPILED_EVIDENCE"

    child = ScriptedModel(responses=[call("child_query", {}), AIMessage(content="COMPILED_DONE")])
    inner = create_agent(model=child, tools=[child_query])
    parent = ScriptedModel(
        responses=[
            call("task", {"subagent_type": "minimal-child", "description": "Query locally."}),
            AIMessage(content="done"),
        ]
    )
    events = []
    agent = create_deep_agent(
        model=parent,
        backend=backend,
        skills=["/skills/"],
        middleware=[ToolRecorder(events)],
        subagents=[CompiledSubAgent(name="minimal-child", description="Minimal compiled child.", runnable=inner)],
    )
    await agent.ainvoke({"messages": [("user", "Delegate to the compiled child.")]})
    assert child.tool_names == {"child_query"}
    assert "contract-skill" not in text(child.seen_messages[0])
    assert executions == ["child"]
    assert events == [("before", "task"), ("after", "task")]


@pytest.mark.asyncio
async def test_default_general_purpose_child_inherits_skills_but_not_custom_parent_hook(sandbox_dir):
    backend = file_backend(sandbox_dir)
    model = ScriptedModel(
        responses=[
            call("task", {"subagent_type": "general-purpose", "description": "Read the local skill."}, "gp-task"),
            call("read_file", {"file_path": SKILL_PATH}, "gp-read"),
            AIMessage(content="GENERAL_CHILD_DONE"),
            AIMessage(content="PARENT_DONE"),
        ]
    )
    events = []
    agent = create_deep_agent(model=model, backend=backend, skills=["/skills/"], middleware=[ToolRecorder(events)])
    result = await agent.ainvoke({"messages": [("user", "Use the general-purpose child.")]})
    assert "contract-skill" in text(model.seen_messages[1])
    assert "CONTRACT_BODY_5821" in text(model.seen_messages[2])
    assert tool_results(result["messages"])[0].content == "GENERAL_CHILD_DONE"
    assert events == [("before", "task"), ("after", "task")]
    assert not model.responses


@pytest.mark.asyncio
async def test_unknown_subagent_returns_error_without_calling_child():
    child = ScriptedModel(responses=[AIMessage(content="must not run")])
    parent = ScriptedModel(
        responses=[
            call("task", {"subagent_type": "missing", "description": "Invalid target."}),
            AIMessage(content="error seen"),
        ]
    )
    agent = create_deep_agent(
        model=parent,
        subagents=[{"name": "known", "description": "Known child.", "system_prompt": "Answer.", "model": child}],
    )
    result = await agent.ainvoke({"messages": [("user", "Try an unknown target.")]})
    assert child.seen_messages == []
    assert "does not exist" in tool_results(result["messages"])[0].content


@pytest.mark.asyncio
async def test_async_sqlite_checkpoint_and_stream_updates_preserve_thread_state(sandbox_dir):
    model = ScriptedModel(responses=[AIMessage(content="FIRST_RESULT"), AIMessage(content="SECOND_RESULT")])
    config = {"configurable": {"thread_id": "contract-thread"}}
    async with AsyncSqliteSaver.from_conn_string(str(sandbox_dir / "checkpoint.db")) as saver:
        agent = create_deep_agent(model=model, checkpointer=saver)
        updates = [
            chunk
            async for chunk in agent.astream({"messages": [("user", "First question.")]}, config, stream_mode="updates")
        ]
        assert any("model" in chunk for chunk in updates)
        model_messages = [message for chunk in updates if "model" in chunk for message in chunk["model"]["messages"]]
        assert model_messages[-1].content == "FIRST_RESULT"
        state = await agent.aget_state(config)
        assert state.values["messages"][-1].content == "FIRST_RESULT"
        await agent.ainvoke({"messages": [("user", "Follow-up question.")]}, config)
        assert "FIRST_RESULT" in text(model.seen_messages[1])
        assert (await saver.aget_tuple(config)) is not None
    # Reopen from disk, rather than only looking at the in-memory graph state.
    async with AsyncSqliteSaver.from_conn_string(str(sandbox_dir / "checkpoint.db")) as reopened:
        restored = create_deep_agent(model=ScriptedModel(responses=[]), checkpointer=reopened)
        assert (await restored.aget_state(config)).values["messages"][-1].content == "SECOND_RESULT"


def test_sync_sqlite_saver_is_not_an_async_execution_backend(sandbox_dir):
    model = ScriptedModel(responses=[AIMessage(content="LOCAL_RESULT")])
    config = {"configurable": {"thread_id": "sync-contract"}}
    with SqliteSaver.from_conn_string(str(sandbox_dir / "sync.db")) as saver:
        agent = create_deep_agent(model=model, checkpointer=saver)
        result = agent.invoke({"messages": [("user", "Local question.")]}, config)
        assert result["messages"][-1].content == "LOCAL_RESULT"
        assert saver.get_tuple(config) is not None


@pytest.mark.asyncio
async def test_sync_sqlite_saver_rejects_async_agent_execution(sandbox_dir):
    model = ScriptedModel(responses=[AIMessage(content="should not run")])
    config = {"configurable": {"thread_id": "invalid-async-contract"}}
    with SqliteSaver.from_conn_string(str(sandbox_dir / "invalid.db")) as saver:
        agent = create_deep_agent(model=model, checkpointer=saver)
        with pytest.raises(NotImplementedError):
            await agent.ainvoke({"messages": [("user", "Async with wrong saver.")]}, config)
