# 第 06 步：Hook 统一执行入口

日期：2026-10-14（Asia/Shanghai）。状态：第 06 步完成。

## 修改前状态

实施目录：`D:\My_workspace\Multi-agent\Multi-agent`。

```text
git status --short
（输出为空）
git branch --show-current
main
git rev-parse HEAD
2e48f9a48e4e97f5b9e8d36007597e64807f4360
git log -3 --oneline
2e48f9a 事件保存与推送
ddf8c79 为每轮任务建立独立记录
a281216 修复异步执行与生命周期管理
```

没有用户已有修改需要移动或覆盖。本步按实施计划单独提交，不推送，不提交密钥、上传文件、会话数据库或本地运行输出。

## 已接通的调用链

新增 `agent/runtime/hooks.py`，提供固定名称、按处理器声明顺序执行的 HookPipeline。没有插件优先级或任意排序机制。

| Hook | 实际入口与行为 |
|---|---|
| before_run | API 绑定 RunContext 后、调用主执行器前；独立脚本由主执行器接入。核对上下文，沿用每轮独立的调用计数字段，后续规则可在此准备限制 |
| before_delegate | 框架 task handler 前，核对注册目标并运行控制处理器；拒绝后不调用子 Agent |
| before_tool | AgentMiddleware 的同步 wrap_tool_call / 异步 awrap_tool_call 中，实际 handler 之前 |
| after_tool | finally 中记录执行状态、返回类型与长度、异常分类、耗时和是否实际调用 handler |
| after_subagent | task 执行结束后保存返回元信息与检查状态；独立调用编译执行器也有明确包装入口 |
| after_run | API finally 中通过 finish_run 保存业务答案、错误和终态；独立脚本也触发此 Hook |
| on_error | 拒绝、工具异常、标准错误 ToolMessage / Command、主执行失败和取消路径中分类并记录 |

控制处理器以 HookRejected 拒绝动作，工具拒绝返回原 tool_call_id 对应的错误 ToolMessage。已经执行的业务结果不因记录处理器异常被重新执行、改写或改判。

`main_agent._build_agent` 明确为主 Agent 和三个普通子 Agent 配置 HookToolMiddleware，父配置不会被假定自动继承。主 Agent 的三个业务文件工具及 write_todos、ls、read_file、write_file、edit_file、glob、grep 均通过相同包装。当前默认 StateBackend 没有 execute 工具。

框架默认 general-purpose 子 Agent 与未知目标在 task handler 前拒绝。仅允许现有三个注册名称；稳定 ID 与注册表重构仍属于第 08 步。

HookSubagentRunnable 为自行构建的 CompiledSubAgent 提供同步和异步执行入口；内部 create_agent 必须显式配置 HookToolMiddleware。真实框架测试已经验证此配置。task 已经拥有委派生命周期时，编译执行器不会重复记录 before_delegate / after_subagent。

## 事件、失败与清理

- 继续使用第 05 步 EventSink，先持久化再推送，不增加事件数据库或另一套 monitor 协议。
- 子工具沿用框架 tool_call_id，parent_call_id 指向 task 调用。同步工具线程也能读取该调用上下文。
- 旧业务函数的 report_tool 在包装内转为 tool_progress 并关联当前 call_id；模型请求、真实执行入口、业务进度、返回保持不同语义。
- 原始工具返回原样传给框架，支持 ToolMessage 和 Command。持久记录保存返回元信息，参数继续脱敏；不复制完整上传正文或原始错误工具响应到事件中。
- 子 Agent 检查状态明确保存为 `not_checked`，不会将尚未实施的第 09 步检查标成通过。合格结果正文存储和传递属于第 10 步。
- 记录处理器失败继续运行后续记录处理器，日志明确标记 `Hook record was NOT saved`，在 RunContext 中累计错误。结束时保存已产生的业务结果，终态事件附带 `recording_status=incomplete` 与错误信息，并抛 EventPersistenceError 供后台任务完成回调读取。
- 异步事件入库失败同样在终态事件标记记录不完整；最终状态自身入库失败仍明确抛出，不声称已经保存。
- 成功、拒绝、异常和取消均通过 finally 清理 active_call；API 和主执行器清理各自绑定的上下文，after_run 完成终态处理。

## 验证范围

新增 `tests/test_hook_pipeline.py`，25 项离线测试覆盖：

- 同步、异步的成功 / 拒绝 / 工具异常顺序及真实 handler 调用次数；拒绝时计数为零。
- 所有当前框架默认工具和假业务文件工具均无法绕过 before_tool；拒绝写入后 files 与 todos 状态未改变。
- 真实 task 路由到普通子 Agent、自行构建执行器；子工具执行一次，结果返回一次。
- 默认通用目标、未知目标、已注册但被控制 Hook 拒绝的目标均不执行子 Agent。
- 生产 `_build_agent` 接入三个离线助手，核对实际执行、SQLite Hook 事件、父子关联与敏感正文不进入事件。
- 工具取消、标准错误响应、Command 原样返回、固定处理器顺序、直接调用编译执行器时的控制入口与清理。
- 主任务成功 / 失败 / 取消触发 before_run、on_error、after_run，并保存正确终态、清理上下文。
- 执行前后的记录失败都不改变业务结果；最终事件明确标记记录不完整。

修改 `tests/test_run_history.py` 的失败记录断言：单独断言唯一业务终态 error，再验证新增 on_error 分类事件，避免把两个不同含义的事件误当作重复终态。

## 实际检查

项目根目录 PowerShell：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
& '.\.venv\Scripts\python.exe' -m pytest tests/test_hook_pipeline.py -q --tb=short
```

```text
25 passed in 11.17s
```

```powershell
& '.\.venv\Scripts\python.exe' -m ruff check agent/runtime agent/main_agent.py api/server.py api/monitor.py tests/test_hook_pipeline.py tests/test_run_history.py
& '.\.venv\Scripts\python.exe' -m ruff format --check agent/runtime agent/main_agent.py api/server.py api/monitor.py tests/test_hook_pipeline.py tests/test_run_history.py
& '.\.venv\Scripts\python.exe' -m pip check
git diff --check
```

```text
All checks passed!
9 files already formatted
No broken requirements found.
（git diff --check 输出为空）
```

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q --tb=short
```

```text
92 passed in 53.06s
```

全套覆盖本步 Hook 测试及此前框架、生命周期、运行互斥、事件保存和历史读取测试，没有失败或跳过项。

初次 Hook 测试有 3 项测试配置缺少 system_prompt，已补全。初次全套回归为 81 passed / 1 failed，失败原因是旧断言将新 on_error 分类事件计入终态数量，已按上述语义修正。初次 Ruff 提示循环 lambda 未绑定变量，已修复，没有忽略这些失败。

真实模型、MySQL、Tavily、RAGFlow 联调：未执行。本步不启动业务服务，不读取真实凭据，不触发付费请求；验证目标是当前框架和项目执行入口的控制与记录契约。

SQL、路径、权限、调用次数等实际执行规则仍待第 07 步；结果检查待第 09 步。本步不声称这些后续能力已经完成。
