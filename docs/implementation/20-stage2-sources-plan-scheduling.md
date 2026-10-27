# 20 · 第二阶段：来源策略、可追加计划与统一调度

日期：2026-10-24（Asia/Shanghai）。

## 状态与范围

本阶段按本聊天确认的阶段划分实施：来源策略、可追加计划、统一调度。它不同于历史 16 号文档中已实现的 S2 research_step 入口替换，不重复建立工具入口、预算账本或证据存储。

代码已写入工作区。用户随后明确要求“无需进行测试了”，已终止仍在执行的测试/真实问答进程，不再启动 pytest、真实模型或搜索请求。最终代码未完成业务验收；此前部分检查的通过记录不代表最终版本全部通过。

保留原有工作区改动，不执行提交、暂存或其他受限 Git 操作，不修改生产数据库。

## 开源依据与采用方式

- [Open Deep Research supervisor_tools](https://github.com/langchain-ai/open_deep_research/blob/1b7d2e80db9faa586165c60e09096dbbfd483a64/src/open_deep_research/deep_researcher.py#L225)：本阶段重新读取研究轮次退出条件、并行任务切片与 overflow 错误处理。采用“阶段结果返回后继续规划”和独立并发上限；超额任务在本项目排队，不照搬将本批超额研究直接返回为错误的处理。
- [GPT Researcher Tavily adapter](https://github.com/assafelovic/gpt-researcher/blob/5cdad9cb434754188b78bd998df18dd8d502cf7e/gpt_researcher/retrievers/tavily/tavily_search.py#L100)：重新读取 query_domains → include_domains 调用。本项目将任务域名范围传入现有 research_step 内部适配器，保留唯一发送审计与重试责任，不照搬其捕获异常后返回空结果的路径。
- [LangChain rate limiter](https://github.com/langchain-ai/langchain/blob/b15a385cb1aa4507b86a3ba1e6430748b0c4ae4b/libs/core/langchain_core/rate_limiters.py#L67)：沿用前期源码核查结论，请求速率不能代替在途请求数或 Token 预算。新增调度器独立控制排队、执行槽和请求间隔，原有单轮预算继续生效。

发布机构身份映射、来源依据登记、追加计划的稳定身份与预算继承、持久任务调度衔接，均为本项目实现，不宣称是上游完整现成功能。

## 来源策略

位置：agent/runtime/source_policy.py、agent/runtime/research.py、agent/runtime/contracts.py、config/runtime.yml。

- 删除 Python 中固定 OFFICIAL 域名集合，改用 sources.identities 配置：publisher、host、include_subdomains、path_prefix、tier、verification。
- 官方身份依据来自操作者确认的映射；模型写“官网”、搜索命中某域名或页面自称官方都不能升级身份。未知身份保留 unknown。
- 域名按完整主机与子域边界匹配；GitHub 身份按仓库路径边界匹配，不把整个 github.com 当作某项目官方来源。畸形地址、模糊路径和相似恶意域名不能通过身份匹配。
- 任务 search_domains 只接受原题实际出现的完整域名；传递到 Tavily include_domains，并在读取及来源消费时再次检查。越界响应保留原始审计数据，不作为可用结果；一个畸形地址不会清除其他有效响应。
- 来源评估区分 eligible、unknown_identity、disallowed_tier、outside_scope，保存身份依据。
- 来源层级由原始任务契约保存。模型面对的 PlanRequirement 不再填写 required_tier，避免凭空添加“官方”条件后反复规划失败。原有完整 RequirementSpec 仍是运行时唯一需求结构。

身份配置需由操作者确认，不是自动发现/认证机构身份的服务。自然语言来源条件的识别仍受原文片段判定规则约束；不声称覆盖所有复杂自然语言表达。

## 可追加计划

位置：agent/runtime/task_plan.py、agent/runtime/requirements.py、agent/runtime/search.py、agent/runtime/reservations.py、agent/runtime/finalization.py。

plan_research 使用一个入口处理：

| operation | 行为 |
|---|---|
| initialize | 登记原始对象与必需需求，服务端生成任务/对象 ID。 |
| append | 校验 expected_version，只追加支持工作；沿用对象身份，或引用当前已审核事实证明新发现名称。 |
| seal | 在任务终结后关闭计划；失败依赖及其后继明确记为未执行，不伪造结果。 |

- 已有任务 ID、需求、证据、调用次数与预算不因追加而重建。
- 新增调查不能添加原题之外的必需完成门槛；闭合任务不能通过原样重新登记继续执行。
- 同对象及其派生调查共享预算归属，按累计尝试扣额，不按新任务 ID 重新发放额度；派生工作继承来源范围。
- depends_on 仅引用已登记任务，形成追加有序依赖；未就绪依赖不会占执行槽。依赖无有效事实时明确拒绝执行，封口时保留阻塞原因。
- 未搜索的其他预算组保留最低首次尝试余量；总调用、搜索及修订额度保持原有上限，不提高预算掩盖故障。
- PlanRequirement 的 conditions 只传显式条件，服务端展开成 RequirementSpec，减少反复输出空型号、地区、渠道字段。
- 原先“所有分工结束就自动交付”改为允许主助手依据阶段结果追加调查，封口后再进入最终交付；预算/时间压力仍可结束采集并保留缺口。

默认配置：

| 配置 | 值 |
|---|---|
| search.max_tasks_per_run | 8，整轮计划任务总数 |
| search.max_concurrent_tasks | 2，每轮同时执行数 |
| search.max_plan_expansions | 3，追加轮次 |

## 统一调度

位置：新增 agent/runtime/scheduler.py，接入 api/server.py、agent/runtime/jobs.py、agent/runtime/models.py、agent/runtime/delegation.py、agent/runtime/research.py、agent/llm.py。

| 资源 | 默认同时执行 | 默认等待容量 | 默认请求速率 |
|---|---:|---:|---:|
| 前台运行及持久后台作业，共享 | 2 | 16 | 无额外速率限制 |
| 全服务子任务 | 4 | 64 | 无额外速率限制 |
| 每模型账户池 | 2 | 32 | 2 次/秒 |
| 每搜索账户池 | 2 | 32 | 3 次/秒 |

- FIFO 排队；前台保持 queued 后才进入 running，容量耗尽时明确返回 503。
- 后台移除独立 Semaphore(2)，使用同一服务运行槽。恢复时暂时没有位置的已受理作业保留在持久队列；新任务遭遇容量上限时拒绝。
- 后台 handler 内的研究运行复用其已持有运行槽；创建新的后台任务时清除执行槽上下文，防止错误继承父任务的槽。
- 模型 MAIN/SUB/REVIEW 使用相同端点与密钥时自动共享池。不同密钥属于同一个供应商账户时，可在 scheduler.model_accounts 将角色映射到同一账户 ID。
- 排队等待在实际模型预算预留、搜索 attempt 预留之前；取消或过期的等待者移出队列，不能稍后开始发送。
- 搜索适配器仍是唯一重试和发送审计执行者，调度器不添加第二套重试。
- scheduler_queued/scheduler_started 记录等待耗时与资源类别，不记录密钥。实际请求耗时与排队时间分开。
- 同一应用在进程内重启时复用未清空的调度器；尚有在途请求时不允许换用不同调度限制，避免清空计数后重复放行。

本实现的配额边界是单进程；多进程/多主机共享限流不在本阶段实现范围。

## 检查点与恢复

- checkpoint_schema_version 升为 13。
- 审核缓存版本为 review-v10-source-plan-contract，缓存键包含来源配置。
- 快照保存计划版本、追加次数、封口状态、依赖、对象及预算归属；来源配置摘要不一致时拒绝继承旧来源信任。
- 保留原始账本、证据与旧快照。旧版本明确中断，不引入旧协议兼容分支。
- 数据库 schema 保持 6；未迁移生产运行库。

## 验证记录与用户停止指令

用户审核通过的第二阶段业务题为：

1. 从 Deep Agents、Open Deep Research、smolagents、GPT Researcher 官方源码与文档比较委派、工具调用、结果返回，并给本项目建议。
2. 先核实 Open Deep Research 正常委派/返回，再根据实际源码风险追加必要异常、取消或任务遗漏调查，复用已有证据。

已复用现有手动脚本的 --stage stage2 选项，不修改既有第一阶段历史证据。相关代码用例在用户停止指令前已经补充，覆盖来源身份、追加/依赖、预算继承、前后台共享槽、取消和持久恢复；此后不再追加或运行测试。

- output/stage2-validation/live-20261024-152540：第一题协调请求达到 60 秒上限；第二题反复出现规划来源层级拒绝并达到总时限。记录均保留为失败，未提高超时或扩大预算。
- output/stage2-validation/live-20261024-153856：更新计划协议后重新执行第一题，已经登记 version=1 的计划；随后按用户要求终止。原始运行库保留中断快照，stopped.json 明确记录主动停止；库中的 running 不表示进程仍在执行或验收成功。
- 最后一次来源层级协议调整、幂等队列处理和依赖链收尾没有继续执行功能测试，不能以此前通过数宣称最终版本已验证。

当前交付结论：第二阶段代码修改已完成，最终业务效果未完成验收。不会自动续跑测试或问答。
