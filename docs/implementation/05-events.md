# 第 05 步：统一事件保存与进度推送

日期：2026-10-14（Asia/Shanghai）。状态：第 05 步完成。

## 修改前状态与范围

实施目录：`D:\My_workspace\Multi-agent\Multi-agent`。
修改前执行 `git status --short`，输出为空。
本步只使用 git status 查看状态，遵照用户要求不执行 add、commit、push 等操作；修改保留在工作区。

## 统一事件出口

新增 `agent/runtime/events.py`：EventSink 是服务调用链的统一保存出口。RunContext 携带本轮 event_sink；异步 Agent 和 asyncio.to_thread 中的同步工具读取同一上下文。

事件包含：

| 字段 | 含义 |
|---|---|
| thread_id / run_id | 会话及轮次 |
| event_seq | 轮内唯一、从 1 开始递增的序号 |
| event | 事件类型 |
| call_id / parent_call_id | 调用标识及父调用标识 |
| actor | 执行对象 |
| status | 请求、开始、返回、失败或运行终态等状态 |
| timestamp | 带时区的 UTC ISO 时间 |
| message / data | 展示说明与经过脱敏的数据 |

普通监控调用先构造脱敏事件，再进入服务循环的异步保存队列；同步工具线程通过线程安全投递接入。工作协程先提交 SQLite 事务，成功后才安排定向 WebSocket 推送。提交和关闭之间用短锁保护，退出时排空队列并等待事件工作协程结束，再清理推送任务、关闭数据库。

- 无连接时仍保存事件；断开连接不删除任何记录。
- WebSocket 实际发送异常由现有完成回调读取并记录；安排推送本身抛异常也只记录显示失败，不更改业务状态。
- 事件保存失败明确记录 `Event was NOT saved`；flush 抛 EventPersistenceError。错误按 run_id 分开，不能被另一个会话的完成路径误消费。
- 普通监控事件缺失时，最终业务答案仍可保存；记录缺失会显式暴露，不宣称所有事件已保存。最终状态与结束事件写入失败时，两者一并回滚。

## SQLite 迁移与一致性

`api/run_store.py` 从 schema version 1 迁到 2，在原 runs 表旁创建 events 表，保留已有问题、答案和终态。

- events 主键为 `(run_id, event_seq)`；序号读取、分配和插入在同一写事务内完成。
- 保存时核对 run_id 对应的 thread_id，拒绝事件归属到其他会话。
- 接收任务、开始执行及结束执行，通过 EventSink 调用存储层的事务接口，同时保存运行状态与对应事件。
- 最终答案或失败原因和结束事件在同一事务中保存；旧终态收到晚到更新时既不覆盖状态，也不追加伪造的结束事件。
- `GET /api/runs/{run_id}/history` 返回 `{run, events}`。运行记录与有序事件在同一把锁内读取，避免运行状态和事件来自两个不一致的读取时刻；不存在返回 404。

该接口提供完整已保存历史快照。after_seq 增量查询、WebSocket 重连补发和前端去重仍留在第 13、14 步，本步没有提前实现。

## 旧监控与流式适配

`api/monitor.py` 保留 report_tool、report_assistant、report_task_result、report_session_dir 和 _emit，服务上下文存在时转到 EventSink；独立脚本没有持久化出口时保留控制台/runtime 兼容输出，不宣称这些独立调用已经落库。

- assistant_call 是模型的助手请求，status=requested；不当作子 Agent 已开始执行。
- tool_requested 是模型工具请求，保留框架 tool_call_id。
- tool_start 由现有工具业务函数内部埋点产生，status=started。模型提出请求与真实函数开始执行是两个不同阶段，不代表函数执行两次。
- tool_returned 来自主图 ToolMessage，使用原 tool_call_id，记录返回阶段及结果长度，不保存原始工具正文。框架明确标为 error 时记录 failed；不凭内容非空声称业务检查通过。
- 主运行器处理单条 ToolMessage、AIMessage、消息列表和 Overwrite，按阶段及调用 ID 去重；Overwrite 中的旧轮历史不重复记作本轮请求。
- 服务终态由统一出口保存并推送，移除主运行器在服务模式下的重复错误上报；最终失败原因只记录一次。

当前工具业务开始埋点使用独立服务端调用标识，父调用先指向本轮根调用；模型请求与主图返回可以用框架 ID 关联。更细的子 Agent 执行边界、内部工具请求和调用树关联需第 06、08 步的实际包装入口。本步没有凭模型请求伪造子 Agent 执行记录。

## 脱敏

参数中的字符串统一隐藏，保留有限的字段结构和数值；密钥、令牌、密码、授权头、文档正文、描述、查询文本等敏感字段递归替换。工具返回只保存状态与长度。

事件说明、保存的问题、最终答案和错误中的常见凭据形式也做替换，包括 key/password/token 赋值、Bearer、sk- 密钥和 URL 内嵌账号密码。实际执行仍接收原始问题，脱敏后的记录不改变模型输入。正常最终答案完整保留，不按普通参数的长度上限截断。

这不是任意格式文本的全面秘密识别器；参数字符串和工具正文的省略是主要保护措施。未向事件保存完整上传文档来充当工具结果。

## 验证

新增 `tests/test_events.py` 与 `tests/test_run_history.py`，共 18 项，使用临时数据库/目录及离线模型。

- 用第二条 SQLite 连接在通知回调中查询，证明每次通知发生时事件已提交，最终答案也已提交。
- 20 个同步工具线程上报：序号唯一递增，调用标识、父标识与会话/轮次归属正确。
- 参数、上传正文、授权头和凭据不出现在事件或推送载荷中。
- 两轮与两会话分离；关闭重开仍能读取；版本 1 迁移到 2 不丢已有运行记录。
- 事件插入失败回滚最终状态；后台保存失败明确暴露；一个会话的保存错误不影响另一个会话；最终业务答案不因显示或普通事件记录失败被覆盖。
- 通知安排抛异常以及真实 ConnectionManager 异步 send_json 抛异常：已保存的成功/失败保持原状态，断开后历史保持不变。
- 没有 WebSocket 时通过真实 ASGI HTTP 路由读取目录、工具开始、最终答案及失败历史。
- 模拟重命名节点、重复更新和旧轮 Overwrite，验证调用事件去重与阶段含义。
- 使用实际 create_deep_agent、ScriptedModel 与 @tool 执行一次工具：请求、业务开始、返回各一条，业务函数调用次数为 1，请求和返回共享框架调用 ID，最终答案落库。
- 正常最终答案超过普通参数长度上限时完整保存；晚到终态不覆盖也不追加结束事件。

更新之前的测试以适配 schema version 2 和统一终态出口；共用服务测试环境改为显式上下文管理器，避免测试夹具作用域/恢复顺序污染后续用例。

实施中曾发现 API 接入变量错误和共享测试环境注册/恢复顺序问题；均已修复。最终没有失败、跳过或通过绕开链路取得的测试结果。

## 最终检查命令与输出

项目根目录，当前锁定虚拟环境：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
.venv/Scripts/python.exe -m pytest tests/test_events.py tests/test_run_history.py -q --tb=line
```

```text
18 passed in 19.30s
```

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
.venv/Scripts/python.exe -m pytest -q --tb=line
```

```text
67 passed in 41.50s
```

```powershell
.venv/Scripts/python.exe -m pip check
.venv/Scripts/python.exe -m ruff check agent/main_agent.py agent/runtime api/monitor.py api/run_store.py api/server.py tests
.venv/Scripts/python.exe -m ruff format --check agent/main_agent.py agent/runtime api/monitor.py api/run_store.py api/server.py tests
```

```text
No broken requirements found.
All checks passed!
15 files already formatted
```

## 未执行与交付

真实模型、MySQL、Tavily、RAGFlow 联调：**未执行**。浏览器和对外监听服务联调：**未执行**；HTTP 验证为进程内 ASGI，未启动监听端口。测试使用临时数据库，未迁移用户真实 runs.db 或 checkpoints.db。

未新增依赖，未修改前端。后台任务生命周期、同会话互斥与文件轮次隔离通过全套回归。Git 暂存、提交、推送：**未执行**，修改留在工作区。
