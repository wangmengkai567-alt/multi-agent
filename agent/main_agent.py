"""Async agent runtime; importing this module does not create clients or files."""

import asyncio
import shutil
from contextlib import AsyncExitStack, aclosing
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Overwrite

from agent.runtime.context import RunContext, get_run_context, reset_run_context, set_run_context
from api.context import reset_session_context, set_session_context, set_thread_context
from api.monitor import monitor

checkpoint_db_path = Path(__file__).parents[1] / "data" / "checkpoints.db"
main_agent = None
checkpointer = None
_resources = None
_initialization_lock = asyncio.Lock()


def _build_agent(saver):
    # These modules load credentials and construct clients, so import only at startup.
    from deepagents import create_deep_agent

    from agent.llm import model
    from agent.prompts import main_agent_content
    from agent.subagents.database_query_agent import database_query_agent
    from agent.subagents.knowledge_base_agent import knowledge_base_agent
    from agent.subagents.network_search_agent import network_search_agent
    from tools.markdown_tools import generate_markdown
    from tools.pdf_tools import convert_md_to_pdf
    from tools.upload_file_read_tool import read_file_content

    return create_deep_agent(
        model=model,
        system_prompt=main_agent_content["system_prompt"],
        tools=[generate_markdown, convert_md_to_pdf, read_file_content],
        checkpointer=saver,
        subagents=[database_query_agent, network_search_agent, knowledge_base_agent],
    )


async def initialize_agent():
    """Initialize once per service lifetime, including concurrent callers."""
    global main_agent, checkpointer, _resources
    async with _initialization_lock:
        if main_agent is not None:
            return main_agent
        stack = AsyncExitStack()
        try:
            checkpoint_db_path.parent.mkdir(parents=True, exist_ok=True)
            saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(str(checkpoint_db_path)))
            await saver.setup()
            agent = _build_agent(saver)
        except BaseException:
            await stack.aclose()
            raise
        checkpointer, main_agent, _resources = saver, agent, stack
        return main_agent


async def shutdown_agent():
    """Call after active executions are cancelled and awaited."""
    global main_agent, checkpointer, _resources, _initialization_lock
    async with _initialization_lock:
        if _resources is not None:
            await _resources.aclose()
        main_agent = checkpointer = _resources = None
    _initialization_lock = asyncio.Lock()


project_root_path = Path(__file__).parents[1].resolve()


async def run_deep_agent(task_query, session_id):
    """Run the root graph using this round's context and output directory."""
    context = get_run_context()
    standalone = context is None
    if standalone:
        run_id = str(uuid4())
        context = RunContext(
            run_id, session_id, project_root_path / "output" / f"session_{session_id}" / f"run_{run_id}"
        )
    if context.thread_id != session_id:
        raise ValueError("Run context belongs to another session")
    run_token = set_run_context(context)
    session_dir = context.work_dir
    session_token = set_session_context(str(session_dir))
    thread_token = set_thread_context(session_id)
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        relative_dir = session_dir.relative_to(project_root_path).as_posix()
        upload_dir = project_root_path / "updated" / f"session_{session_id}"
        upload_prompt = ""
        if upload_dir.exists():
            files = [path for path in upload_dir.iterdir() if path.is_file()]
            for path in files:
                shutil.copy2(path, session_dir / path.name)
            if files:
                upload_prompt = "\n[已上传文件] 本轮工作目录中的文件：\n" + "\n".join(path.name for path in files)
        path_instruction = f"""
        【工作环境指令】
        工作目录: {relative_dir}
        {upload_prompt}
        新生成文件必须保存到本轮工作目录 '{relative_dir}/filename'。
        读取上传文件时直接传文件名，不带目录前缀；存在上传文件时请先读取并分析内容。
        使用相对路径，禁止使用绝对路径。
        """
        config = {"configurable": {"thread_id": session_id}}
        agent = await initialize_agent()
        monitor.report_session_dir(str(session_dir))
        stream = agent.astream(
            {"messages": [{"role": "user", "content": task_query + path_instruction}]},
            config=config,
            stream_mode="updates",
        )
        seen_events = set()
        async with aclosing(stream):
            async for chunk in stream:
                # updates keys identify graph nodes; message type defines the action.
                for state in chunk.values():
                    if not isinstance(state, dict):
                        continue
                    messages = state.get("messages", [])
                    if isinstance(messages, Overwrite):
                        messages = messages.value
                        if isinstance(messages, list | tuple):
                            current = [
                                i
                                for i, item in enumerate(messages)
                                if isinstance(item, HumanMessage) and item.content == task_query + path_instruction
                            ]
                            if current:
                                messages = messages[current[-1] + 1 :]
                            elif any(isinstance(item, HumanMessage) for item in messages):
                                continue
                    if isinstance(messages, AIMessage | ToolMessage):
                        messages = [messages]
                    if not isinstance(messages, list | tuple):
                        continue
                    for message in messages:
                        if isinstance(message, ToolMessage):
                            marker = ("returned", message.tool_call_id)
                            if marker not in seen_events:
                                seen_events.add(marker)
                                monitor.report_tool_result(message)
                        if not isinstance(message, AIMessage):
                            continue
                        for call in message.tool_calls:
                            marker = ("requested", call["id"])
                            if marker in seen_events:
                                continue
                            seen_events.add(marker)
                            if call["name"] == "task":
                                args = call["args"]
                                monitor.report_assistant(
                                    args["subagent_type"], {"description": args["description"]}, call_id=call["id"]
                                )
                            else:
                                monitor.report_tool_request(call["name"], call["args"], call["id"])

        # Only the completed root state is authoritative. Intermediate AI text,
        # tool messages and subagent updates are not final task answers.
        snapshot = await agent.aget_state(config)
        messages = snapshot.values.get("messages", [])
        final = messages[-1] if messages else None
        if snapshot.next or not isinstance(final, AIMessage) or final.tool_calls or not final.text:
            raise RuntimeError("Agent completed without a final assistant answer")
        if standalone:
            monitor.report_task_result(final.text)
        return final.text
    except Exception as exc:
        if standalone:
            monitor._emit("error", f"执行主智能发生异常信息：{exc}")
        raise
    finally:
        reset_session_context(session_token, thread_token)
        reset_run_context(run_token)
