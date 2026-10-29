# 取消与最终文件交付一致性修复

日期：2026-10-29。项目根目录：D:/My_workspace/Multi-agent/Multi-agent。

本轮修复审计中的第二个问题：答案已落盘、成功终态尚未保存时取消任务，运行记录为 cancelled、answer 为 null，但文件列表及下载仍公开 answer.md，completion 还显示 delivery_status=completed。起始工作区干净；本轮修改 agent/runtime/events.py、agent/runtime/completion.py、api/server.py。未新增或修改测试文件。

## 实际读取并采用的 Codex 实现

通过 GitHub 连接器搜索并读取 openai/codex 固定提交 e75e4af22938c8715b2a1e0d3fc142836ab4d962 的源码正文：

| 已读上游位置 | 实际机制 | 本项目采用方式 |
| --- | --- | --- |
| [tasks/mod.rs 第 385–419 行](https://github.com/openai/codex/blob/e75e4af22938c8715b2a1e0d3fc142836ab4d962/codex-rs/core/src/tasks/mod.rs#L385) | 任务返回后先完成记录屏障，再检查 cancellation_token；已经取消就不进入正常完成发布 | EventSink.finish 在 flush 返回后重查 stop_status；取消、超时、中断及失败已经生效时，按该停止终态保存，清空成功正文 |
| [on_task_finished 第 686–699 行](https://github.com/openai/codex/blob/e75e4af22938c8715b2a1e0d3fc142836ab4d962/codex-rs/core/src/tasks/mod.rs#L686) 与 [abort_turn_if_active 第 603–632 行](https://github.com/openai/codex/blob/e75e4af22938c8715b2a1e0d3fc142836ab4d962/codex-rs/core/src/tasks/mod.rs#L603) | 完成和取消在 active_turn 锁内取得处理资格，取走任务；失去资格的完成处理直接返回 | 沿用 RunStore.finish 的事务及仅允许 queued/running 更新的条件，以唯一持久化终态决定最终文件的公开资格；不增加第二套竞争锁或完成标记 |
| [tasks/mod.rs 第 858–919 行](https://github.com/openai/codex/blob/e75e4af22938c8715b2a1e0d3fc142836ab4d962/codex-rs/core/src/tasks/mod.rs#L858) | 对应终态选择 TurnAborted 或 TurnComplete，再向客户端发送 | 非成功终态的 completion 不携带成功 artifact；有准备结果时交付状态为 not_delivered |
| [mod_tests.rs 第 505–573 行](https://github.com/openai/codex/blob/e75e4af22938c8715b2a1e0d3fc142836ab4d962/codex-rs/core/src/tasks/mod_tests.rs#L505) | UncooperativeTimedTask 与 hard_abort_drops_timing_guards_before_profile_completion 验证取消后的收尾边界 | 验证停止信号发生在记录屏障期间时，迟到的完成处理不能发布成功正文或最终文件 |

对应的现成方案是任务完成发布资格控制，上游的输出载体为任务事件。本项目采用相同生命周期方式，结合现有 SQLite 终态和认证文件接口实施。上游原文已读片段及测试正文保存在 upstream-source.json。

## 实际行为

- answer.md 是否可公开，由已保存的 completed 终态决定。答案准备期间，以及 cancelled、timed_out、interrupted、failed 运行的最终答案，不出现在文件列表中，直接下载返回 404。
- Windows 大小写别名经过同一规则；.answer.tmp 始终不公开。
- 非成功终态不发布成功 artifact；已经准备的交付信息表示为 not_delivered。调查来源覆盖情况 coverage 与文件交付状态独立保留。
- 磁盘上的已准备正文可保留为运行内部数据；公开接口以持久化终态为依据，不将文件存在等同于交付完成。
- 正常完成先取得终态时，后续取消仍返回 completed，已有正文、文件、摘要和唯一成功事件保持一致。历史资源的认证、所有权和路径校验继续生效。

## 复现和验证

证据目录：[output/verification/cancelled-delivery-20261029/](../../output/verification/cancelled-delivery-20261029/)。

修复前最小复现使用真实认证 HTTP、临时 SQLite、现有 GatedAgent，在生产代码本来就会等待的 EventSink.flush 处固定交错顺序。任务 running 时已有 answer.md，随后取消；结果是 cancelled、answer=null、delivery_status=completed，下载返回 200，文件列表仍有 answer.md。验证“取消后应返回 404”的断言失败。

保存修复前源码后，通过真实路由重放再次确认原始问题。修复后 8 个场景的全部断言通过，见 http-replay.json/log：

1. 修复前源码的取消窗口重放，确认能复现原问题。
2. 文件准备期间取消：运行和完成信息均为 cancelled，artifact=null，delivery_status=not_delivered；直接下载和大小写别名均 404，文件列表无最终答案；重启后保持一致。
3. 正常完成先保存：只产生一个 task_result，下载字节等于数据库正文；取消不改写完成记录；重启后仍能下载。
4. 至 7. flush 等待期间分别设置 cancelled、timed_out、interrupted、failed，随后只释放正常完成处理：重查停止状态后保存相应终态，无成功事件、无成功正文、无公开最终文件。
8. 模拟成功终态保存失败：准备文件仍不可下载，无 task_result；关闭和重启后记录为 interrupted，最终文件仍不公开。

重放使用临时存储和受控模型响应，禁止外部网络；未调用真实模型或搜索服务，未改写生产数据库或 .env。未修改第一项 URL 修复源码和前端进度逻辑。

## 相关回归与交付检查

现有 60 项相关回归全部通过，248.06 秒，无 skip/xfail，记录见 regression.log/xml。覆盖运行取消与超时、持久化历史、并发运行、事件保存与通知、运行正文/文件一致性，以及文件下载和路径隔离。未新增或修改测试文件。

实际执行的既有测试范围：tests/test_run_cancellation.py、tests/test_run_history.py、tests/test_run_concurrency.py、tests/test_events.py、tests/test_runs.py，以及 test_tool_policies.py 中的 test_upload_validation_size_and_valid_output_delivery、test_static_download_listing_and_upload_junction_bypasses_blocked。

Ruff 与格式检查通过，见 lint.log、format.log。生产 AST 与字节审计确认修改仅涉及 EventSink.finish、completion_snapshot、run_workspace、download_file、list_files 及新增的 _published_run_file；其余生产节点不变。UTF-8、原 BOM、CRLF 和行尾空白检查通过；源码差异见 production.patch，检查记录见 source-audit.json、validation-summary.json。

按项目约定仅执行 git status，未执行其他 Git 操作。改动保留在工作区。
