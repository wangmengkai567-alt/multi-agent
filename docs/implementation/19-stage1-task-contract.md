# 19 · 第一阶段：任务类型与交付契约

日期：2026-10-24。代码变更保留在工作区，不提交。用户已明确授权修改本阶段代码及测试，并批准下列两道真实业务题。历史方案的“不修改测试/不实跑”限制已由本次授权更新。

## 当前结论

核心链路已接入：先锁定原始任务，再按任务契约生成、审核交付；加工上一轮答复时由服务端绑定同会话已审核结果，禁止重新搜索和新增已核实结论。

**真实业务验收未通过。** 第一题最后一次得到的正文仅有缺口说明；搜索服务实际返回 HTTP 432。第二题重跑后正常完成存储、零搜索、复用第一题交付，并明确没有足够材料生成实施计划。它证明上下文绑定与禁止联网约束生效，不能证明已经完成源码调研或产出实施计划。

## 开源实现如何用于本阶段

沿用此前源码调研，并核对本机安装版本的真实接口：

- [Deep Agents 子代理中间件](https://github.com/langchain-ai/deepagents/blob/a3381668c4d3b558742101fa7e6b8fe4a64e56ef/libs/deepagents/deepagents/middleware/subagents.py)：子代理独立上下文、通过 task 委派、最终消息返回父代理。本阶段不把旧研究对话重灌给“整理上一轮”的生成器，仅绑定经过审核的交付正文。这个任务契约是本项目新增设计，不是上游现成实现。
- [LangChain agent factory](https://github.com/langchain-ai/langchain/blob/b15a385cb1aa4507b86a3ba1e6430748b0c4ae4b/libs/langchain_v1/langchain/agents/factory.py)：工具调用与结构化响应使用各自的绑定方式。本阶段给路由、直接交付和直接审核定义独立 Pydantic 协议；最终响应清理工具绑定的 strict 参数。真实运行确认，将其作为最终 parse 请求的顶层参数会导致 TypeError。
- [Open Deep Research 主图](https://github.com/langchain-ai/open_deep_research/blob/1b7d2e80db9faa586165c60e09096dbbfd483a64/src/open_deep_research/deep_researcher.py)：区分研究协调、子研究和最终报告阶段。本阶段明确采集与交付边界，交付和修订禁用业务工具，审核格式修订耗尽后不再启动一轮相同的完整审核。并发调度、来源策略等后续阶段不在这里扩展。

本地安装 deepagents 为 0.4.3，未擅自升级依赖或改用当前上游才有的接口。没有把 LangChain 的请求速率限制解释成并发数/Token 预算。

## 实施位置和行为

| 位置 | 行为 |
|---|---|
| agent/runtime/contracts.py | 新增不可变 TaskContract、任务类型判定、原文范围检查、是否需要证据/联网、上一轮来源身份绑定。首次业务动作前仅判定一次；显式源码/来源/上传/数据库要求不能被路由为无证据直接答复。规划 Schema 限定原文确实要求的来源级别，避免凭空增加“官方”必需条件。 |
| agent/main_agent.py、agent/runtime/context.py | 接入契约中间件和运行上下文，路由调用计入模型预算。 |
| api/server.py | 仅从服务端同一 thread 的 completed 且已通过 answer_validation 的结果选取上一轮材料；不信任客户端传入的旧答复。 |
| agent/runtime/delivery.py | DirectDraft 与现有事实/分析/限制块协议按已锁定任务选择；保留有证据任务的事实与引用约束。 |
| agent/runtime/finalization.py | 直接加工仅输入原始契约和绑定材料；交付阶段无工具；清理工具级 strict 参数；修订保留审核意见与不可信旧稿，不再被初稿生成逻辑覆盖。 |
| agent/runtime/answer_review.py | 直接交付独立审核，不要求不存在的本轮事实 ID；检查材料忠实性及计划/已实现事实区别；一次修订后仍失败则明确拒绝。通过时使用数据库认可的 complete 覆盖状态。 |
| agent/runtime/hooks.py | 校验锁定范围；直接交付拒绝业务工具；禁止联网时拒绝搜索、网页读取及后台研究提交。 |
| agent/reflection.py、agent/runtime/delegation.py | 审核 Schema 按问题类型约束合法 fact_id/rid，缩短解释；已耗尽格式修订的不完整审核不重复启动整个审核循环。 |
| agent/runtime/checkpoints.py、agent/runtime/models.py | checkpoint 版本 12，审核缓存版本 review-v9-task-delivery-contract；持久化契约并校验恢复范围，路由请求单独记录耗时与用量。 |

数据库 schema 保持 6；没有迁移生产运行数据。旧 checkpoint 保留并中断，不引入兼容或降级执行分支。

## 获准的真实业务题

1. 请查看 Deep Agents、Open Deep Research 和 smolagents 的源码，分析它们如何进行子 agent 协作与工具调用，并结合我的项目提出改进建议。关键结论需要提供源码依据。
2. 不再联网，把上一轮的调研结果整理成一份实施计划，包含修改位置、依赖关系和验收标准。不要补充未经核实的新结论。

执行脚本：tests/manual/stage1_acceptance.py。调用真实生产 ASGI 生命周期、模型与搜索服务，使用独立运行目录，同一 thread 连续提问；没有用假检索结果替代真实服务。

最新证据：output/stage1-validation/live-20261024-125425/summary.json。脚本区分 process_passed 与 business_passed：结束且审核通过只能证明交付流程成立；第一题覆盖不足时，两题整体业务验收仍为 false。

| 题目 | 最新 run_id | 结果 |
|---|---|---|
| 源码调研 | bc9933a5-58cc-47ac-add6-bf6c6d7564f0 | completed，但 coverage=unknown；正文仅有缺口，业务失败。12 次 research_step 搜索动作，原始失败记录含 Tavily HTTP 432。 |
| 不联网整理 | 68d98d5a-fd11-4c96-aaa1-645c4e757688 | completed、审核通过、搜索动作 0、来源 ID 指向上一题；明确缺少可整理的结论。耗时约 20.5 秒，未产出有效实施计划。 |

之前的超时、无可绑定上轮来源、strict 参数异常以及 coverage 枚举写库失败均保留在各独立运行目录和数据库中；未改写历史记录为成功。修正枚举后，使用原始第一题数据库继续执行第二题，未重造来源或注入结论。

## 回归与限制

本阶段测试围绕路由和工具边界、同会话材料绑定、事实/审核协议、报告修订隔离、预算、取消、生命周期和持久化，不增加常识问答或要求模型输出固定文案的能力题。旧测试中不存在的字段和退役入口改为当前协议；不新增旧协议兼容路径。

最终选定的 14 个模块共 **150 项通过（61.77 秒）**，记录见 19-tests.log；Ruff、格式及 27 个相关文件的 UTF-8/CRLF/BOM/空白/AST 检查通过，见 19-static.log。先前扩展检查还发现未迁移的旧委派、事件版本、迁移 fixture 等测试失败，因此不宣称全仓测试通过。

业务验收剩余条件：真实搜索服务可用后，第一题取得三个项目的实际源码证据并形成可复核结论，再用相同第二题验证实施计划。HTTP 432 的响应记录未包含可证明具体账号原因的材料，本次不将其武断归因为额度、计费或密钥失效；未更换服务商、绕过失败或扩大预算。

遵守只允许 git status 的限制；不执行 git diff --check，使用直接文件字节扫描检查 UTF-8、BOM、CRLF、混合换行和尾随空白。
