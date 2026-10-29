# 历史响应与实时进度合并修复

日期：2026-10-29。项目根目录：D:/My_workspace/Multi-agent/Multi-agent。

本轮修复审计中的第三个问题：用户手动读取历史期间，实时 run_started 事件已把页面状态推进为 running，随后较旧的 queued HTTP 快照又覆盖前端状态。起始工作区干净；生产修改仅在 api/static/app.js 的 syncSession 内。未新增或修改测试文件。

## 实际读取并采用的 Codex 实现

通过 GitHub 连接器搜索并读取 openai/codex 固定提交 6c776cfc8c3b2aa4e429813c52c0e044ff01104b 的源码正文：

| 上游已读位置 | 实际处理方式 | 本项目采用方式 |
| --- | --- | --- |
| [thread_routing.rs 第 1789–1806 行](https://github.com/openai/codex/blob/6c776cfc8c3b2aa4e429813c52c0e044ff01104b/codex-rs/tui/src/app/thread_routing.rs#L1789) | 恢复输入状态时设置 preserve_in_flight_turn=true；先重放历史 turns，再重放缓冲事件 | 将历史作为状态基础，保留已经接收到的较新实时生命周期；用已有的单运行 event_seq 判断 HTTP 快照是否落后 |
| [input_restore.rs 第 655–761 行](https://github.com/openai/codex/blob/6c776cfc8c3b2aa4e429813c52c0e044ff01104b/codex-rs/tui/src/chatwidget/input_restore.rs#L655) | 显式保留在途的 agent_turn_running、user_turn_pending_start 和 task_running，随后更新运行状态界面 | 刷新记录不清除较新的 running 状态；较新的历史响应仍能推进 queued 状态 |
| [thread_events.rs 第 146–174、204–235 行](https://github.com/openai/codex/blob/6c776cfc8c3b2aa4e429813c52c0e044ff01104b/codex-rs/tui/src/app/thread_events.rs#L146) | 独立保存历史 turns 和缓冲通知；跟踪运行身份和当前在途 turn，完成通知须匹配对应 turn | 快照序号只计算 thread_id/run_id 匹配且 event_seq 为安全整数的事件，避免其他运行或非法序号污染当前状态 |
| [thread_events.rs 第 832–852、888–902 行](https://github.com/openai/codex/blob/6c776cfc8c3b2aa4e429813c52c0e044ff01104b/codex-rs/tui/src/app/thread_events.rs#L832) | 上游用例验证其他 turn 的完成不能清除当前在途 turn，以及从快照恢复在途状态 | 验证跨运行事件隔离、历史恢复、实时事件与快照交错等边界 |

采用的是上游的“保留在途状态、分层合并历史与增量”方式。上游载体为 Rust TUI 的 turns 和通知缓冲，本项目用现有 event_seq 实现同样的合并边界。上游源码片段及固定链接保存在 upstream-source.json。

## 实际修改

- 从当前运行历史的有效事件计算快照水位 historySeq。
- 将历史事件继续交给现有 applyEvent 去重、排序和处理。
- 活动快照只有在不落后于当前状态水位时，才可以更新页面状态；接受后同步推进 statusSeq。
- 已持久化终态继续提供完整正文、错误和 completion。现有终态保护、身份检查、事件去重和重连游标保持生效。
- 不增加后端字段、依赖、定时轮询或另一套状态来源。

RunStore.history 在同一存储锁内读取运行记录及事件，事件序号属于单个运行；终态通过既有条件更新固定。这些既有约束为前端水位比较与终态正文恢复提供依据。

## 复现和验证

证据目录：[output/verification/history-live-state-20261029/](../../output/verification/history-live-state-20261029/)。

修复前最小复现在生产 app.js 和现有 VM 浏览器装置上执行：页面已有排队运行并建立事件流，读取历史时注入 run_started，再返回旧 queued 响应。实际输出先为 running，后为 queued，而最新日志仍是 run_started；断言状态应为 running 失败。

使用真实认证 HTTP、临时 SQLite 和现有 GatedAgent 采集 queued、running、completed、cancelled、failed 五类历史，再交给生产 syncSession 重放。临时数据及采集记录见 server-histories.json/log。未调用真实模型或搜索服务，未修改生产数据库或 .env；实时事件的前端交错使用现有 VM 装置控制。

最终 20 个重放场景全部满足断言，其中 1 项确认修复前问题、19 项验证修复后行为，见 frontend-replay.json/log：

- 实时开始事件先于旧 HTTP、异步延迟响应、两个 HTTP 响应逆序。
- 较新 HTTP 从排队推进为运行，历史事件重复及乱序。
- completed、cancelled、timed_out、interrupted、failed 实时终态均不被活动快照覆盖。
- 从真实终态快照恢复完整正文、错误和 completion。
- 排除其他运行、其他对话和超出安全整数范围的事件序号。
- 移除会话后忽略旧请求、响应归属不匹配、完成快照后的迟到运行事件。

首次扩展重放脚本错误地把运行事件数量固定为 2；真实历史还包含调度事件，实际为 3。仅修正临时重放中的固定序号和数量假设，改用实际历史事件序号和集合；生产代码、现有测试及真实接口数据未因此改变。首次脚本失败记录保留在 frontend-replay-first.log。

现有 3 个前端用例全部通过，见 frontend.log，覆盖页面恢复、事件归属、工具展示、取消、下载、事件顺序、去重、重连和游标。node --check api/static/app.js 通过。

源码差异核对确认仅 syncSession 内容改变，其余源码字节不变；UTF-8、原 BOM、CRLF 和行尾空白检查通过。差异与检查记录见 production.patch、source-audit.json、validation-summary.json。按项目限制仅执行 git status，未执行其他 Git 操作。
