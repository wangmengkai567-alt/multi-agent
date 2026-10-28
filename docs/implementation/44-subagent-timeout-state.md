# 子助手超时终态持久化修复

日期：2026-10-28。项目根目录：D:/My_workspace/Multi-agent/Multi-agent。

本轮只修复已复现的第一个问题：子助手总时限结束后，将 timed_out 写入不支持该状态的 subtasks 表，导致 SQLite CHECK 错误和 EventPersistenceError，覆盖原始超时信号。工作区起始干净；用户本轮明确授权新增或调整相关回归测试。未修复另两项已报告问题。

## 实际读取的 Codex 方案

本轮实际通过 GitHub 连接器读取了以下源码，而非仅引用搜索摘要：

| 已读取来源 | 实际行为 | 采用方式 |
| --- | --- | --- |
| [core/src/agent/status.rs](https://github.com/openai/codex/blob/26ad281fd1ca712e4f0c7920a6273cf18c961b8a/codex-rs/core/src/agent/status.rs) | TurnComplete 携带错误或 Error 事件时使用 Errored(message)；中断与预算终止使用 Interrupted；其他终止原因保留在错误中 | 子助手实际因总时限终止时，使用已有 failed 执行终态，并保留原始 error 与 error_category=timeout |
| [exec/src/exec_events.rs](https://github.com/openai/codex/blob/26ad281fd1ca712e4f0c7920a6273cf18c961b8a/codex-rs/exec/src/exec_events.rs) | CollabAgentStatus 使用既有执行状态，CollabAgentState 另存 message；没有为等待超时增加同名执行终态 | 使用状态与原因分离，不为本项目 subtasks 新增 timed_out 枚举或引入数据库迁移 |
| [multi_agents_v2/wait.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/tools/handlers/multi_agents_v2/wait.rs) | WaitOutcome 区分消息、用户输入和等待超时；WaitAgentResult.timed_out 表示等待结果 | 仅在本项目实际总时限已终止执行时处理失败；不能把一次等待超时推断成子助手已经失败或完成 |

同时读取了固定提交 970111f6cb62cc02fe36a82ea7d71b37a2889c97 的 status.rs，与本轮检索到版本的状态投影一致。

Codex 没有本项目 Python/SQLite 的现成补丁；本轮直接采用其已有执行终态与具体错误原因分离的方式，在现有 Python 委派器中实现同一机制。没有把 Rust 源码、Codex SDK 或旧协议兼容分支接入本项目。

## 复现与修改

修改前先新增同步与异步回归：在真实 DelegationRunnable 已开始执行后，将该运行的单调时钟截止时间设为过去，再调用实际 context.check_active。该方式确定性地走真实总时限终止路径，不依赖睡眠竞态或伪造 SQLite 异常。

两项均在修复前失败，生产 EventSink.flush 抛 EventPersistenceError，底层为 subtasks.status CHECK 拒绝 timed_out；见 output/verification/subagent-timeout-20261028/before.xml 和 before.log。根因已经在上一轮隔离排查确认，本轮不重复泛化假设和额外日志插桩。

生产修改仅在 agent/runtime/delegation.py：

- _failed 将总时限导致的子助手终态归为 failed，error_category 仍按 context.stop_status 保留 timeout，原始 RunStopped 和超时错误文本继续向外传播；不吞异常或标记成功。
- _save 为分工终态事件保存 error 和 error_category，使认证历史回放可直接读取实际失败原因；继续复用既有原子“子任务行 + 事件”事务和单一 EventSink 出口。
- 整轮运行保持 timed_out。单次工具/模型超时、取消、服务中断及正常成功继续各自原有处理。
- 父运行先结束时，继续保留数据库已落下的中断或取消终态；迟到返回不能覆盖它。不把已有父终态重新解释为一次新完成。

未修改数据库版本、历史表约束、迁移器或生产数据库。不增加新的并行执行/兼容路径，不修改其他两个已报告问题。

## 验证证据

证据目录：[output/verification/subagent-timeout-20261028/](../../output/verification/subagent-timeout-20261028/)。before/ 保存修改前源码；首轮失败记录保留。

- 同一对同步/异步回归修复后 2 项通过（after.xml/log）。
- 增加认证 HTTP 链路后，3 项针对性验证通过（targeted.xml/log）。使用真实临时数据库迁移、登录、运行调度、委派器、事件存储及下载入口；执行器使用可控截止时间，不调用真实模型或外部服务。
- 验证子任务数据库行、内存及分工 JSON 均为 failed，原始超时原因保留；唯一 subagent_failed 事件分类为 timeout；整轮和完成快照为 timed_out，未出现 recording_status=incomplete。
- 认证下载 answer.md 返回 404；所有调度槽归零；终态后提交成功结果或取消不能覆盖已保存的历史。数据库关闭并重开后，完整历史与关闭前一致。

最终相关回归 **56 项全部通过**（regression.xml/log，281.28 秒），覆盖取消与总时限、三助手协作、失败资源释放、认证历史、事件持久化和回放；无 skip/xfail。3 个既有前端用例全部通过（frontend.log）。3 项针对性验证已包含在这 56 项内，不重复相加。最终验证期间和验证后未再修改生产或测试源码。

Ruff、格式、UTF-8、原有 BOM、CRLF 与变更行空白结果见证据目录中的日志及 source-audit.json。只执行 git status；按项目更严格限制不执行 git diff --check，使用原始字节快照与变更行审计。

本轮不执行真实业务题，不调用真实模型、搜索或业务数据库，不改写历史运行记录，不执行 Git 暂存、提交或其他版本管理操作。
