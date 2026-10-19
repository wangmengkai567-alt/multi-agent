"""Acceptance regressions found by the real model and front-end integration."""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from test_agent_lifecycle import isolated_runtime, runtime  # noqa: F401

from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.delegation import DelegationRunnable
from agent.subagent_registry import ReviewConfig, SubagentSpec


@pytest.mark.asyncio
@pytest.mark.usefixtures("isolated_runtime")
async def test_output_instructions_and_markdown_work_with_isolated_storage(sandbox_dir, monkeypatch):
    from tools.markdown_tools import generate_markdown

    directory = sandbox_dir / "acceptance" / "files" / "session_thread" / "run_run"
    directory.mkdir(parents=True)
    context = RunContext("run", "thread", directory, reference_time=datetime.fromisoformat("2026-12-31T23:50:00+08:00"))

    class Model:
        async def astream(self, inputs, **kwargs):
            prompt = inputs["messages"][0]["content"]
            assert "2026-12-31" in prompt and "2027-01-01" in prompt
            assert "path 留空或传相对子目录" in prompt
            assert str(directory) not in prompt
            assert "output/acceptance/files" not in prompt
            generate_markdown.invoke({"content": "# 测试数据\n10笔订单\r\n末行\r结束", "filename": "acceptance.md"})
            yield {"model": {"messages": [AIMessage(content="已生成测试报告")]}}

        async def aget_state(self, config):
            return SimpleNamespace(values={"messages": [AIMessage(content="已生成测试报告")]}, next=())

    monkeypatch.setattr(runtime, "_build_agent", lambda saver: Model())
    token = set_run_context(context)
    try:
        assert await runtime.run_deep_agent("测试", "thread") == "已生成测试报告"
    finally:
        reset_run_context(token)
    assert (directory / "acceptance.md").read_bytes() == "# 测试数据\r\n10笔订单\r\n末行\r\n结束".encode()


@pytest.mark.asyncio
async def test_model_timeout_with_cancellation_cause_is_dependency_failure_without_content_retry(sandbox_dir):
    async def expires(state):
        async with asyncio.timeout(0.01):
            await asyncio.Event().wait()

    spec = SubagentSpec("search", "联网搜索助手", "测试", "测试", (), review_config=ReviewConfig(enabled=True))
    executor = DelegationRunnable(spec, RunnableLambda(expires))
    context = RunContext("run", "thread", sandbox_dir)
    token = set_run_context(context)
    try:
        result = await executor.ainvoke({"messages": [HumanMessage(content="测试模型超时")]})
    finally:
        reset_run_context(token)
    text = result["messages"][-1].text
    assert "dependency_unavailable" in text and "TimeoutError" in text
    assert context.call_counts["retry"] == 0
