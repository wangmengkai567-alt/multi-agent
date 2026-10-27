# 21 · 第三阶段：引文登记与完整模型请求计量

日期：2026-10-24（Asia/Shanghai）。

## 实施状态

按本聊天确认的第三阶段实施：减少报告生成的重复引文输入，保留逐字证据校验，记录完整模型请求规模和各阶段耗时。代码保留在工作区，未暂存或提交，未修改生产数据库。

沿用用户“无需进行测试了”的指令，本轮未新增或修改测试，未执行 pytest、业务问答、真实模型或搜索。仅做源码核对、静态检查和文件格式检查。因此不能声称请求缩小了某个实测百分比、响应速度已改善或业务验收通过。

## 核对的开源实现

| 代码 | 本轮核对内容 | 本项目采用方式 |
|---|---|---|
| [Deep Agents summarization.py](https://github.com/langchain-ai/deepagents/blob/a3381668c4d3b558742101fa7e6b8fe4a64e56ef/libs/deepagents/deepagents/middleware/summarization.py#L279) | 根据模型上下文能力配置摘要触发/保留范围；使用 token 或消息数管理上下文。 | 保留报告与研究会话隔离，不把完整研究历史再次塞入报告生成；登记短引文 ID 是本项目设计，不能声称上游已提供该注册表。 |
| [smolagents monitoring.py](https://github.com/huggingface/smolagents/blob/96f33faaf028479119ec8d34507b47694cf14e34/src/smolagents/monitoring.py) | TokenUsage、Timing、Monitor.update_metrics：逐步骤累计耗时及非空用量。 | 每次模型调用独立记录用途、归属、等待和执行耗时；供应商用量缺失时保留未知。额外增加 HTTP 请求体计量，补齐消息之外的工具和输出 Schema。 |
| [GPT Researcher costs.py](https://github.com/assafelovic/gpt-researcher/blob/5cdad9cb434754188b78bd998df18dd8d502cf7e/gpt_researcher/utils/costs.py) | 对输入/输出文本分词，并使用代码内价格常量估算费用。 | 采用明确区分计量口径的思路；不将文本估算当作供应商真实用量，不沿用其固定价格常量。 |

同时阅读本地已安装的 langchain_openai/chat_models/base.py 和 LangChain 摘要中间件：前者的规范化 usage_metadata 会为部分缺失维度补零，后者通过公开调用 metadata 的 lc_source=summarization 标记摘要调用。本轮据此读取原始 token_usage，识别摘要用途；不升级框架，不改动第四阶段的私有接口清理范围。

## 引文链路

主要位置：agent/runtime/quotes.py、facts.py、reporting.py、evidence.py、source_policy.py、delivery.py。

原先同一批原文分别出现在 tools、citation_options、输出 Schema 的枚举中；报告还要重新输出整段原文。现在：

1. 保留现有任务证据采集和原始结果保存链路。
2. 从真实、可消费的成功证据构造有预算的可见片段；网页正文优先，SQL 结果也参与同一上下文字符预算。
3. 服务端登记任务所属的不可变证据快照，以及快照内连续引文的起止位置。
4. 报告输入只有来源目录和一次候选引文文本；不再重复传整份 tools 正文。
5. 输出 Schema 只枚举短 quote_id；模型只选择 ID。
6. 程序按当前任务和实际证据展开原文，语义审核继续读取展开后的原文及事实，不以 ID 存在代替事实正确。

报告模型的引用字段统一为：

```json
{
  "evidence": [{"quote_id": "q1"}],
  "identity_evidence": [{"quote_id": "q2"}],
  "time_evidence": null
}
```

服务端规范报告将引用展开为 quote_id、source_id、quote。此处 source_id 是快照编号（如 s1）；origin_source_id 保存原来的网页目录 ID 或 SQL 执行 ID。研究工具的 read 动作仍使用原来的网页目录 ID，两个用途不会混用。

注册表保留 task_id、工具 call_id、attempt、缓存来源调用/attempt、来源目录 ID、正文摘要和原文位置。短 ID 在逻辑研究轮次内稳定；同一快照内的重复引文复用 ID，同一 URL 的不同响应版本或不同任务分别登记，不能由 URL 字典的最后一条记录覆盖旧引用。

跨任务或不存在的 ID、缺少对应真实证据、内容摘要/引文位置不一致的引用明确拒绝。共享检索缓存必须先通过原有 cache-consumption 校验，再在当前消费任务登记自己的引文；不能直接借用其他任务的 ID。

快照沿用既有持久化脱敏规则。可登记引文必须同时是实际工具结果和持久快照中的连续原文；包含系统脱敏占位符的片段不作为引文候选，不能把改写后的脱敏文字冒充原文。失效网页、失败工具响应和越界结果不因注册获得可用身份。

日期配对与日期原文校验、对象身份审核、来源层级/域名约束、冲突和过时事实处理继续生效。覆盖判断按具体快照检查来源；最终渲染再检查所用事实的全部引用。旧的模型输出 source_id+quote 协议及长原文 Schema 枚举已删除，不提供兼容解析。

运行目录内新增 quote-registry.json，保存来源快照和引文表；它属于本轮证据审计材料，不替代原始工具结果。

## 完整模型请求计量

主要位置：agent/runtime/measurement.py、models.py、agent/llm.py、agent/reflection.py、finalization.py、answer_review.py、api/static/app.js。

MAIN、SUB、REVIEW 的当前 OpenAI 兼容调用统一经过 BudgetChatModel。原先语义审核自行预留和自行观测的实现已删除；首次审核和既有的一次格式修正均使用统一调度、预算和计量入口，不重复扣同一模型调用。

模型入口登记逻辑调用；角色客户端的同步/异步公开 HTTP hooks 在 SDK 发送边界取得完整序列化请求体。SDK 重试保持 0，客户端不跟随重定向；同一逻辑请求出现第二次隐式发送会明确拒绝。封装器保持非流式调用，确保从完整响应读取供应商原始用量；未新增流式协议。

| 字段/记录 | 含义 |
|---|---|
| request_id、run_id、task_id、parent_call_id | 每次请求与执行/分工的关联。 |
| phase、category | 任务契约、协调、研究、报告生成/修订、摘要、终稿生成/修订、语义/终稿审核、审核格式修正。 |
| queue_wait_ms | 取得模型账户执行槽之前的等待；预算拒绝或取消仍留记录。 |
| elapsed_ms | 获准调用后的封装器请求耗时，含请求准备、审计和响应解析，不包括排队。 |
| request_bytes、request_chars | HTTP hook 取得的完整 JSON 请求体实际 UTF-8 字节数、序列化字符数；包含工具定义、输出 Schema、修订稿和其他设置。 |
| messages_bytes、tools_bytes、schema_bytes | 对各请求组成部分重新按 canonical UTF-8 JSON 编码后的大小；用于归因，不能相加冒充实际请求体大小。 |
| revision_draft_bytes | 报告/终稿修订输入的草稿，以及审核修正中的上一份判定；是 messages 的子项，不能再次加到总量。 |
| http_status、headers_ms | 收到的 HTTP 状态及等待响应头的耗时；不是首 Token 延迟。 |
| input_units、output_units、reasoning_units、cached_units | 供应商原始 token_usage 中实际返回的计数。未返回、无效或尚未取得的字段为 null。 |
| transport、status、error_type | 区分排队/拒绝、发送边界观测、收到响应、完成/失败/取消和中断的不确定状态。 |

完整请求体只在内存中计算大小。计量文件和事件不保存请求正文、模型思考、HTTP 头或 API 密钥，也不保存供应商原始用量对象，只选取明确的数值字段。

request_bytes 是应用 JSON 请求体大小，不包括 HTTP 头、TLS 或协议开销，也不是模型 Token 数。HTTP hook 证明已到 SDK 发送边界，不能证明供应商已计费。没有发送边界观测的排队/拒绝请求不计入 transport_attempts；超时或取消后没有用量回执时保持未知。

model-requests.json 保存每次逻辑请求及累计摘要：已观测请求体总字节、发送边界次数、已返回用量小计、缺失输入/输出用量的请求数。小计不代表完整账单，不使用分词估算或费用常量填补缺口。原有搜索 research-ledger.json 和 transport.jsonl 继续独立记录搜索/读取尝试，本轮不重写它们。

监控调用详情增加完整请求 KiB、排队秒数和输入/输出 Token。单项用量缺失时显示“未知”；详细事件保留各组成部分大小和用途。

## 检查点与恢复

- checkpoint_schema_version 为 14；REVIEW_CONTRACT_VERSION 为 review-v11-registered-quotes。
- 保存 evidence_snapshots、quote_registry、request_ledger，沿用来源策略摘要和原有预算/任务账本。
- 引文校验快照摘要、位置和归属；损坏时明确中断。
- 恢复时核对 model-requests.json 与检查点的逻辑研究范围、请求身份和已有记录；保留检查点更新前已落盘的较新观测，不凭空接纳未归属的发送。
- 中断的 queued/running 请求标为 interrupted；无完成回执的发送状态保留不确定，恢复不重置用量，不凭此自动重发。
- 旧协议检查点保留并中断，不自动迁移或静默降级。数据库 schema 仍为 6，本轮未启动服务或迁移运行库。

## 检查范围与限制

本轮执行 Ruff 格式化/静态检查、Python AST 解析、JavaScript 语法检查，以及 UTF-8/BOM、CRLF、尾随空白检查。具体结果见 21-static.log。

未修改测试、未执行测试或真实业务验收。移除重复正文和长引文 Schema 的代码路径已落地，但同证据条件下的请求规模/时延改善尚无本轮实测结果。旧协议测试夹具也没有在本轮迁移，不能将历史测试通过数作为本版本结论。

本轮未实施第四阶段 agent 注册/私有依赖清理，也未实施对外身份认证与资源归属方案。
