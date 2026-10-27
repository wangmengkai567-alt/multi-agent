# 第 10 步：保存合格查询结果并传给后续任务

日期：2026-10-15（Asia/Shanghai）。状态：第 10 步完成，离线验收通过。

## 工作区与约定

实施目录：D:\My_workspace\Multi-agent\Multi-agent。修改前 git status --short 输出为空。
遵守上层 AGENTS.md，仅执行 git status，未执行其他 Git 命令，未提交。Git 配置查询、git ls-files 和 git diff --check 均未执行；改用实际文件字节、BOM 与新增行末尾空白检查。
根目录 AGENTS.md 和项目 .editorconfig 规定 UTF-8 / CRLF；未发现 .gitattributes。已有文件保留 BOM 状态，新文件无 BOM。代码、测试、文档与检查日志全部纳入最终字节审计。

## 实现与数据结构

新增 agent/results.py，在 DelegationRunnable 的真实执行边界接入 ResultService。工厂为三类专家显式配置同一无状态结果服务，保留框架 task 路由和原调用预算。同步入口通过线程安全桥接在 EventSink 所属事件循环读写 SQLite；生产异步入口直接 await，不在线程内另建数据库连接。独立执行且未配置 EventSink 时不声称持久化，也不会提供共享摘要。

RunStore 使用版本化迁移将 schema 3 升为 4；从 0、1、2 版本也可按既有顺序升级。新增 results 表，不删除旧 runs、events 或 subtasks。每条记录含服务端生成的 result_id、run_id、thread_id、call_id、尝试序号、来源（db / knowledge / search）、生成时间、查询对象、时间范围、统计口径、原文引用及 SHA-256、摘要、完整检查结论与发布状态。历史接口 GET /api/runs/{run_id}/history 新增 results 列表，内部与外部记录分别保存。

每次尝试分别归档，重试前的不合格原文不会被修订结果覆盖；同一 call_id / attempt 不可重复写入。原文包括专家正文和真实 Hook 收集的工具名称、调用 ID、执行状态、返回内容。数据库工具另外保留实际传入工具的 SQL 与表名；查询对象优先取实际成功查询的表，时间范围与报告口径明确作为专家声明，未提供时不猜测。statistical_scope.executed_sql 是传入已成功执行的 SQL 工具的原始语句；数据库工具仍按第 07 步策略加行数限制和超时。

原文 JSON 不超过 16 KiB 时保存在 SQLite；更大时写入本轮 results/{result_id}.json，SQLite 保存相对位置、摘要及校验值。文件显式使用 UTF-8 无 BOM / CRLF，ResultService.read_raw 校验所属轮次、会话、路径边界和原文 SHA-256。业务文件路径策略及框架后端的 write / edit / upload 三条入口均禁止改写 results/；正常读取仍允许。工具结果和原文会对密钥赋值、结构化密码 / token 等字段脱敏，进度事件只带 ID 和状态，不复制正文。

摘要为最多 2,000 字符的原文节选，截断时附标记，不调用额外模型、不把摘要改写当成新事实。只有 passed 且父运行仍有效的记录才发布摘要；消费时还要求所属子任务 completed / passed。insufficient、check_incomplete（包含 return_unchecked 降级）、dependency_unavailable、tool_failed、budget_exhausted 和 not_checked 不会成为可用内部摘要。主任务结束后的晚到结果只归档，不发布。

## 搜索前的实际传递

搜索执行器启动模型与工具前，先等待此前子任务记录入库，再按当前 thread_id / run_id 从 SQLite 读取可用结果。只注入 db / knowledge 的合格摘要，附 result_id、来源轮次、时间戳、对象、时间与口径、原文位置及校验值；不依赖主模型口述“已查到数据”，也不使用模型传入的伪造引用。

追加内容作为数据而非指令，并明确要求内外结果分别展示、保留时间和统计口径差异，关键词或趋势相似不等于事实已证实。相同要求接入主 Agent 汇总提示和各专家提示。首版在存储与历史接口中分开保留两种来源，不做关键词一致性自动判真，也不承诺提示词能保证事实准确性。

注入时不修改调用方 messages / files；补查任务继续保有原任务及内部依据。result_saved 与 result_handoff 复用统一事件出口，记录来源结果 ID 和消费方 call_id。结果行及 result_saved 事件在一个事务中提交，显示发送失败不会推翻已提交结果；记录失败会明确暴露 EventPersistenceError 与 hook_failures，不声称已发布摘要。此前“子任务记录失败但仍返回业务正文”的旧测试调整为明确报告记录失败。

默认且目前仅支持本轮结果，未开放跨轮复用接口；同会话新轮与不同会话都不自动混用旧数据。普通搜索在没有合格内部结果时继续执行，不提前实现第 12 步的业务流程前置条件。Skill、取消接口与事件补发仍属后续步骤。

## 验证记录

使用项目 .venv/Scripts/python.exe（Python 3.11.15）。依赖版本详见 10-environment.log，未安装或升级依赖。全部测试为离线可控模型、临时 SQLite 与本轮测试目录，不读取真实密钥、不调用真实 MySQL / Tavily / RAGFlow 或付费模型。

首次直接运行 pytest 遇到本机临时 / 缓存目录权限错误；后续禁用 cacheprovider，并显式使用工作区 output/ 下各次独立的 --basetemp，未修改系统目录权限。首轮可执行的专项输出为 1 failed, 16 passed in 44.55s，原因是新测试误把协程当作 Runnable，已修复。扩展回归输出为 2 failed, 86 passed in 145.36s，均为原委派测试的精确事件序列未包含新增 result_saved；已保留完整序列断言并加入新事件。

最终执行命令及原始输出（UTF-8 / CRLF）保存在同目录日志：

- 全套回归：python -m pytest -q -p no:cacheprovider --basetemp=output/test-step10-full-01 → 273 passed in 242.19s (0:04:02)，见 10-tests.log。
- 全套启动后，继续补齐嵌套结构化敏感字段脱敏与不可覆盖断言；对最终代码运行 python -m pytest tests/test_result_handoff.py -q -p no:cacheprovider --basetemp=output/test-step10-handoff-final → 17 passed in 85.57s (0:01:25)，见 10-handoff.log。
- python -m ruff check（本步 13 个 Python 文件）→ All checks passed!，见 10-lint.log。
- python -m ruff format --check（同 13 个文件）→ 13 files already formatted，见 10-format.log。
- python -m pip check → No broken requirements found.，见 10-dependencies.log。
- 环境实际版本：Python 3.11.15、deepagents 0.4.3、langchain 1.2.10、langgraph 1.0.9、langgraph-checkpoint-sqlite 3.0.3、pytest 8.3.5、aiosqlite 0.22.1、sqlglot 26.33.0，见 10-environment.log。
- 最终文本字节、BOM 与新增行空白检查结果见 10-text-audit.log。

专项覆盖真实框架同步与异步 db → search 路由、数据库摘要与搜索实际收到的任务逐字段比对、未检查及各类不合格结果隔离、最多一次补查与拒绝原文保留、大文本落盘及重开读取、原文完整性、文件多入口只读、两轮 / 两会话并发、伪造引用、任务输入不变、元数据缺失、嵌套敏感字段脱敏、记录与事件事务回滚、重复记录不可覆盖、主任务结束后晚到结果、v3 升级保留旧记录和 HTTP 历史的内外数据分离。

真实外部服务与真实模型联调：未执行；本步不声称线上业务准确率得到验证。
