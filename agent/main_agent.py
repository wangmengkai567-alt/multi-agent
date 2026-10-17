"""Async agent runtime; importing this module does not create clients or files."""

import asyncio
import shutil
from contextlib import AsyncExitStack, aclosing
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Overwrite

from agent.runtime.backend import run_backend
from agent.runtime.context import RunContext, get_run_context, reset_run_context, set_run_context
from agent.runtime.hooks import HookCall, HookToolMiddleware, pipeline
from agent.runtime.models import budget_model
from agent.runtime.policies import load_runtime_policy
from agent.runtime.workflows import finalize_answer, instruction
from api.context import reset_session_context, set_session_context, set_thread_context
from api.monitor import monitor
from utils.path_utils import confined_path, validate_filename, validate_identifier

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
    from agent.results import RESULT_DISPLAY_PROMPT
    from agent.runtime.skills import ProjectSkillsMiddleware
    from agent.subagent_factory import build_subagents
    from agent.subagent_registry import load_default_registry
    from tools.markdown_tools import generate_markdown
    from tools.pdf_tools import convert_md_to_pdf
    from tools.upload_file_read_tool import read_file_content

    registry = load_default_registry()
    subagents = build_subagents(registry, model)
    return create_deep_agent(
        model=budget_model(model, "main_model"),
        backend=run_backend,
        system_prompt=main_agent_content["system_prompt"] + "\n" + RESULT_DISPLAY_PROMPT,
        tools=[generate_markdown, convert_md_to_pdf, read_file_content],
        checkpointer=saver,
        subagents=subagents,
        middleware=[
            ProjectSkillsMiddleware(),
            HookToolMiddleware(allowed_targets=registry.specs, managed_delegation=True),
        ],
    )


async def initialize_agent():
    """Initialize once per service lifetime, including concurrent callers."""
    global main_agent, checkpointer, _resources
    async with _initialization_lock:
        if main_agent is not None:
            return main_agent
        stack = AsyncExitStack()
        try:
            load_runtime_policy()  # Reject malformed limits before opening resources or constructing clients.
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
    validate_identifier(session_id)
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
        if standalone:
            pipeline.emit("before_run", HookCall("main", context.run_id))
        confined_path(".", session_dir)
        session_dir.mkdir(parents=True, exist_ok=True)
        upload_dir = project_root_path / "updated" / f"session_{session_id}"
        upload_prompt = ""
        if upload_dir.exists():
            files = [path for path in upload_dir.iterdir() if path.is_file()]
            for path in files:
                validate_filename(path.name)
                source = confined_path(str(path), project_root_path / "updated")
                target = confined_path(f"uploads/{path.name}", session_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            if files:
                upload_prompt = "\n[已上传文件] 本轮工作目录中的只读文件：\n" + "\n".join(
                    f"uploads/{path.name}" for path in files
                )
        path_instruction = f"""
        【工作环境指令】
        本轮虚拟文件系统根目录为 /，工具已自动绑定本轮工作目录。
        {upload_prompt}
        生成 Markdown 时 filename 只传文件名（如 report.md），path 留空或传相对子目录（如 reports）。
        不要重复拼接 output/、session_ 或 run_ 目录，也不要传本机绝对路径。
        读取上传文件时传 uploads/文件名；存在上传文件时请先读取并分析内容。
        业务文件工具使用相对路径。框架文件工具使用本轮虚拟路径，如 /report.md、/uploads/文件名。
        /skills/ 是独立只读的任务说明目录，不能写入；uploads/ 中的用户源文件也不能改写；results/ 中的查询原文只读。
        禁止访问本机真实绝对路径或其他轮次目录。
        """
        path_instruction += instruction(context)
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
            final_answer = await finalize_answer(context, final.text)
            pipeline.emit("after_run", HookCall("main", context.run_id, status="completed", result=final_answer))
            monitor.report_task_result(final_answer)
            return final_answer
        return final.text
    except BaseException as exc:
        if standalone:
            status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            pipeline.emit("on_error", HookCall("main", context.run_id, status=status, error=exc))
            pipeline.emit("after_run", HookCall("main", context.run_id, status=status, error=exc))
            monitor._emit("error", f"执行主智能发生异常信息：{exc}")
        raise
    finally:
        reset_session_context(session_token, thread_token)
        reset_run_context(run_token)
