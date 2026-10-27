# 第 04 步：每轮执行记录与同会话互斥

日期：2026-10-14（Asia/Shanghai）。状态：第 04 步完成。

## 范围与修改前状态

实施目录：`D:\My_workspace\Multi-agent\Multi-agent`。
修改前执行 `git status --short`，输出为空。
遵照用户最新要求：本步只使用 `git status` 查看状态；未执行 git add、commit、push 或其他 Git 操作。修改保留在工作区，交由用户处理提交。

## 实现行为

### 会话与每轮执行

- thread_id 继续标识会话并用于 LangGraph checkpoint；run_id 由服务器生成 UUID，标识一轮执行，不接受客户端指定。
- 新增 `agent/runtime/context.py`：RunContext 携带 run_id、thread_id、work_dir、deadline 与每轮独立的 call_counts；ContextVar 绑定和复原本轮上下文，asyncio.to_thread 的同步工具可读取相同上下文。
- deadline 当前为 None，计数字段独立初始化为 0；实际计数和限制在第 07 步接入，总超时在第 13 步接入。本步不宣称已经限次或超时。
- `api/context.py` 保留旧工具接口，优先从 RunContext 返回本轮目录和会话，兼容未绑定运行上下文的旧调用。

### 运行记录与终态

- 新增 `api/run_store.py`，服务启动时打开 `data/runs.db`，关闭时释放连接；导入时不打开数据库或创建目录。与 checkpoints.db 分离。
- schema version 1 使用 PRAGMA user_version 标记，事务创建 runs 表与唯一索引；不支持的未来版本明确拒绝，保留已有数据。
- 每条记录保存 run_id、thread_id、question、status、answer、error、created_at、ended_at。时间以带 UTC 偏移的 ISO 格式保存。
- queued/running 为活动状态，completed/failed/cancelled/interrupted 为终态。SQL 更新使用 run_id 并要求仍处于活动状态；终态无法被后续完成、错误或取消覆盖。
- 服务器先持久化 queued 记录，再注册后台执行；执行开始转为 running，成功保存答案，异常保存错误，关闭取消保存 cancelled。协程尚未开始等关闭残留记录转为 interrupted。
- 后台任务按 run_id 保存引用和清理；旧轮次完成回调不会删除新轮次任务。异常继续由第 03 步机制读取、记录。
- 成功答案保存完成后才推送最终结果；消息推送不替代持久化记录。统一事件持久化仍属于第 05 步。

### 请求互斥与接口

- `POST /api/task` 返回 `{status: "started", thread_id, run_id}`。
- 部分唯一索引保证每个 thread_id 最多一条活动记录，异步锁保护同连接事务；API 提交锁覆盖占位和任务注册。同会话重复请求返回 HTTP 409：`{status: "busy", thread_id, run_id}`，run_id 是已有活动轮次。
- 不同会话只在短暂的提交/数据库写入阶段串行，Agent 执行可并发。完成或失败后同会话可以创建新轮次。
- 请求在记录写入后被取消时，按事先生成的 run_id 清理占位，不清理其他轮次，不留下持续忙碌的会话。
- 新增 `GET /api/runs/{run_id}`，返回对应持久化记录；不存在返回 404。不要求连接 WebSocket 才能读取答案与错误。
- 请求 thread_id 限定为 1–128 位字母、数字、下划线或连字符，避免该标识被拼成越界的输出路径。

### 每轮文件目录

- 主运行器使用 `output/session_<thread_id>/run_<run_id>/`，模型提示词与 session_created 通知均使用本轮目录。
- 当前会话上传文件在本轮开始时复制到该目录；下一轮不会覆盖上一轮副本。
- `utils/path_utils.py` 在 RunContext 存在时使用本轮目录：支持裸文件名、子目录、当前轮的项目相对前缀及已有虚拟路径别名，保留子目录结构；越出本轮目录的解析结果拒绝。
- Markdown、文件读取和 PDF 工具已共用 get_session_context/resolve_path，本步通过共同适配入口接入每轮目录，不重复改写每个工具。顺带去掉原文件顶部无效的重复函数定义。
- 无运行上下文时保留旧路径行为。第 07 步仍需统一工具/文件路由权限校验，不能将本步目录适配宣称为完整权限系统。

## 验证内容

新增 `tests/test_runs.py` 与 `tests/test_run_concurrency.py`，共 19 项：

- 记录字段、异步保存与关闭后重开读取；schema version 1；未来版本拒绝且数据保留。
- 四种终态均拒绝晚到更新，旧轮不能改写第二轮；无效状态和不存在的 run_id。
- 存储层 10 个同会话并发占位只有一个成功。
- 真实 ASGI HTTP 接口 8 个同会话并发请求只有一个成功，其余 409 返回同一个活动 run_id，实际只开始一次执行。
- 两会话同时处于 running，各自上下文、答案、通知与同名 report.md 分离；执行和任务引用各自清理。
- 同会话第二轮使用新 run_id；第一轮记录、报告、上传副本不变；旧任务回调不删除新任务。
- 异常持久化错误，释放会话以便重试；服务端生成 ID；非法 thread_id、不存在的运行记录。
- 取消执行、关闭前尚未开始的记录、关闭后连接释放，以及取消提交后占位释放。
- ContextVar 传到同步工具线程，计数独立，退出恢复；实际 Markdown 写入与文本读取经过本轮路径解析。

更新第 03 步生命周期测试的临时数据库和通知注入，适配新增 run_id 响应；增加实际 lifespan 关闭后重开读取 cancelled 运行记录的断言，保留原有初始化、流式及 checkpoint 验证。

测试使用可控 Agent、假模型、临时 SQLite 与目录，禁止外部网络。并发测试通过实际 FastAPI/ASGI 路由和主运行器执行，并调用真实 Markdown/文本读取工具；不是只测试内存字典。

## 实际检查命令与输出

从项目根目录执行：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
.venv/Scripts/python.exe -m pytest tests/test_runs.py tests/test_run_concurrency.py -q --tb=short
```

```text
19 passed in 9.74s
```

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
.venv/Scripts/python.exe -m pytest -q --tb=short
```

```text
49 passed in 20.93s
```

```powershell
.venv/Scripts/python.exe -m pip check
.venv/Scripts/python.exe -m ruff check agent/main_agent.py agent/runtime api/server.py api/run_store.py api/context.py utils/path_utils.py tests
.venv/Scripts/python.exe -m ruff format --check agent/main_agent.py agent/runtime api/server.py api/run_store.py api/context.py utils/path_utils.py tests
```

```text
No broken requirements found.
All checks passed!
13 files already formatted
```

## 未执行与后续范围

- 真实模型、MySQL、Tavily、RAGFlow 联调：**未执行**。
- Word/PDF 实际转换：**未执行**；本步实际文件验证为 Markdown 与文本读取，共同路径解析已覆盖。
- 对外监听服务和浏览器联调：**未执行**，测试只使用进程内 ASGI，不占用监听端口。
- Git 暂存、提交、推送：**未执行**，按用户要求保留未提交修改。
- 异常崩溃重启的未完成记录处理仍安排在第 13 步；本步验证正常生命周期退出的清理，不自动恢复任务。
- 统一事件、Hook、调用限制和前端完整状态展示仍属于后续步骤。本步没有修改前端，也不宣称已有事件补发或用户取消接口。
