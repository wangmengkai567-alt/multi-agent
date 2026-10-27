# 第 03 步：异步执行与生命周期管理

日期：2026-10-14（Asia/Shanghai）。状态：第 03 步完成。

## 修改前的基线

实施仓库：`D:\My_workspace\Multi-agent\Multi-agent`，分支 `main`。
HEAD：`f70d8c6ddf71fdb165e6a7359be6622ac4409be8`（增加 skill 和 hook 接口并验证）。
修改前 `git -C Multi-agent status --short` 输出为空。
实施过程中发现 `README.md` 出现非本步修改；保留原样，不暂存、不提交该文件。

## 实现行为

- `agent/main_agent.py` 使用已验证的 AsyncSqliteSaver，依赖版本保持第 02 步组合不变；aiosqlite 0.22.1 与 langgraph-checkpoint-sqlite 3.0.3 已是直接锁定依赖，无需重复新增。
- initialize_agent 在异步锁内打开数据库、执行 saver.setup、加载模型/工具/子 Agent 并构建图。并发或重复调用返回同一对象；构建失败或取消初始化时关闭已打开的连接，不发布部分初始化对象。
- 模块导入不建立模型、数据库连接或输出目录，也不加载 .env；构建业务客户端的导入延迟到初始化。移除 server/monitor 导入时的全局标准输出编码改写。静态目录声明保留，输出目录在启动时创建。
- FastAPI lifespan 启动时初始化并绑定 monitor 循环；初始化失败则不进入服务阶段。关闭时禁止新任务，取消并等待后台执行，清理推送协程和 WebSocket，最后关闭 checkpoint 连接。
- API 保存后台任务强引用；完成回调调用 exception()、记录异常堆栈并移除引用。运行器推送错误后重新抛出，取消保持 CancelledError 语义，finally 恢复 ContextVars。
- monitor 同步工具线程通过 call_soon_threadsafe 安排推送，实际 asyncio Task 在服务循环中保存；完成回调读取异常。关闭时取消并等待真实推送协程，避免只取消线程安全 Future 却未等实际协程结束。
- 明确使用 stream_mode="updates"；按 AIMessage 类型处理 task 请求，不依赖 model 节点名称；兼容普通消息列表、单个 AIMessage 和 Overwrite.value。流式执行使用 aclosing，解析出错也关闭生成器。
- 流正常结束后读取 aget_state 的根状态，只在无待执行节点、最后消息为无工具请求且有正文的 AIMessage 时推送一次最终答案。支持文本内容块；中间正文和 ToolMessage 不作最终答案。

## 验证范围

新增 `tests/test_agent_lifecycle.py`，12 项测试全部使用可控 Agent、假模型与临时目录/SQLite。

1. 独立 Python 进程导入模块，禁止 mkdir/sqlite3.connect，确认没有加载模型及业务客户端。
2. 8 个并发初始化复用同一对象，只构建一次。
3. 真实 create_deep_agent 使用异步流式与 SQLite 保存答案，关闭连接后重开仍能读取状态。
4. 构建失败关闭连接，后续可以重新初始化。
5. 真实 StateGraph 使用不同节点名与文本块，最终答案只上报一次，恢复上下文。
6. 可控流通过 API 后台任务正常完成并清理引用。
7. 可控流报错向前端报告，并由完成回调读取、记录异常。
8. 关闭时取消并等待执行和推送任务，然后释放数据库。
9. 最后消息为工具结果时判为失败，不报告成功。
10. 推送异常被完成回调读取、记录。
11. 消息解析报错时关闭流并恢复上下文。
12. 启动失败拒绝进入服务阶段；同步工具线程投递的实际推送协程在关闭时被等待。

其中正常/报错后台路径参数化为两项；上述清单按验证行为分组，测试实际总数为 12。

首轮真实框架测试发现消息更新可能是 Overwrite，导致 TypeError；已修复并重新通过。该失败不是跳过或忽略。

## 检查命令与实际输出

项目根目录，项目 `.venv`（Python 3.11.15），保持离线测试配置：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
.venv/Scripts/python.exe -m pytest tests/test_agent_lifecycle.py -q --tb=short
```

```text
12 passed in 5.94s
```

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
.venv/Scripts/python.exe -m pytest -q --tb=short
```

```text
30 passed in 13.56s
```

```powershell
.venv/Scripts/python.exe -m pip check
.venv/Scripts/python.exe -m ruff check agent/main_agent.py api/server.py api/monitor.py tests
.venv/Scripts/python.exe -m ruff format --check agent/main_agent.py api/server.py api/monitor.py tests
 git diff --check
```

```text
No broken requirements found.
All checks passed!
7 files already formatted
```

`git diff --check` 无输出、退出码 0。

## 交付与限制

本步修改 main_agent、server、monitor，更新 conftest 的过时说明，新增生命周期测试和本记录，更新计划第 03 步复选框。单独提交本步代码和记录，不纳入 README、密钥、用户文件、本地输出或数据库。

真实模型、MySQL、RAGFlow、Tavily 联调：**未执行**。对外监听的服务启动：**未执行**，无需清理监听端口；lifespan 的启动/关闭在离线测试中实际执行。导入与测试不使用真实 checkpoint 数据库。

本步不实现运行记录、同会话互斥、Hook、业务取消接口或事件持久化，这些仍属于第 04 步及以后。Python 无法强制终止已经在线程中运行的同步外部请求；外部超时和晚到结果的业务约束留给第 13 步，不宣称本步具备这些能力。
