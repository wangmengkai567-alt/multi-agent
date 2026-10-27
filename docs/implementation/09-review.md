# 第 09 步：子 Agent 结果检查与有限重试

日期：2026-10-14（Asia/Shanghai）。状态：第 09 步完成，离线验收通过。

## 工作区与约定

实施目录：`D:\My_workspace\Multi-agent\Multi-agent`。修改前执行 `git status --short`，实际输出为空。遵守上层 AGENTS.md，仅使用 git status，未执行提交、分支或其他 Git 操作。改动留在工作区。文本使用 UTF-8 / CRLF，保留已有 BOM 设置，并做字节检查。

## 接入行为

新增 `agent/reflection.py`，使用统一的 ReviewResult：verdict（判定）、issues（问题）、missing_evidence（缺失依据）、revision_instruction（具体修订指令）。严格校验类型、额外字段和判定一致性；内容不足必须带问题和具体补查指令，通过不能带未解决问题。

检查接在第 08 步 DelegationRunnable 的真实执行边界。同步 invoke 与异步 ainvoke 均支持，生产异步路径直接 await 检查模型。沿用原框架 task 路由，未增加调度器。

每次尝试单独绑定 ContextVar 工具证据收集器。HookToolMiddleware 在 after_tool 前捕获工具名称、模型调用 ID、是否实际执行、业务状态、分类错误及实际返回内容。这些原始依据只在本次检查的内存中使用，不另建监控协议或把完整工具正文写入事件。同步线程和异步并发分别继承所属尝试的收集器；相同工具调用 ID 的成功重试可以覆盖此前临时失败。

检查顺序：

1. 核对实际工具失败，区分预算耗尽、依赖不可用与工具失败，不接受最后回答自称成功。
2. 检查空结果、配置要求的真实工具记录、空工具依据、关键字段。
3. 搜索要求来源时，回答必须含实际 internet_search 返回的网页 URL；自行编造 URL 或仅声称已检索不能通过。
4. 规则通过后，按注册项 model_check 配置调用检查模型，输入原任务、结果和真实工具记录；只接受 passed / insufficient 的 JSON 检查结论。规则不通过时不会调用模型将其改成通过。

默认生产注册项启用检查和模型检查，最多补充查询一次：db 要求 get_table_data 的字段与样本查询以及 execute_sql_query；search 要求 internet_search 和匹配的来源 URL；knowledge 要求 create_ask_delete。ReviewConfig 可分别配置 enabled、model_check、required_tools、required_fields、require_sources、max_refinements；max_refinements 仅允许 0 或 1。自建注册项沿用 disabled 默认值，保持此前显式配置的兼容性。

模型检查复用专家对应的原始模型，SDK 隐式重试必须为 0。每次检查先原子预留 review 预算；补查先预留 retry 预算，再执行专家图，子模型与工具继续经过既有预算和 Hook，受整轮总限制。单个子任务保持同一服务端 call_id，before_delegate / after_subagent 各执行一次。第 07 步的幂等工具网络重试仍按自身配置处理，不与本步内容补查混淆。

内容不足时，在原任务后追加具体缺失项作为 HumanMessage，重新执行一次；不修改调用方输入，也不会因为仍不合格而执行第三次。依赖缺配置、权限或最终网络故障直接返回 dependency_unavailable；其他业务错误返回 tool_failed；不让模型补造依赖结果。预算耗尽返回 budget_exhausted。

检查模型缺失、异常或返回非法结构均为 check_incomplete。`config/runtime.yml` 增加 `review.on_check_failure`，默认 reject；可显式配置 return_unchecked，返回带 check_incomplete 标签的未检查内容，仍不标为 passed。配置接受值经过启动校验。取消异常继续传播，清理证据上下文和 active_call，不进入补查。

## 返回与持久记录

新增 subagent_review 和 subagent_refinement 事件，经既有 make_event / EventSink 保存后推送，携带同一 run_id、call_id、parent_call_id 和 attempt。每次检查结论、问题与缺失依据可在历史事件中追溯；修订指令仍按统一参数脱敏规则记录，完整指令实际交给专家执行器。

subtasks.check_status 保存最终判定，模型给出的失败原因写入 error 前也经过 redact_text，避免由问题描述泄露敏感字段。通过时返回原专家结果；不合格、依赖失败、预算耗尽及默认拒绝的未完成检查返回明确的结构化检查说明，不将被拒绝的专家正文作为合格回答传给主 Agent，子任务业务状态为 failed。显式 return_unchecked 时业务状态 completed，但检查状态仍为 check_incomplete。after_subagent 在检查完成后触发并携带相同检查状态。沿用 schema 3 的 check_status 文本列，无数据库迁移或旧数据删除。

## 验证记录

使用 `.venv/Scripts/python.exe`，离线测试禁止网络、真实凭据和付费服务，SQLite 与输出目录均为临时位置。

- `python -m pytest tests/test_delegation.py -q`：`24 passed in 43.28s`，确认原委派行为兼容。
- 首轮 `python -m pytest tests/test_subagent_review.py -q`：`17 passed in 27.70s`。
- 扩充同步、并发隔离、检查取消、业务异常、恢复重试与空依据测试后，专项：`22 passed in 44.70s`。
- `python -m pytest tests/test_hook_pipeline.py::test_production_builder_and_persisted_child_correlation tests/test_subagent_review.py -q`：`23 passed in 28.69s`。
- 全套 `python -m pytest -q`：`255 passed in 109.77s (0:01:49)`，包含新增的真实 db 路由补查测试。
- 最终 `python -m pytest tests/test_subagent_review.py -q`：`23 passed in 22.77s`。
- 最后补齐模型检查问题文本的错误记录脱敏，并新增持久历史脱敏回归测试；最终 `python -m pytest -q`：`256 passed in 119.82s (0:01:59)`。
- `python -m ruff check agent/reflection.py agent/runtime/delegation.py agent/runtime/hooks.py agent/runtime/policies.py agent/subagent_registry.py agent/subagent_factory.py tests/test_subagent_review.py tests/test_hook_pipeline.py`：`All checks passed!`。
- 对上述 8 个 Python 文件执行 `python -m ruff format --check ...`：`8 files already formatted`。
- `python -m pip check`：`No broken requirements found.`。
- 对本步 11 个改动文件执行字节检查：`UTF-8 / CRLF byte audit passed: 11 files; no bare CR/LF.`；全部可严格 UTF-8 解码，没有裸 CR / LF。

初次全套回归出现生产构建假模型脚本缺少检查响应的失败，以及调整期间测试 fixture 共享的注册错误。已修复：生产入口假模型增加明确的检查 JSON 与 review=3 计数断言，保留真实工具执行、父子 ID 关联和隐私断言；新专项使用独立临时 runtime fixture，避免依赖其他测试模块的 fixture 注册顺序。最终验收以修复后的全套结果为准。

专项覆盖空结果、缺来源与伪造自述、标准化工具失败、依赖不可用、检查模型失败、显式降级、review/retry/total 次数耗尽、最多一次补查、并发证据隔离、同步与异步调用、取消传播、默认生产启用配置。真实框架 search 和 db task 路由证明专家实际收到具体缺失项；db 补查真实经过字段查询与 SQL 工具 Hook。

真实模型与 MySQL/Tavily/RAGFlow 服务联调：未执行。本步完成离线可控模型和真实框架调用验收，不据此声称内容准确率提升。整轮超时、运行中外部请求强制停止、共享合格结果、Skill 与前置流程仍按后续步骤实施。
