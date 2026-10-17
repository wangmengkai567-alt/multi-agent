# 第 13 步：总超时、取消与事件补发

日期：2026-10-16（Asia/Shanghai）。状态：第 13 步完成，离线验收通过。

## 工作区与约定

实施目录：D:\My_workspace\Multi-agent\Multi-agent。修改前内层仓库 git status --short 只有总计划的文档修改（上轮已补齐第 01、02 步状态），本次保留该修改。上层目录还有用户暂存的 AGENTS.md 与嵌套仓库状态，未改动其暂存内容。遵守上层 AGENTS.md，只执行 git status，未执行暂存、提交、推送及其他 Git 命令。项目 .editorconfig 与 AGENTS.md 要求 UTF-8 / CRLF；已有文件保留 BOM，新文件无 BOM。没有适用于本次文件的下层 AGENTS.md 或 .gitattributes。Git 配置查询、git ls-files 与 git diff --check 因项目限制未执行，以全部交付文件的实际字节、BOM、行尾空白和哈希审计替代。

## 总截止时间与请求超时

config/runtime.yml 新增 timeouts，默认 total_seconds: 300.0、request_seconds: 30.0、shutdown_seconds: 5.0。总时长必须为正且不超过 86400 秒，单请求不超过 300 秒，退出等待不超过 30 秒。无效配置在服务启动前拒绝；旧的合法配置可使用默认值。

每次 POST /api/task 创建独立 RunContext，保存 UTC 截止时间与单调时钟截止点。后台监督任务等待整个执行体，覆盖主模型、子模型、结果检查、补充查询、工具重试及重试退避，不按阶段重新计算总时长。执行体使用独立 asyncio Task；总时长耗尽后保存 timed_out 终态，并取消执行体。即使执行体吞掉取消，也无法再次预留调用、返回最终答案或覆盖终态。

所有受控模型、工具和子任务执行前检查停止标记及截止点；返回后再次核对。同步工具线程共享 RunContext 的停止标记。调用预算仍按原规则记录，不因超时或取消获得额外次数。

OpenAI 兼容模型构造时配置单请求超时，受预算包装的模型调用还将当前剩余时间传给 SDK；异步模型外层另设请求超时。Tavily、RAGFlow、MySQL 请求有各自超时，并取配置上限与本轮剩余时间的较小值。Tavily / RAGFlow 仍受既有 SQL 工具超时上限约束。MySQL 驱动只接受整数秒，连接、读写超时至少为 1 秒；SQL 执行还保留 MAX_EXECUTION_TIME。总监督任务仍负责及时结束本轮。

Python 无法强制终止已经运行的同步线程或远端请求；这类动作在自身请求超时后返回，结果通过停止检查和持久层终态条件拒绝发布。没有承诺远端物理请求在取消瞬间停止。

## 取消接口与终态

POST /api/runs/{run_id}/cancel 按 run_id 定位执行任务及上下文，先设置停止标记并保存终态事件，再取消监督任务。响应为当前完整运行记录，包含 status、answer、error、ended_at。不存在的 run_id 返回 404；重复取消和对已结束运行取消均返回原记录，不改写答案或终态。取消与正常完成竞争时，由持久层的首次终态转换决定结果。

终态为 completed、failed、cancelled、timed_out、interrupted。终态转换和最终事件在同一事务中完成；只有 queued / running 可以转换。运行结束时，同事务将仍活跃的子任务标为 cancelled（父任务取消）或 interrupted（其他父任务终态），避免遗留 running。既有已完成子任务与合格结果保留；晚到结果不能变成新的可用摘要。

runs 数据库迁移到 schema v5，仅扩展运行状态约束。迁移事务保留 runs、events、subtasks、results 及原有索引关系，不删除历史业务数据。新库和 v1–v4 库均按版本迁移；未知版本仍拒绝打开。本次只迁移临时测试库，真实 data/runs.db 未打开或改写，启动服务时才执行迁移。测试覆盖历史答案、事件、已检查结果及外键关系。

## 事件读取与重连

GET /api/runs/{run_id}/events?after_seq=0&limit=200 返回 run_id、thread_id、events、next_seq、has_more。只返回 event_seq 严格大于 after_seq 的事件，按序升序排列。after_seq 允许 0–9007199254740991，limit 允许 1–1000；空页保留请求游标。不存在运行返回 404，无效游标或分页参数返回 422。持续使用 next_seq 读取后续页即可补齐持久事件；完整答案仍可通过运行详情及 history 读取。

WebSocket 路径：/ws/{thread_id}?run_id={run_id}&after_seq={last_seq}。握手前检查运行归属，未知运行、跨会话和非法会话 ID 以 1008 拒绝；非零游标必须同时指定 run_id，因为序号只在单轮内唯一。

连接先注册并持有该连接的发送锁，再补读数据库；实时通知使用同一锁。通知只触发按游标追读数据库，而不直接发送通知载荷，因此补发与实时消息竞争、通知重复或乱序均不会造成缺失或重复。每批最多读取 1000 条，直到追平；慢连接单次发送超时为 5 秒，不影响业务终态。旧连接的延迟断开不能删除新连接。沿用每个 thread_id 一个活动连接的首版约定；新连接替换旧连接。

未传 run_id 的旧客户端仍可按会话补读各轮历史，但非零 after_seq 不适用于这一兼容模式。新前端明确绑定 run_id。

新增 api/static/run-events.js。前端携带 run_id 与最后连续收到的序号重连，按 run_id:event_seq 去重；乱序事件不会越过缺口推进游标。断线只改变连接状态，不再被标为任务完成；主动关闭页签会取消重连定时器。重连退避从 500 毫秒增长，上限 10 秒。完整的刷新会话恢复、取消按钮与各终态的界面细分属于第 14 步；本步已接入实际事件流重连和去重。

## 服务退出与重新启动

服务退出先停止接收新任务，将本进程未完成运行标为 interrupted，取消并等待监督任务，取消执行体并按 shutdown_seconds 等待其结束，然后清理事件持久化、WebSocket 推送、数据库和 Agent checkpoint 资源。取消之前尚未获得执行机会的 queued 记录也会被清理。

重新启动时读取旧数据库，将遗留 queued / running 标为 interrupted 并保存 run_interrupted 事件；既有终态和答案保持原样。用户可以对同一 thread_id 提交新轮次。首版不自动恢复旧执行体、不重发旧模型请求。

## 验证与范围

专项命令：.venv/Scripts/python.exe -m pytest tests/test_run_cancellation.py tests/test_event_replay.py -q --tb=short -p no:cacheprovider --basetemp=output/pytest-step13-focused

全套命令：.venv/Scripts/python.exe -m pytest -q --tb=short -p no:cacheprovider --basetemp=output/pytest-step13-full-final

前端脚本：node tests/frontend/run-events.test.cjs。生产 RunEventStream 在隔离的 Node VM 中运行，使用可控 WebSocket 和定时器，覆盖断线重连游标、重复与跨轮过滤、序号缺口、终态事件及主动关闭。ASGI WebSocket 测试使用 FastAPI TestClient 检查真实路由、补发、心跳及跨会话拒绝；其他测试使用真实 SQLite、HTTP 路由、Hook、模型包装器和结果检查器。

已完成相关回归：68 passed in 113.28s。首轮专项曾有 1 failed、17 passed：150 毫秒测试窗口在 Windows 磁盘启动阶段就已耗尽，未进入待测重试阶段；改为 1.5 秒窗口，未改变生产总超时。首轮全套记录为 4 failed、356 passed：一项旧退出测试仍预期 cancelled，三项 Hook 边界测试暴露缺省截止时间的处理缺失。现已更新退出预期，并在执行入口补齐默认截止时间、使用所属任务集合进行清理；正在重新验证。修复后针对性验证为 29 passed in 43.25s；计划专项为 24 passed in 48.35s。

| 检查 | 实际输出与记录 |
|---|---|
| 全套离线回归 | 360 passed，见 [13-tests.log](13-tests.log) |
| 取消与重连专项 | 24 passed，见 [13-cancellation-replay.log](13-cancellation-replay.log) |
| 修复与历史迁移验证 | 29 passed，见 [13-repaired.log](13-repaired.log) |
| 前端客户端 | PASS，见 [13-frontend.log](13-frontend.log) |
| Ruff 检查 | All checks passed，见 [13-lint.log](13-lint.log) |
| Ruff 格式 | 21 files already formatted，见 [13-format.log](13-format.log) |
| 依赖 | No broken requirements found，见 [13-dependencies.log](13-dependencies.log) |
| 环境与版本 | Python 3.11.15，依赖版本见 [13-environment.log](13-environment.log) |

最终全套回归：360 passed in 427.37s（0:07:07），见 [13-tests.log](13-tests.log)。25 个受测源码、配置及前端文件在最终测试期间的 SHA-256 均未改变；全部本次交付文本（含源码、测试、配置、页面、文档与检查日志）的字节及哈希审计见 [13-text-audit.log](13-text-audit.log)。日志只统一为 UTF-8 / CRLF 并移除行尾空白；命令、退出码、错误内容和统计结果保留。首轮失败及诊断输出保留在 13-initial-focused.log、13-initial-tests.log、13-diagnosis.log；未将其冒充通过记录。

测试沿用离线隔离：收集前移除真实凭据，阻止外部网络，使用临时数据库和目录；没有调用付费模型、MySQL、RAGFlow、Tavily 或外部服务。没有启动监听端口。真实模型与服务联调仍留给第 14 步，不将离线通过等同于真实服务验收。
