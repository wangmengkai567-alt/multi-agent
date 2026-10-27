# 基本多智能体修复记录

日期：2026-10-27。依据 37 号方案及用户最新“主要功能稳定可用、减少复杂功能”的要求实施。本轮不执行版本管理操作，所有改动保留在工作区。

当前状态：主链路及步骤 2 至 5 已实施；348 项 Python 回归与 3 个前端用例通过，依赖、Ruff 和格式检查通过。原第 1 道真实题已执行一次，因 Tavily HTTP 432 套餐用量限制失败，无成功来源、无成功文件；未满足 37 号方案的完整业务验收条件。已停止，不进入第 2 题或重跑。

## Codex 作为主要方案来源

固定引用 openai/codex 的 970111f6cb62cc02fe36a82ea7d71b37a2889c97 版本，先阅读相关实现，再适配当前 Python/Deep Agents 框架：

| 本项目修复 | 主要来源与采用内容 |
| --- | --- |
| 基本委派 | [multi_agents_v2/spawn.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs)：模型提供任务，程序控制身份、权限、实际创建与状态，不添加逐条事实或原文锚点前置条件 |
| 状态与等待 | [multi_agents_v2/wait.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/tools/handlers/multi_agents_v2/wait.rs)：按真实执行事件与期限报告状态；不把受理或等待结束等同于业务成功 |
| 错误边界 | [function_call_error.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/tools/src/function_call_error.rs)：可纠正参数错误反馈给模型；终止、取消和资源错误不能伪装成成功结果 |
| 资源回收 | [exec.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/exec.rs)：保留期限、取消、输出边界及进程组回收的职责；现有 Windows Job Object 保护继续生效 |
| 验证环境 | [test_codex.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/tests/common/test_codex.rs)：隔离存储、可控外部依赖、真实初始化入口，沿用前一轮已建立的验证环境 |

技能识别还参考 [skills/parser.rs](https://github.com/openai/codex/blob/a5ef02e56147c4c3da493655fd5e2c64db13210f/codex-rs/skills/src/parser.rs)：从原始文件内容提取 frontmatter，不把展示封装交给头部解析器；未引入其容错或补缺分支。

Codex 没有本项目所用 MySQL/sqlglot 只读查询校验的现成实现。该项按本地 SQL AST 修复：拒绝没有结果表达式的 SELECT，继续保留只读权限、表白名单、危险函数、行数和超时保护。并非因没有可复制的 SQL 函数而放弃使用 Codex 的多智能体执行结构。

## 实际行为

- 主助手按需要向 db、document、search 委派完整任务说明，子助手直接使用现有工具并返回结果。
- 所有分工由程序签发独立 ID 并保留所属运行、父调用和实际工具证据。一次模型消息提出多个分工时，程序通过每轮单槽队列顺序执行，不因批量请求而直接终止。
- 搜索直接使用 internet_search/read_web_page。真实发送前核验运行仍有效，限制服务并发及隐式重复发送。来源只来自本分工实际工具结果；不同分工不能借用网页来源。
- 绑定上传内容摘要；文档位置、支持范围、截断与解析限制保留。文件读取变化后拒绝继续使用。
- 成功分工结果与失败分工分别保存；部分失败会在正文明确披露。调查没有有效结果、引用虚构 URL 或取消后迟到返回，不能发布成功文件。
- 最终正文由程序直接保存为 answer.md。UTF-8/CRLF 文件字节必须与保存的正文一致，通过所属运行的认证下载入口交付。

去除强制研究计划、字面对象名、逐字锚点、动态需求图、逐条 fact_id/rid、独立报告生成/语义审核、嵌套委派、后台研究与检查点自动恢复。保留现有框架、三个专家、认证和资源保护。生产 Python 文件从 72 个、18,452 行收敛至 50 个、9,196 行（统计时点记录；最终以源码为准）。行数减少仅说明范围收敛，不构成正确性证明。

历史 RunStore、认证迁移与历史结果表的读取保留；既有生产数据库、jobs.db、历史业务内容和 .env 未改写。前端保留历史事件展示，不再提供后台调研或动态需求操作。

## 验证义务迁移

没有使用 skip/xfail 掩盖失败。18 个专用旧协议测试文件被替换；原文件字节、179 个测试函数名称与迁移记录保存在 output/verification/basic-repair-before/。最终报告同时列出与步骤 1 基线相比新增、移除和变化的用例；本轮通过数量不能解释为把旧 625 项逐项修成通过。

| 原验证义务 | 当前执行证据 |
| --- | --- |
| 实际委派、独立 ID、多分工结果和返回 | test_basic_coordination：真实认证 HTTP 入口、生产构建器、实际框架 task 和三个专家；分别覆盖逐个提出与同回合批量提出分工 |
| 搜索、网页来源、失败来源不可引用、跨任务/跨运行隔离 | test_basic_coordination、test_basic_boundaries：真实工具响应规范化、来源完整性、当前分工域名约束、伪造来源拒绝 |
| 数据库只读、白名单、连接前拒绝危险 SQL、连接回收 | 原 test_tool_policies 有效断言保留；基本协作场景另外验证 SQL 结果属于对应分工 |
| 上传文件、读取位置、摘要绑定与只读性 | test_basic_coordination、test_basic_boundaries、原 test_tool_policies：实际受控文档进程与绑定文件；保留原 Skill 读取、范围和隔离断言 |
| 配额、重试、模型期限与上下文限制 | 原 test_call_budget、test_execution_governance 中独立有效断言迁移保留；模型 SDK 无隐式重试、工具按真实尝试扣费 |
| 排队、取消、超时、迟到返回、服务关闭与资源释放 | 原 test_run_cancellation、test_stage2 调度断言及 test_basic_boundaries；包含实际 Windows 子进程与后代回收 |
| 历史、持久化顺序、认证、Origin、CSRF、所有者与下载隔离 | 原 test_runs、test_events、test_event_replay、test_run_history、test_verification_environment、test_tool_policies 相关断言保留 |
| 最终正文与文件一致 | test_basic_coordination 和 test_run_history 检查真实认证下载的字节、摘要及终态；不存在虚构的模型文件工具调用 |
| 逐条语义审核、需求覆盖图、后台恢复与嵌套任务 | 对应功能已移除，其专有协议断言不再作为基本功能要求；没有重建兼容实现来满足旧断言 |

## 最终验收记录

- 最终完整离线记录：output/verification/basic-release/report.json、pytest.xml 及各阶段日志。Python 348/348、前端 3/3，依赖、Ruff 与格式检查均通过；不存在 skip/xfail 豁免。
- 原第 1 题：output/verification/basic-live-question1/question-1.json。运行 61e46d69-94d2-4834-a5e0-1bedb06bab57，181.58 秒，实际委派与工具请求发生，运行最终为 failed；没有成功来源，没有生成 answer.md。没有进入第 2 题，也没有重跑业务题。
- Tavily 实际响应 HTTP 432。其[官方错误说明](https://help.tavily.com/articles/8645538886-understanding-http-errors)将此定义为 Plan Limit Exceeded。需先恢复该账户可用额度；本轮未更换引擎、扩充预算或购买服务。
- 真实题提交前，验收脚本的 Headers.update 用法曾导致初始化失败；当时尚未提交业务题。修正为传入映射后，才执行上述唯一一次业务题。自动回归源码清单与真实运行清单分别保存，生产执行源码未在二者之间修改。
- 字节与空白审计：output/verification/basic-repair-before/workspace-audit.json。检查 UTF-8、CRLF、既有 BOM 及新增行空白；仅执行 git status，按项目限制未执行 git diff。

### 正式启动仍需完成的既有配置

本机 data/runs.db 为 schema v6、没有 security_identity，.env 未设置 APP_ACCESS_PASSWORD。它们是已有安全配置的前提；本轮没有自动改写生产库或 .env。

已在生产数据库的一致性副本上验证现有显式迁移：output/verification/basic-production-copy/copy-verification.json。迁移成功，核对的历史业务内容保留，生产原件未变。正式执行前按 README 配置独立访问口令，并显式运行既有迁移命令。

### 后续验证范围

按本次修改选择实际链路验收，覆盖必要的委派、工具执行、结果回传及受影响的取消、隔离或交付边界。简单模型问答不作为功能验收，也不默认反复执行全量回归。

程序只校验真实执行、来源归属、引用 URL 与交付一致性，不声明能机械证明全部自然语言结论。复杂业务结论仍应核对实际来源；文件解析不增加 OCR 或公式重算能力。
