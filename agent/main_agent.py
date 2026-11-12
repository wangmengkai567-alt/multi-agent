# 主助手入口：构建带权限和预算约束的智能体，并执行每一轮独立任务。
"""Async agent runtime; importing this module does not create clients or files."""

import asyncio
import shutil
from contextlib import AsyncExitStack, aclosing
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Overwrite

from agent.runtime.backend import run_backend
from agent.runtime.context import RunContext, get_run_context, reset_run_context, set_run_context
from agent.runtime.hooks import HookCall, HookToolMiddleware, pipeline
from agent.runtime.models import BudgetSummarizationMiddleware, budget_model
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
    # 这些模块会加载凭据并创建客户端，延迟到服务启动时导入，避免仅导入模块就产生副作用。
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
    from deepagents.middleware.subagents import SubAgentMiddleware
    from langchain.agents import create_agent

    from agent.llm import create_role_model
    from agent.prompts import main_agent_content
    from agent.runtime.capabilities import READONLY_FILESYSTEM_PROMPT, CapabilityDisclosureMiddleware
    from agent.runtime.contracts import TaskContractMiddleware
    from agent.runtime.evidence_protocol import adapter_for_tool, bind_framework_tools
    from agent.runtime.experts import ExpertRegistryMiddleware, SerialDelegationMiddleware
    from agent.runtime.skills import ProjectSkillsMiddleware, SkillReadMiddleware
    from agent.subagent_factory import build_subagents, validate_model
    from agent.subagent_registry import SummaryConfig, load_default_registry
    from tools.upload_file_read_tool import read_file_content

    # 注册表决定有哪些专家及其模型配置；主助手通过 task 工具使用这些专家。
    registry = load_default_registry()
    profiles = {"MAIN"} | {spec.model_config.profile for spec in registry.specs.values()}
    models = {name: create_role_model(name) for name in sorted(profiles)}
    subagents = build_subagents(registry, models)
    main_model = models["MAIN"]
    validate_model(main_model, needs_tools=True)
    root_model = budget_model(main_model, "main_model")
    tools = [read_file_content]
    for tool in tools:
        adapter_for_tool(tool)
    return create_agent(
        model=root_model,
        system_prompt=main_agent_content["system_prompt"],
        tools=tools,
        checkpointer=saver,
        # 中间件将专家选择、只读文件访问、任务契约和工具执行检查接入同一个图。
        middleware=[
            ExpertRegistryMiddleware(registry),
            SerialDelegationMiddleware(),
            SkillReadMiddleware(),
            bind_framework_tools(FilesystemMiddleware(backend=run_backend, system_prompt=READONLY_FILESYSTEM_PROMPT)),
            bind_framework_tools(SubAgentMiddleware(backend=run_backend, subagents=subagents)),
            BudgetSummarizationMiddleware(model=root_model, backend=run_backend, **SummaryConfig().kwargs()),
            PatchToolCallsMiddleware(),
            TaskContractMiddleware(),
            ProjectSkillsMiddleware(),
            CapabilityDisclosureMiddleware(registry.specs),
            HookToolMiddleware(allowed_targets=registry.specs, managed_delegation=True),
        ],
    ).with_config({"recursion_limit": 1000})


async def initialize_agent():
    """Initialize once per service lifetime, including concurrent callers."""
    global main_agent, checkpointer, _resources
    # 并发请求共享一次初始化；资源全部创建成功后，才向其他请求公开可用实例。
    async with _initialization_lock:
        if main_agent is not None:
            return main_agent
        stack = AsyncExitStack()
        try:
            load_runtime_policy()  # 先校验配置，再打开数据库或创建模型客户端。
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
    # API 已提供本轮上下文；直接调用本函数时才自行创建运行 ID、目录并负责收尾。
    standalone = context is None
    if standalone:
        run_id = str(uuid4())
        context = RunContext(
            run_id,
            session_id,
            project_root_path / "output" / f"session_{session_id}" / f"run_{run_id}",
            user_query=task_query,
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
        # 上传区按会话保存，执行前复制到本轮目录，使工具只访问当前轮次的材料。
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
        from agent.runtime.contracts import bind_contract

        bind_contract(context)
        path_instruction = f"""
        【本轮参考时间】
        当前时间：{context.reference_time.isoformat()}；今天：{context.reference_time.date().isoformat()}；
        明天：{(context.reference_time + timedelta(days=1)).date().isoformat()}。相对日期按此时间解释。
        【工作环境指令】
        本轮虚拟文件系统根目录为 /，工具已自动绑定本轮工作目录。
        {upload_prompt}
        最终正文由程序自动保存为 answer.md，不调用写文件或转换工具。
        不要重复拼接 output/、session_ 或 run_ 目录，也不要传本机绝对路径。
        读取已绑定上传材料时传 uploads/文件名；绑定文本 reference 为 input_text。
        业务文件工具使用相对路径。框架文件工具使用本轮虚拟路径，如 /report.md、/uploads/文件名。
        /skills/ 是独立只读的任务说明目录，不能写入；uploads/ 中的用户源文件也不能改写；results/ 中的查询原文只读。
        禁止访问本机真实绝对路径或其他轮次目录。
        """
        path_instruction += instruction(context)
        # thread_id 是业务会话 ID；图的检查点使用本轮 run_id，避免恢复上一轮的执行状态。
        config = {"configurable": {"thread_id": validate_identifier(context.run_id)}}
        agent = await initialize_agent()
        monitor.report_session_dir(str(session_dir))
        stream_input = {
            "messages": Overwrite(
                [
                    HumanMessage(
                        content=task_query + path_instruction, additional_kwargs={"task_input_run_id": context.run_id}
                    )
                ]
            )
        }
        stream = agent.astream(
            stream_input,
            config=config,
            stream_mode="updates",
        )
        seen_events = set()
        async with aclosing(stream):
            async for chunk in stream:
                # 流式更新按图节点返回；根据消息类型识别工具请求和返回，并按调用 ID 去重。
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
                            if (
                                call["name"] == "task"
                                and isinstance(call["args"].get("subagent_type"), str)
                                and isinstance(call["args"].get("description"), str)
                            ):
                                args = call["args"]
                                monitor.report_assistant(
                                    args["subagent_type"], {"description": args["description"]}, call_id=call["id"]
                                )
                            else:
                                monitor.report_tool_request(call["name"], call["args"], call["id"])

        # 只从已结束的主图状态提取最终答复；中间模型文本、工具消息和子助手更新不算最终结果。
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
        # 用 token 恢复调用前的上下文，防止同一服务中的后续任务串用目录或身份。
        reset_session_context(session_token, thread_token)
        reset_run_context(run_token)
