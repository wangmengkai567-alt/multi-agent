# 17 · 修复实施与验收记录

> 对应方案：16-repair-plan.md v7。用户已明确授权实施；方案中的“本次仅优化文档”是上一阶段边界，不作为本次代码实施指令。
> 代码保留在当前工作区，未提交。仅使用 git status，未新增或修改测试。验证资料在 output/replay/v7；原生产数据库未迁移或写入。

## 已实施的主链路

| 问题 | 生产实现与结果 |
|---|---|
| 整份未通过导致合法事实一起丢失 | runtime/review_state.py、review_contract.py、facts.py：逐事实判断；引文和日期错误强制拒绝对应事实；有效同组事实与依赖仍成立的分析保留；缺键、重复、越权 ID 不接受。 |
| 需求完整性与适用范围 | requirements.py、task_plan.py：程序签发 rid/gid；原文 anchor；未指定维度保持 null；可选条件不升格；无法明确归属的原始子句保留 main unknown；覆盖矩阵必须完整。 |
| 旧判定和历史信任混用 | RunStore v6、ResultService、delegation、API/JS 同批使用统一状态；handoff 与 publishable 区分；最新失败遮蔽旧成功；历史原文与原审核保存，不转成逐事实通过。 |
| 工具入口过复杂、重复外发与扣额 | 仅向研究模型提供 research_step；适配层保留 search/read 实现。精确实际请求 key、in_flight 唯一执行者、缓存本地登记、逐 attempt 原子预留、独立 transport.jsonl；外层和 SDK 不再重复重试。 |
| 有预算却无法产出报告 | reservations.py：每子任务报告/审核预留；预留不计消耗；首次报告/初审 remaining 与不可抢占的 revision_floor 1+1 分开，修订时才转入可消费余额；R3仅重审不重写、不重搜；R1/R2/R3按必需模型/工具耗时检查准入；只剩 report 时直接结束采集。 |
| 不变草稿反复审核 | reflection.py：规范 JSON 指纹、真实证据、需求/契约/配置/模型版本；完整终态 passed/insufficient 才可复用；不完整检查不缓存；任务跨 gap 重复正文不推进 evidence_version。 |
| 终稿夹带无依据事实 | delivery.py、answer_review.py、finalization.py：只渲染 DeliveryDraft；每事实/分析有当前有效事实 ID；未用事实逐条说明原因；每块独立终审，coverage 只引用终稿实际采用事实。 |
| 限制说明夹带价格、承诺以后补充 | limitation 不允许模型写正文，仅允许未覆盖 requirement_ids；程序以原始需求生成状态文字；数据库 degraded 时必须有程序生成的降级说明。 |
| 模型审核漏条目 | review Schema 限定 facts/analysis/claims/coverage 数组的本次准确数量与合法 ID；程序仍检查集合一致性，不把 schema 数量当成语义正确。 |
| 后台/恢复和页面状态漂移 | checkpoint v10 保存需求、gap、来源/缓存、账本、状态和交付映射；预留恢复按稳定 task_id 原子接管旧 UUID owner，不重置消耗；恢复必须符合数据库当前结果指针；旧 checkpoint 明确中断并保留原文件。后台 job 使用稳定逻辑 scope，独立发送记录保留实际执行 run ID。UI 只读服务端 coverage，后台失败/取消优先，不从 completed/日志 passed 推断完整。静态脚本 URL 带明确版本避免旧浏览器缓存。 |

内部分析允许主助手基于合法事实提出新的综合分析，analysis_ids=[]；经终审后才记录 approved_analysis。已有分析必须使用真实 ID，不能伪造。此方式覆盖主助手“谁值得买”的综合责任，避免先要求不存在的分析 ID。

数据库 schema v6、checkpoint v10、审核契约 review-v7.5-complete-review-schema 是不同版本概念。旧后台快照不自动恢复；首次使用新代码打开数据库会按事务迁移。迁移已在隔离副本上验证，未在生产库执行。

## 验证证据及其边界

| 证据 | 实际验证 | 不能据此声称 |
|---|---|---|
| baseline-manifest.json / baseline-runs.db | 冻结原始输入和结果；实际4份原始结果（Apple有2次），历史 coverage 0 complete/1 partial/12 unknown；32条结果原文与原审核保留。 | 旧整份 passed 等于新逐事实通过。 |
| semantic-baseline-quotes/verification.json | 使用真实审核模型、只给各事实自己的引文：无金额依据的200元券、额外续航断言、HRV等错误被拒绝，其他合法事实保留。 | 所有模型与所有重跑均作相同语义判定。较早较宽来源上下文的失败保留在 semantic-baseline。 |
| offline/final-independent、fact-errors-final | 实际生产 parser/ReviewContract：错误日期与外来引文强制拒绝，合法同组事实保留；缺/重/外来事实、分析、claim、支持边拒绝。 | 固定审核响应能证明审核模型准确。 |
| offline/research-boundaries、research-delta | 生产研究入口缓存、并发、归属、持有预算；发现并修复“用尽新外发额度后也不能免费读缓存”与“报告自身预留不能消费”。 | 没发过真实HTTP也等于外部发送计数正确。 |
| offline/transport-final | 实际 requests 适配层对本地HTTP服务；搜索/读取429重试、500、401、超时、预发送/已发送取消；独立发送日志与逐 attempt 预算对齐。 | 等同供应商接收或账单。 |
| offline/review-replay | 同一冻结真实审核响应重放3次业务结果一致；键序/排版同指纹，mW/MW、数字/证据/版本变化重审；终态 insufficient 复用零新增审核。 | 模型三次生成都一致。 |
| offline/migration-final、recovery-final | 版本0–5迁移到6并重开，索引/FK/原始数据保留；迁移故障回滚；当前结果指针、scope校验；恢复已观察发送与未观察预留的保守处理。 | 生产库已升级；版本10完成了新的真实进程崩溃恢复实跑。恢复边界证据主要在此前checkpoint8协议上，v9增加终稿协议边界，v10增加独立修订floor布局。 |
| offline/program-limitations、program-limitations-independent | 程序渲染、禁止任意正文/非法ID/过时或已覆盖需求；生成正文实际进入审核；独立发现并修复“完全不写限制块可漏数据库降级说明”；增量验证通过。 | 用合法ID绑定的自然语言必然真实。 |
| offline/review-schema-final | 实际生产动态Schema包含本次精确条数和ID（含0条）；未调用模型/网络。 | 当前供应商一定遵从Schema或不会超时。 |
| offline/delivery-final/ui-verification.json、实际浏览器 | 服务端 coverage、后台 job 结果/失败/取消在展示函数中正确；刷新真实页面后历史失败与部分完成显示一致，旧脚本缓存问题已复现并修复。 | 其他所有浏览器与所有交互均无缺陷。 |
| final-file-audit.json、gate-audit.json、production-isolation.json | 变更文件UTF-8/CRLF/AST/尾随空白检查；旧协议可达引用扫描；原生产三库大小/时间/摘要未变。 | 已做Git提交或git diff检查。 |

## 真实浏览器、模型与检索实跑

原题均为：**帮我对比华为 FIT 5、小米手环 10 Pro、Apple Watch SE3，双11 各多少钱、谁值得买。**

所有运行使用 output/replay/v7/live 下独立 runs/checkpoints/jobs 库及文件目录。原生产库保持不变。每轮失败均保留，不挑选成功截图替代失败。

| 轮次 | 执行 run ID 前缀 | 观察及处理 |
|---|---|---|
| 1 | ac422ffd | 前台300秒超时；规划自加维度、报告持有额度不可消费。修正精确参数反馈和报告预留激活。 |
| 2 | cb97d627 | 终稿依赖/虚构分析ID导致失败。改为动态合法ID，主助手新分析基于事实接受终审。 |
| 3 | ff239ee2 | partial；人工发现已结束任务被写成尚未返回、限制块夹带价格。增加真实采集状态及逐块审核。 |
| 4 | 3b16c312 | partial；历史优惠被扩展成当前推荐。明确参考时间和限定口径。 |
| 5 | 7168caca | partial；部分事实被错误日期拖累，限制说明仍可能夹带数字。错误改为逐事实拒绝，限制正文改由程序生成。 |
| 6 | 6b276c2a | partial；仅保留华为兼容性事实，三款双11价格仍缺口。不可称完成了购买对比。 |
| 7 | a17fe24a | failed/unknown；终稿含残缺句子和无依据分析；终审漏条目、修复后超时，程序拒绝发布。按准确条数约束动态审核Schema。 |
| 8 | 44afd8d6 | failed/unknown；终稿修订触发60秒模型请求超时。保留硬上限，缩减非必需背景展开；补保存每次无效DeliveryDraft及其具体契约错误。 |
| 9 | 053cc0cd | failed/unknown；首稿采用与省略ID冲突；修订稿终审把不匹配型号/系列/Pro的支持事实标为covered，程序拒绝。原日志还保留1次规划决策被归为invalid_argument。未满足最终出口。 |

部分回答只表示诚实发布已核实部分；不等于已查齐三款价格或已给出完整购买建议。程序拒绝错误草稿是防护生效，也不能把该次失败改称用户任务成功。

## 后续实测新增的确定性修复

- 独立最小反例确认：原 admission 1+1 实为首次报告/初审额度，会在修订前耗尽。现新增独立 revision_floor；竞争调用不能抢占；R1可激活完成；失败admission不留下active任务或预留。证据：offline/admission-revision-independent/verifier-report.json（失败）及verifier-delta-report.json（修复后通过）。
- R3实际生产sync/async循环核对：执行器1次、审核2次，原报告和证据不变，工具/搜索0；时间不足或修订次数耗尽不会启动。R2按3次模型请求+2次工具请求的上限检查时长。证据：offline/R3-recovery-independent/verifier-report.json。
- 同一独立报告还确认重启后旧UUID预留孤立的缺陷；已按稳定task_id接管，旧owner与spent原始身份仍可追溯。多个旧owner对应同一task时明确中断，不猜测。实际进程崩溃端到端仍不声称已覆盖。
- DeliveryDraft结构失败也写入results/delivery/draft-UUID.json，保留可见原稿和准确contract_error；有效稿保存程序渲染映射。证据：offline/delivery-attempt-audit/verification.json。

## 现有测试的真实结果

未创建或修改 tests。`python -m pytest -q` 在收集阶段出现7个错误：旧Finding字段和已删除visible_search_summary的导入。完整输出：output/replay/v7/existing-pytest.txt。

另选取消、生命周期、并发、事件四个现有文件运行：**47 passed / 3 failed**。失败为旧schema版本期待5（现为6）；两项样例仍走已移除的旧搜索重试入口/不带结构化报告的自由文本审核，未进入其期待的执行点。完整输出：existing-runtime-pytest.txt。此前调用预算组也有旧外层搜索重试断言失败。前端 run-events 通过，app/call-groups 仍期待按旧状态推断完成，失败保留。没有为过测恢复旧协议或修改断言。

Ruff agent/api/tools 与 node --check api/static/app.js 已执行通过。它们证明静态检查与语法，不证明真实问答正确。未执行被项目禁止的 git diff --check，以文件真实字节和尾随空白扫描代替。

## 尚未逐项满足的方案出口

以下不以“本轮未发生”当作验证通过：

1. 当前一轮需求注册后不可改写；新用户需求创建新的RunContext和缓存域。尚未实现同一上下文中query_version递增并保留旧需求，以及gap的requirement_changed/claim_resolved/superseded三种dropped操作。
2. 当前gap按rid注册与重算；未实现主终稿反查的claim专属gap/source_fact_id生命周期。终稿只允许删除/修订无支持断言，不能重启采集。reason/source_fact_id/drop_reason尚不是完整可达生命周期。
3. 全量现有测试尚未适配新协议。用户未授权改测试，本次保持原样；不能宣布整个回归套件通过。
4. 任意自然语言事实、scope和分析推导仍需语义审核；合法引文/ID/Schema只能证明结构边界。当前真实实跑有语义误判、审核格式修复及超时记录，不能承诺任意问题均不再出错。

用户要求停止反复测试并先给修复方案后，已停止追加测试和模型/检索实跑，关闭隔离验收服务。后续收敛方案见18-repair-convergence-plan.md。末尾的精确省略ID诊断与终稿修订携带原稿改动尚未再做实跑，不计作已通过。

因此，此记录不将S0–S5全部标记完成。已完成的主路径与未满足出口分开，后续修改需继续保留这些证据和限制。
