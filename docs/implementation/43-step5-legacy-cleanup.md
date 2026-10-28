# 步骤 5：被替换实现与接线清理记录

日期：2026-10-28。项目根目录：D:/My_workspace/Multi-agent/Multi-agent。

按 [37 号方案](37-basic-multi-agent-repair-plan.md) 步骤 5 清理。工作区起始干净；用户本轮明确授权迁移相关现有测试。所有修改留在工作区，未执行 Git 版本管理、生产数据库迁移或真实业务题重跑。

## Codex 主要来源与采用边界

先读取 openai/codex 的固定提交 970111f6cb62cc02fe36a82ea7d71b37a2889c97 中相关执行与历史代码；另检索其默认分支的历史和 Skill 实现。主要依据如下：

| 具体问题 | 主要来源与采用方式 |
| --- | --- |
| 前端把旧审核结论当成当前执行结果 | [core/src/agent/status.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/agent/status.rs)：由实际开始、完成、错误和中断事件推导状态。本项目前端只用分工执行终态及服务端 completion，不再读取 check_status 判定当前成功 |
| 回放旧事件重新启动旧状态机或混淆分工 | [app-server-protocol/src/protocol/thread_history.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/app-server-protocol/src/protocol/thread_history.rs)：持久化回放和当前事件使用共同 reducer，按原始轮次及条目身份重建视图。本项目保留 applyEvent 的统一入口、运行/序号去重及父调用归属；清理旧审核、研究计划与补查状态分支，旧消息和原始详情继续可读 |
| 工具返回或等待结束被当成整个任务成功 | [multi_agents_v2/wait.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/tools/handlers/multi_agents_v2/wait.rs)：明确区分消息、输入和等待超时。保留工具、分工和整轮不同层级，不用工具返回或子任务完成推断整体完整 |
| 旧身份、结果发布接口和重复运行状态 | [multi_agents_v2/spawn.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs)：执行身份、状态及协作观测由程序管理。继续采用运行/分工/调用身份；移除重复 scope_id、旧发布/恢复 API 和无人调用的旧模型阶段，不建立第二条执行或发布链 |
| Skill 仍指向已删除的审核和文件协议 | [ext/skills/src/catalog_prompt.rs](https://github.com/openai/codex/blob/26ad281fd1ca712e4f0c7920a6273cf18c961b8a/codex-rs/ext/skills/src/catalog_prompt.rs)：以当前实际可用目录及读取的 Skill/引用为执行说明来源。核对本项目完整生效目录和引用，修正与真实能力矛盾的指令；这是说明一致性适配，不声称 Codex 提供了这些业务 Skill |

Codex 没有本项目 Python 旧审核模型、SQLite 结果表和业务 Skill 的现成迁移补丁。以上采用的是其程序管理身份、事件驱动状态和统一历史投影机制；具体 Python 删除、历史约束保留及材料说明依据本项目真实依赖调整。未接入 Codex Rust 内核、SDK 或旧协议兼容执行路径。

## 清理结果

- 删除 agent/runtime/review_state.py 的旧审核模型、判断及发布过滤。移除 RunStore 的 record_result、get_current_result、get_result、list_results；唯一历史结果读取为 _historical_results，只用于认证历史视图。
- 原 v6 结果表约束移到 api/legacy_schema.py，只由已有显式离线迁移调用；与原 constraints_sql 和 PRE_REVIEW 完全一致。保留历史表、字段和原始行，不为本轮清理增加数据库版本或自动迁移。旧 subtasks.check_status 的非空约束仅在存储插入处填固定占位值，运行内存、结果 JSON、状态更新和前端不再依赖它。
- 删除 review 预算类别、近阈值旧配置、审核/报告修订等无调用模型阶段、model_phase、budget_retry、旧计量恢复函数、来源状态及部分答案恢复分支。模型请求继续逐次计量，工具和合法只读重试继续原子扣费；取消与超时后保留已完成分工记录，不发布整轮成功答案。
- 删除无人调用的后台 job_id 创建入口和 unavailable_agents 状态；保留明确的专家注册、材料绑定、联网权限、资源回收、独立运行入口、当前运行图检查点及历史归属检查。已有 jobs.db 仅保留安全验证和显式历史迁移，不删除或自动续跑。
- 前端移除旧研究计划、逐条审核/修订、隐式降级、澄清与旧额度提示的专用接线。当前分工只按实际生命周期分组，错误与取消按实际状态显示；历史事件使用保存的消息和原始详情，不能产生当前计划或审核结论。
- 核对五份生效 Skill 和所指引用：删除 result_id 必填、独立报告/审核、正文终审、自动附加内部摘要和上一轮材料读取约定；纠正 Excel 仅首表前五行、Word 仅顶层表格等旧描述。保留来源、位置、局部读取、解析局限及工具能力边界，最终正文仍由程序保存为同一份 Markdown。

## 测试迁移与执行证据

证据目录：[output/verification/step5-cleanup-20261028/](../../output/verification/step5-cleanup-20261028/)。before/ 保存修改前源码，diff/ 由这些原始字节生成；不使用 Git diff。

原有保护断言没有为通过而删除：审核模型预算用例迁移至仍在执行的主/子模型预算；旧 review 和 near_ratio 配置改为明确拒绝。主、子、自定义配置仍分别验证凭据隔离。原前端审核用例迁移为分工、工具、失败和交付状态；保留序号去重、乱序回放、认证、取消竞态、下载隔离和转义断言。新增历史检查位于已有跨运行历史用例内，只在临时数据库注入归档行，验证原文、摘要、审核 JSON、发布标记及事件重复读取不变，且不进入后续运行。

首次回归 235 项：225 通过、10 失败，保留 regression.xml/log。其中 9 项由本轮修改 expert_available 参数时漏改一个实际委派调用点引起，已补齐；另 1 项为此前记录的 Windows 标记文件存在但尚未写完，int('') 失败的既有竞态，未修改该测试或生产进程实现。修正后相关 57 项通过（recheck.xml/log）；认证历史、事件与回放 24 项通过（history.xml/log）。首次失败没有覆盖或改记为通过。

最终源码的 14 个受影响测试文件集中回归：**307/307 通过，无 skip/xfail**（final.xml/log，655.85 秒）。范围包括三助手实际委派与工具、失败和取消、运行初始化与关闭、原子预算、Skill 加载、模型配置隔离、来源及输出边界、认证历史与事件回放。首次 235 项记录、修正后的复核和最终 307 项分别保留，不将不同轮次相加计为独立验收数量。实际四次模型调用与计量 ledger 的四条记录一致，且分工内存结果不再包含 check_status。最终已有 Windows 进程回收用例也通过，但该测试的标记文件读取竞态仍未修改。

Ruff、格式及前端三项已通过。dependency-audit.json 确认冻结迁移约束一致、旧结果方法和已删除状态引用为空，Skill 不再包含所清理的旧约定。文件编码、既有 BOM、CRLF 和变更行空白在 source-audit.json 记录。只执行 git status；按项目更严格限制未运行 git diff --check，改用原始快照和实际字节检查。

本轮只证明步骤 5 的清理及对应离线链路。没有调用真实模型、Tavily 或业务数据库，没有重跑此前因 Tavily 配额失败的原第 1 道业务题；37 号方案的完整真实业务验收仍未完成。程序对实际执行、来源归属与交付一致性的检查不能证明全部自然语言事实。
