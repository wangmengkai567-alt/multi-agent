# 第 08 步：统一子 Agent 注册与触发入口

日期：2026-10-14（Asia/Shanghai）。状态：第 08 步完成，离线验收通过。

## 修改前状态与用户约定

实施目录：`D:\My_workspace\Multi-agent\Multi-agent`。修改前执行 `git status --short`，输出为空。

遵守上层 AGENTS.md：只运行 git status，没有运行其他 Git 操作；所有修改保留在工作区，由用户自行提交。使用 PowerShell，新建和修改文本按 UTF-8 / CRLF 保存，Python 格式化沿用 Ruff 的 cr-lf 配置。

## 注册与真实执行入口

新增 `agent/subagent_registry.py`，固定内部 ID 与中文显示名称：

| 内部 ID | 显示名称 | 业务工具 |
|---|---|---|
| db | 数据分析助手 | list_sql_tables、get_table_data、execute_sql_query |
| search | 市场情报助手 | internet_search |
| knowledge | 内部知识助手 | get_assistant_list、create_ask_delete |

每个不可变注册项包含角色提示、工具集合、模型配置、检查配置和允许的 Skill ID。当前模型配置使用已有默认模型且 SDK 重试为 0；检查配置 disabled，允许 Skill 集合为空，避免把第 09、11 步能力提前标为启用。重复 ID、非法 ID、未启用的配置拒绝。注册表的载入没有导入时构造客户端的副作用，真实工具和提示定义只在启动构建时读取。

旧 `agent/subagents/` 三份定义保留为提示词与工具来源，中文 name 不再用作框架路由键。主提示词明确 task 的 subagent_type 应使用 db / search / knowledge。

新增 `agent/subagent_factory.py`，用真实 LangChain create_agent 构建每个专家执行器，再包装为 CompiledSubAgent 注册给原有 create_deep_agent。每个子执行器明确配置本轮文件后端、Todo、摘要、工具调用修补、工具 Hook 与 sub_model 预算；没有假定父配置自动继承。模型提供非零 SDK 内部重试时构建拒绝，防止隐式请求漏计。

没有另写循环调度器。实际链路为：

```text
主模型 task 请求（模型 tool_call_id）
 → 主工具 Hook 校验目标、参数并预留一次 tool 预算
 → 框架按稳定 ID 路由至 CompiledSubAgent
 → DelegationRunnable 生成服务端子任务 call_id
 → 保存 queued，执行 before_delegate
 → 保存 running，执行专家图
 → 专家模型与工具分别经过已有预算 / Hook
 → 保存返回结果或失败 / 取消状态，执行 after_subagent
 → 框架原 task 返回 ToolMessage / Command 给主模型
```

before_delegate 的唯一实际执行入口和 after_subagent 的唯一结束入口均为 `agent/runtime/delegation.py` 的 DelegationRunnable。主工具中间件开启 managed_delegation 后只保留 task 工具本身的控制和记录，不再重复触发委派 Hook；执行器也不重复扣 tool 次数。专项和生产构建测试核对实际调用计数。

未知 ID、中文展示名称、默认 general-purpose 目标、空任务说明均在 task handler / 框架路由前拒绝。SDK 仍自动提供默认通用目标，但业务入口明确禁止其执行；三个专家自身也没有开放二次委派入口。

## 子任务存储、关联和前端

SQLite schema 从 2 升为 3，仅新增 subtasks 表与按 run 索引，保留已有 runs、events 和结果。版本 0、1、2 均支持升级，未知版本仍明确拒绝；不自动删除旧数据。旧代码不识别版本 3，回退前必须核对该迁移。

每条子任务保存：服务端 UUID call_id、run_id、thread_id、模型 request_call_id、稳定目标、中文显示名称、任务说明元信息、状态、截止时间、创建 / 结束时间、返回结果、错误与检查状态。

- 原始任务说明通过真实 HumanMessage 交给子 Agent；持久 description 字段按此前参数隐私规则保存为 `[REDACTED]`，另存长度和 SHA-256 供核对。没有把任务说明中的完整上传正文写进事件或任务记录。
- 截止时间继承 RunContext.deadline，没有配置时为 NULL；有截止时间且开始时已过期则拒绝。完整执行总超时仍属于第 13 步，不宣称本步提供运行中的截止时间强制终止。
- 返回结果作为业务输出保存，并对密钥等文本脱敏；结果的 check_status 明确为 not_checked。这里检查的是框架返回结构，不是第 09 步的内容质量检查。
- 同一个模型调用 ID 被重复使用时，每次执行仍产生不同服务端 UUID。子工具 parent_call_id 指向服务端子任务 ID，子任务记录通过 request_call_id 关联原 task 请求。
- 主流式解析返回事件经 monitor 适配到子任务 ID，同时保留模型请求 ID。前端收到中文名称、接收 / 执行 / 返回 / 失败 / 拒绝 / 取消事件。

子任务行和对应生命周期事件通过原有 EventSink 同一队列写入，在 RunStore 中使用同一事务提交，成功后才投递前端，不增加另一套事件出口。任务状态终态不可覆盖；身份不能被迁移到其他会话或轮次。父运行已经结束时，迟到的子任务结果不会被发布为成功。

记录提交失败会记入 hook_failures 或 EventSink 的持久化失败清单，最终沿用第 06、07 步“记录不完整”的显式处理，保留已经发生的业务结果。失败注入测试证明任务行与事件一起回滚。

`GET /api/runs/{run_id}/history` 在已有 run、events 外增加 subtasks，未执行任何子任务时为空列表。旧字段和接口保留。

## 实际检查

新增 `tests/test_delegation.py`，24 项离线测试覆盖同步 / 异步真实 task 路由、三个目标、执行前拒绝、异常与取消、重复模型调用 ID、两个会话与连续轮次、并行 task、原子存储、版本 2 升级、终态保护、HTTP 历史与中文名称、专家工具真实函数执行次数为零、禁用默认通用目标和隐式模型重试。

旧测试根据本步契约调整：生产构建测试的子工具父 ID 改为服务端 UUID；SQLite schema 断言改为 3；版本 1 测试夹具明确移除新增 subtasks 表以准确模拟旧版本。

项目根目录 PowerShell：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
& '.\.venv\Scripts\python.exe' -m pytest tests/test_delegation.py -q --tb=short
```

```text
24 passed in 43.25s
```

补充专项前的联合检查：

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_delegation.py tests/test_hook_pipeline.py tests/test_events.py -q --tb=short
```

```text
58 passed in 54.00s
```

```powershell
& '.\.venv\Scripts\python.exe' -m ruff check agent/subagent_registry.py agent/subagent_factory.py agent/runtime/delegation.py agent/main_agent.py api/run_store.py api/monitor.py agent/runtime/events.py agent/runtime/hooks.py tests/test_delegation.py tests/test_hook_pipeline.py tests/test_events.py tests/test_runs.py
& '.\.venv\Scripts\python.exe' -m ruff format --check agent/subagent_registry.py agent/subagent_factory.py agent/runtime/delegation.py agent/runtime/context.py agent/runtime/hooks.py agent/runtime/events.py agent/main_agent.py api/run_store.py api/monitor.py tests/test_delegation.py tests/test_hook_pipeline.py tests/test_events.py tests/test_runs.py
& '.\.venv\Scripts\python.exe' -m pip check
```

```text
All checks passed!
13 files already formatted
No broken requirements found.
```

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q --tb=short
```

```text
232 passed in 93.30s (0:01:33)
```

全套无失败、跳过项。最终以 Python 读取本步全部 17 个文本文件的字节，检查没有孤立 LF 或 CR，且 UTF-8 可解码；全部为 CRLF。未执行 git diff 等其他 Git 检查。

初次联合检查 35 passed / 2 failed：旧父 ID 断言与版本 1 测试夹具需要随新契约更新，均已修复后通过联合检查。新测试初次 Ruff 提示单行语句，格式化后检查通过，没有忽略失败或跳过测试。

真实模型、MySQL、Tavily、RAGFlow 联调和实际浏览器人工验收：未执行。本步使用假模型、假业务工具、真实框架图、临时 SQLite 与 ASGI HTTP 测试，没有读取真实凭据、启动服务或触发付费请求。UI 本步仅接入中文执行事件和标签；完整前端验收仍在第 14 步。
