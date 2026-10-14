"""Async agent runtime; importing this module does not create clients or files."""

import asyncio
import shutil
from contextlib import AsyncExitStack, aclosing
from pathlib import Path

from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Overwrite

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


project_root_path = Path(__file__).parents[1].resolve()  # 绝对 解析路径标识以及软连接


# project_root_path = Path(__file__).parents[1].absolute() # 绝对
async def run_deep_agent(task_query, session_id):
    """
    定义流式+异步执行主智能体！！
    执行过程中，返回  会话文件化返回  调用子智能体  调用最终结果 （monitor）
    task_query: 前端提问的问题
    session_id: 每个前端会话对应的标识 （1.存储session_id ContextVars 2.session_id 给他创建对应的output输出地址）
    """
    print(f"当前会话的main_agent开始执行了！ 会话id:{session_id}")
    # 准备工作 【1. session_dir（前端） 2. relative_session_dir (大模型) 3. 上传的文件拼接上传文件专属提示词】
    # project_root_path / output / session_session_id(uuid)
    # 当前会话存储生成文件的专属文件夹
    session_dir = project_root_path / "output" / f"session_{session_id}"
    # 文件夹可能没有，第一次请求要创建
    session_dir.mkdir(parents=True, exist_ok=True)
    # \  \n \t -> /
    session_dir_str = str(session_dir).replace("\\", "/")
    # 获取相对文件夹
    # session_dir : project_root_path / output / session_session_id(uuid)
    # project_root_path : project_root_path
    # relative_session_dir_str: / output / session_session_id(uuid)
    relative_session_dir_str = str(session_dir.relative_to(project_root_path)).replace("\\", "/")

    # 处理上传文件 （updated / session_session_id）
    updated_dir_path = project_root_path / "updated" / f"session_{session_id}"
    updated_info_prompt = ""  # 有上传文件，拼接上传文件专属解析位置的提示词
    if updated_dir_path.exists():
        # 有
        files = [f.name for f in updated_dir_path.iterdir() if f.is_file()]
        # 将上传文件统一赋值到 output_dir 方便前端统一读取 session_dir
        if files:
            for filename in files:
                # 将原文件 -》 复制 -》 目标文件中  （copy2 保留原文件修改时间和权限等元数据）
                shutil.copy2(updated_dir_path / filename, session_dir / filename)
            # 构建提示词！告诉大模型，有上传文件，你要读取上传文件！！
            updated_info_prompt = (
                "\n    [已上传文件] 已加载到工作目录:\n"
                + "\n".join([f"    - {f}" for f in files])
                + "\n    请优先使用工具（read_file_content）读取并参考这些文件。"
            )

    # 继续准备 1. 当前会话的对应的session_id session_dir 存储到contextVars [后续工具获取，socket -> 推送消息] 2.调用monitor给前端推送session_dir信息
    session_dir_token = set_session_context(session_dir_str)  # 存储的当前会话对应的文件夹地址
    session_id_token = set_thread_context(session_id)  # 获取当前会话的session_id对应socket

    # 执行main_agent
    config = {"configurable": {"thread_id": session_id}}

    # 构建提示词
    path_instruction = f"""
    【工作环境指令】
    工作目录: {relative_session_dir_str}
    {updated_info_prompt}

    规则：
    1. 新生成文件必须保存到工作目录：'{relative_session_dir_str}/filename'
    2. 读取已上传的文件时，请直接将文件名（例如：'开篇.txt'）作为 filename 参数传入（read_file_content）读取工具，不要带上任何目录前缀。
    3. 使用相对路径，禁止使用绝对路径
    4. 若存在上传文件，请先分析内容
    """
    try:
        agent = await initialize_agent()
        monitor.report_session_dir(session_dir_str)
        stream = agent.astream(
            {"messages": [{"role": "user", "content": task_query + path_instruction}]},
            config=config,
            stream_mode="updates",
        )
        async with aclosing(stream):
            async for chunk in stream:
                # updates keys identify graph nodes; message type defines the action.
                for state in chunk.values():
                    if not isinstance(state, dict):
                        continue
                    messages = state.get("messages", [])
                    if isinstance(messages, Overwrite):
                        messages = messages.value
                    if isinstance(messages, AIMessage):
                        messages = [messages]
                    if not isinstance(messages, list | tuple):
                        continue
                    for message in messages:
                        if not isinstance(message, AIMessage):
                            continue
                        for call in message.tool_calls:
                            if call["name"] == "task":
                                args = call["args"]
                                monitor.report_assistant(args["subagent_type"], {"description": args["description"]})

        # Only the completed root state is authoritative. Intermediate AI text,
        # tool messages and subagent updates are not final task answers.
        snapshot = await agent.aget_state(config)
        messages = snapshot.values.get("messages", [])
        final = messages[-1] if messages else None
        if snapshot.next or not isinstance(final, AIMessage) or final.tool_calls or not final.text:
            raise RuntimeError("Agent completed without a final assistant answer")
        monitor.report_task_result(final.text)
        return final.text
    except Exception as exc:
        monitor._emit("error", f"执行主智能发生异常信息：{exc}")
        raise
    finally:
        reset_session_context(session_dir_token, session_id_token)
