# 第 02 步：框架接口与版本验证

日期：2026-10-13（Asia/Shanghai）。状态：第 02 步完成。

结论：**当前版本组合已通过所需的离线接口验证，不升级框架、不改动依赖锁文件。** 后续可以在这组版本上接入 Skill、工具调用 Hook 与自定义子 Agent。

本步新增框架接口测试，修正 Windows 异步测试的隔离配置，并编写本记录。未修改业务 Agent 或 API，不启动项目服务，不调用真实模型、MySQL、RAGFlow、Tavily，不执行 Git 暂存、提交或推送。

## 1. 最终版本组合

| 组件 | 已安装并验证的版本 |
|---|---|
| Python | 3.11.15，Windows x64 |
| deepagents | 0.4.3 |
| langchain | 1.2.10 |
| langchain-core | 1.2.14 |
| langgraph | 1.0.9 |
| langgraph-checkpoint | 4.0.0 |
| langgraph-checkpoint-sqlite | 3.0.3 |
| aiosqlite | 0.22.1 |

版本继续由 requirements.txt 与 requirements-dev-win-py311.lock 固定。没有必要为这些功能先升级到最新版；未来如果需要当前版本没有的接口，再单独验证升级。

核对依据是虚拟环境中实际安装的源码与运行测试，不是现行在线文档的参数假设：

- `deepagents/graph.py`：主 Agent、普通子 Agent 和默认通用子 Agent 的构建。
- `deepagents/middleware/skills.py`：Skill 元信息发现与提示词注入。
- `deepagents/middleware/subagents.py`：task 参数、子任务执行与返回。
- `langchain/agents/middleware/types.py`：同步、异步工具包装接口。

## 2. 用什么方式验证

新增 `tests/test_framework_contract.py`。

使用 ScriptedModel：每次调用返回预定的 AIMessage，可记录模型实际收到的消息和工具参数结构；额外调用会失败，不循环返回假答案。使用本地假工具，不访问外部服务。

通过真实的 create_deep_agent、create_agent、task 工具路由和 LangGraph 图运行这些消息；不是直接调用自行写的分派函数来代替框架验证。

Skill 文件和 checkpoint 全部在 pytest 临时目录创建。不存在项目业务 Skill 的实现，测试 Skill 仅用于验证框架读取机制。

### 验证范围

| 验证项 | 实际检查 |
|---|---|
| 构建接口 | create_deep_agent 包含 skills、middleware、subagents、backend、checkpointer |
| 本机文件 Skill | 发现名称、描述和正文路径，再实际执行 read_file 读到正文标记 |
| 状态文件 Skill | 用 create_file_data 构造 files，经过同样的实际读取链路 |
| 工具执行前后 Hook | 真实假工具执行发生在 before 与 after 之间 |
| 异步拒绝 | 返回拒绝 ToolMessage，不调用 handler；假工具执行次数为 0 |
| 自定义子图 | task 调用 CompiledSubAgent 的 StateGraph，子图调用自行构建的执行器 |
| 子 Agent 显式配置 | 子执行器读取自己的 Skill，工具经过自己的包装，父 Agent 接收最终结果 |
| 普通子 Agent 配置继承 | 主 Agent Skill 不会自动注入普通命名子 Agent；明确配置 skills 后才可发现 |
| CompiledSubAgent 配置继承 | 使用 runnable 原样执行，不添加父 Agent 的 Skill、文件工具或自定义包装 |
| 默认通用子 Agent | 自动具备主 Agent 配置的 Skill；不自动具备父 Agent 自定义工具包装 |
| 未知目标 | task 返回不存在目标的错误，已注册子 Agent 未被执行 |
| 异步 checkpoint 与流式输出 | updates 返回 model 消息；同 thread_id 可继续；关闭后重新打开数据库仍可读取状态 |
| 同步 checkpoint | 同步 invoke 可保存；配合 ainvoke 会抛 NotImplementedError |

参数化 Skill 后端和普通子 Agent 配置形成总计 14 项框架测试。

**测试证明接口与调用链可用，不证明真实模型会自动选择正确 Skill 或专家。** 自动选择质量、工具权限、业务结果检查和服务可用性留到对应实施步骤。

## 3. Skill 的确定接入方式

### 主 Agent

当前版本支持：

```python
create_deep_agent(
    model=model,
    backend=backend,
    skills=["/skills/"],
    middleware=[project_tool_middleware],
)
```

`/skills/` 指向包含各 Skill 子目录的来源，不是直接指向单个 SKILL.md。元信息先进入系统提示词，详细正文由模型通过 read_file 工具读取。

测试在第一次模型调用里发现 Skill 名称和正文路径，同时确认正文独特标记尚未进入上下文；实际 read_file 之后，工具返回与下一次模型上下文里都出现正文标记。因此可以区分“仅登记名称”和“确实读取正文”。

### 文件来源的两个已验证方案

| 来源 | 配置方式 | 限制 |
|---|---|---|
| 临时本机目录 | FilesystemBackend(root_dir=..., virtual_mode=True) | 已验证虚拟路径读取；不等于完整文件沙箱或只读控制 |
| 状态文件 | 默认 StateBackend，输入 files 中使用 create_file_data | 本机文件需由服务端受控读取后加入 files，不能直接把本机目录当作已可见 |

后续 Web 服务优先考虑受控加载 Skill 到状态文件，或经权限包装的组合后端；不能把本次测试用的本机目录读取直接当成生产安全方案。写入 Skill 路径的禁止规则仍需第 07 步实现。

### 普通命名子 Agent

普通字典配置支持单独的 `skills`、`middleware`、`model`、`tools`：

```python
{
    "name": "search",
    "description": "查询外部信息",
    "system_prompt": "执行查询任务",
    "model": child_model,
    "tools": child_tools,
    "skills": ["/skills/"],
    "middleware": [child_tool_middleware],
}
```

真实配置键是 system_prompt，不能使用 graph.py 部分说明文字中残留的 prompt 示例。主 Agent 指定 skills 后，普通命名子 Agent 不会因此自动得到这些 Skill；主 Agent 自定义 middleware 也不会自动包装子 Agent 内部工具。

### 自行构建的子 Agent

推荐后续用 LangChain create_agent 构建内部执行器，明确加入：

```python
middleware=[
    SkillsMiddleware(backend=backend, sources=["/skills/"]),
    FilesystemMiddleware(backend=backend),
    child_tool_middleware,
]
```

然后按需要用 StateGraph 包装结果检查，作为 CompiledSubAgent 的 runnable 注册。它必须接收和返回带 messages 的状态；父 Agent 读取最后一条消息作为该次 task 的结果。

测试已经验证“子图 → 内部执行器 → read_file → 子结果 → 父 ToolMessage”全过程。父 Agent 不会收到全部子 Agent 查询消息；本次只收到最终子结果。框架还可能共享非排除状态字段，不能把消息分开说成进程或权限隔离。

不要在未来直接给 create_react_agent 传入未经确认的 middleware 参数。此次已验证的包装接入点是 create_agent；现有业务字典可先保留，构建改造在第 08 步进行。

## 4. Hook 的确定接入方式

项目以后通过 AgentMiddleware 的以下方法包装实际工具执行：

```python
class ProjectToolMiddleware(AgentMiddleware):
    def wrap_tool_call(self, request, handler):
        # 执行前检查
        result = handler(request)
        # 执行后处理
        return result

    async def awrap_tool_call(self, request, handler):
        # 执行前检查
        result = await handler(request)
        # 执行后处理
        return result
```

同步与异步版本分别实现，不能只写同步方法后假设异步调用自动使用它。实际测试同时覆盖同步工具前后执行和异步调用拒绝。

调用信息使用 `request.tool_call`，包括 name、args、id。允许执行时调用 handler；拒绝时不调用 handler，返回关联原 tool_call_id 的 ToolMessage。真实工具可能返回 ToolMessage 或 Command，后续包装不可假设所有返回都是字符串。

权限、次数与业务前置条件接入 before_tool/before_delegate 的具体逻辑仍未实现。本步的 ToolRecorder 只放在测试中，不作为生产 Hook 模块。

## 5. 子 Agent 触发参数、流式与默认工具

### task 调用结构

模型可见的 task 参数包含 description 与 subagent_type，runtime 是框架注入对象，不是模型需要生成的字段。

```json
{
  "name": "task",
  "args": {
    "subagent_type": "contract-child",
    "description": "Read local evidence."
  },
  "id": "dispatch-5821"
}
```

实际返回的 ToolMessage 保留同一个 tool_call_id，内容为子 Agent 最后结果；第二次父模型调用能读到该结果。

工具参数结构验证使用 `convert_to_openai_tool`，它会过滤注入参数；直接将完整 args_schema 转为 JSON 可能把运行时对象也纳入，不能把这种测试辅助错误归因于框架不兼容。

### 默认工具的实测范围

本次 StateBackend 和临时 FilesystemBackend 均向模型提供：

```text
write_todos
ls
read_file
write_file
edit_file
glob
grep
task
```

两种非执行后端均没有向模型暴露 execute。其他具备执行能力的后端未验证，不能推断永远没有 execute。

0.4.3 还会自动创建 general-purpose 子 Agent：继承主 Agent 的 Skill 来源及相关默认能力，不自动继承主 Agent 的自定义工具包装。后续调用控制应在 task 执行入口校验允许目标，避免该默认目标绕过专用子 Agent 包装；不能仅移除自定义 subagents 就认为默认通用目标不存在。

### checkpoint 与流式结果

- `stream_mode="updates"` 返回节点名到更新的映射，本次可从 model 节点读取 AIMessage。
- AsyncSqliteSaver 可用于 ainvoke/astream，同 thread_id 延续消息，关闭后重新打开仍能读取 SECOND_RESULT。
- 同步 SqliteSaver 支持 invoke，但在异步执行路径会抛 NotImplementedError。第 03 步修复现有应用配置的依据已经由测试确认。
- 流式模式对齐不等于已经修复现有 API 消息解析，本步未修改主运行代码。

## 6. Windows 离线测试配置修正

首次运行时，网络拒绝逻辑阻止了 Windows asyncio 创建内部通信通道。修复位置为 tests/conftest.py：

1. 标准库 socketpair 创建期间临时允许其内部连接，结束后恢复拒绝状态。
2. 普通 socket.connect、connect_ex 和 create_connection 仍被拒绝，包括用户代码自行连接 loopback。
3. 新增隔离测试，证明内部 socketpair 可工作，随后任意 loopback 连接仍失败。
4. 禁用 LangSmith/LangChain 追踪，并清除测试进程的 LangSmith 凭据，避免假模型测试自动上传调用记录。

没有为测试放开一般网络权限，没有忽略 asyncio 警告，也没有改变业务进程的环境变量；上述配置只在 pytest 进程生效。

另一个初次验证失败来自假模型读取了包含注入 runtime 的完整工具 schema，现已改用框架标准的工具参数转换。两项问题均已修正，最终测试没有失败或跳过项。

## 7. 实际检查输出

从项目根目录执行：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
& '.\.venv\Scripts\python.exe' -m pytest tests/test_framework_contract.py -q --tb=line
```

```text
14 passed in 9.82s
```

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q --tb=line
```

```text
18 passed in 9.64s
```

18 项包括 14 项框架接口测试和 4 项测试环境隔离检查，没有现有应用端到端测试。

```powershell
& '.\.venv\Scripts\python.exe' -m pip check
& '.\.venv\Scripts\python.exe' -m ruff check tests
& '.\.venv\Scripts\python.exe' -m ruff format --check tests
```

```text
No broken requirements found.
All checks passed!
3 files already formatted
```

由于没有升级版本，“升级前后比较”的结论为：核心版本、依赖锁定与默认 API 未改变；新增测试固定了工具参数、默认工具、子任务返回、流式与 checkpoint 行为。此步没有对未安装的新版本作比较或兼容承诺。

## 8. 文件变更与下一步依据

新增：

- `tests/test_framework_contract.py`：当前框架真实接口测试。
- `docs/implementation/02-framework.md`：本记录。

修改：

- `tests/conftest.py`：Windows 内部通信例外与追踪隔离。
- `tests/test_test_environment.py`：内部通信例外没有放开普通网络的验证。

没有修改 requirements、锁文件、业务 Agent、API 或前端。没有做 Git add、commit、push。

第 03 步可以在当前版本上实施：将应用改为 AsyncSqliteSaver，将初始化迁入生命周期，再用已验证的 updates 模式适配消息解析。Skill 与统一 Hook 的业务接入仍分别安排在后续步骤，不能将本次接口测试通过说成应用已经拥有这些功能。
