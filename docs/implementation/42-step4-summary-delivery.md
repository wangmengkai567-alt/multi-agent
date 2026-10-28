# 步骤 4：汇总与交付补修记录

日期：2026-10-28。项目根目录：D:/My_workspace/Multi-agent/Multi-agent。

依据 [37 号修复方案](37-basic-multi-agent-repair-plan.md) 的步骤 4 实施。本轮只补修汇总和交付边界，不进入步骤 5，不改写历史数据库，不执行版本管理操作，不新增或修改测试文件。

## 实际执行位置与 Codex 主要来源

计划中的 delivery.py、answer_review.py、finalization.py 已在前轮收敛中移除。当前主助手从完成的根图状态取得最终 AIMessage，HTTP 入口调用 agent/runtime/workflows.py 的 finalize_answer；独立运行入口也复用该函数。agent/runtime/completion.py 校验正文和文件字节，生成完成快照。本轮继续使用这一条交付路径，不重建旧的逐断言 ID、覆盖图、报告生成工具或审核链。

修改前，先通过 GitHub 连接器读取 openai/codex 固定提交 970111f6cb62cc02fe36a82ea7d71b37a2889c97 的相关源码，并搜索默认分支的 final_output、last_agent_message、limitations。主要采用以下机制：

| 具体问题 | 已读取的 Codex 来源与本项目采用方式 |
| --- | --- |
| 接受了请求或产生了文本，但实际调查没有有效结果 | [core/src/agent/status.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/agent/status.rs)：终态取自执行事件和错误，不能由正文声明。本项目据程序记录的成功分工与必需来源判断是否允许交付 |
| 成功分工掩盖被拒绝的分工 | [multi_agents_v2/wait.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/tools/handlers/multi_agents_v2/wait.rs)：区分工具调用结束、消息活动、用户输入和超时；结合 status.rs 的错误终态机制，把拒绝记录保留为未完成说明，不能凭某个成功返回认定整体完成 |
| 正文、限制说明和下载内容必须一致 | [exec/src/event_processor.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/exec/src/event_processor.rs)：直接将最后助手消息写入输出文件。继续采用一次整理最终正文、再保存同一正文的机制，不再要求模型另外生成报告 |
| 空正文被程序说明补成成功交付 | 同上 event_processor.rs 的最终消息作为文件内容的机制；本项目按自身明确验收要求，在追加说明前拒绝空正文，不采用 Codex 的无消息时写空文件分支 |

所读源码及搜索结果中没有本项目 required_sources、required_uploads、unfinished、limitations、coverage 和认证 Markdown 下载的现成业务适配器。这部分在上述 Codex 执行状态和直接交付机制下修改现有 Python 实现；未接入 Rust 内核、Codex SDK 或另建兼容路径，也不把本地业务规则描述为 Codex 原样实现。

## 复现与实际改动

修改前，隔离诊断直接调用生产 finalize_answer，四种情况均复现：必需搜索来源完全没有分工结果时仍生成 partial 文件；成功分工加被拒绝分工被标为 complete；读取限制丢失；空正文加未完成事项仍生成文件。原始生产文件快照保留在 output/verification/step4-summary-20261028/before/。

agent/runtime/workflows.py 的修改：

- 先验证模型原始最终正文非空，再追加程序说明；最终脱敏和换行整理之后继续验证正文。
- 必需来源、必需文件均纳入没有有效结果的失败判定。全部委派失败或被拒绝时继续明确失败；没有任何成功分工且必需资料未取得时，不生成 answer.md。
- 只要存在被拒绝分工，就在最终正文披露拒绝原因，并通过原有 coverage 字段记录 partial；已有成功分工结果保留。
- 将成功和失败分工实际返回的 limitations 都加入最终正文；原有 unfinished、失败分工、缺失来源、未读文件说明继续保留。
- 仍先校验最终正文引用 URL 是否来自本轮实际输入或成功分工工具结果，再将包含缺口和限制说明的同一正文保存为 UTF-8/CRLF Markdown；继续使用已有摘要、实际文件字节和认证下载校验。

agent/prompts.py 说明主助手直接依据实际返回的 status、result、sources、unfinished、limitations 汇总，保留来源位置、检索与正文读取的区别；执行完成不等于完整读取。没有恢复逐断言 ID 必填协议。

通用解析能力限制本身不自动等于本次任务未完成；是否存在实际遗漏仍由真实 unfinished、必需文件读取状态及来源要求决定。例如无公式的纯单元格表可在披露不支持公式重算等能力限制后完成；含未计算公式的材料则继续是 partial。简单对话没有分工、没有必需来源和必需文件时，继续允许直接正文交付。

## 验证记录

证据目录：[output/verification/step4-summary-20261028/](../../output/verification/step4-summary-20261028/)。本轮生产源码在上述修复后未继续修改，以下结果对应同一份最终源码。

- 四项隔离诊断修复后均通过：无必需来源结果和空正文拒绝交付；成功加拒绝为 partial；实际返回限制进入正文。
- 五项认证 HTTP 链路诊断全部通过，逐场景证据见 http-summary.json 及同名 JSON。使用真实迁移、认证、生产主助手构建器、框架 task、执行 hook、结果归档和下载入口。模型使用既有 ScriptedModel，搜索为可控响应；XLSX 使用真实上传、真实 document_read 工作进程和原生解析。每次运行终态和资源释放之后收集证据，没有读取正在写入的应用 JSON。
- 无必需来源结果：运行 d992f7be-1af6-4e52-9478-025594d1d874 失败，answer 为 null、无成功 artifact，下载 answer.md 返回 404。
- 成功搜索加未知助手拒绝：运行 97f0fc3e-6839-4ee3-bc5a-d08d072ce86a（见 rejected_with_success.json）；有真实搜索工具记录，正文保留来源和拒绝原因，completion.status 为 partial。
- 普通 XLSX：真实读取后将原生解析限制写入最终正文；含未计算公式的 XLSX：正文保留 Sheet!C2:formula_result_not_calculated、未完整读取文件说明和读取限制，completion.status 为 partial。
- 空模型正文加分工拒绝：运行 37b6275a-c3ac-4432-9766-693a25826be8 明确失败，answer 为 null、无成功 artifact，下载返回 404。
- 三个成功/部分交付场景的认证下载字节与保存的最终 answer 完全一致，均为 UTF-8/CRLF，SHA-256 与 completion.artifact.file_digest 相同。五个场景均检查运行任务结束和调度槽归零。
- 6 个现有测试文件共 164 项，首次 163 通过、1 失败，完整原始结果见 regression.xml。唯一失败是 test_basic_boundaries.py 的 test_actual_windows_worker_close_reaps_descendants：标记文件刚创建尚未写完，marker.read_text() 返回空字符串，int('') 抛 ValueError，尚未到达进程回收断言。生产进程实现和测试均未修改；只对该项隔离复核，1 项通过，见 windows-recheck.xml。首次失败仍保留，不能把首次回归描述为全通过，测试自身的标记文件读取竞态未在本轮修复。
- Ruff check 和 ruff format --check 对两个修改的生产文件通过。最终 4 个变更文件的 UTF-8、既有 BOM、CRLF 和变更行空白均经原始字节检查，见 source-audit.json。仅执行 git status；依照项目更严格的 Git 限制，未运行 git diff --check，使用原始快照和变更行检查代替。

验证程序自身有一次断言错误：把实际工作进程操作名 document_read 写成 document，导致普通 XLSX 的诊断在工具名称核对处停止。改正诊断预期后续验相关场景；未修改生产实现或测试文件，没有将该次中断计为通过。

本轮步骤 4 的目标链路和修复边界已验证；现有测试竞态及其首次失败如上保留。没有执行真实模型、真实 Tavily 或生产数据库业务题验收。

## 边界

不通过自然语言关键词推断用户的完整业务需求，也不声称程序能证明每个自然语言结论。来源白名单、任务归属、实际输入快照和原有资源保护继续生效。保留失败分工的成功工具记录，不将其冒充成功分工结论或终稿引用授权。

本轮不自动重跑原第 1 道真实业务题，不把可控模型与外部响应的离线链路验收称为真实模型或生产外部服务验收。步骤 4 的补修证据也不能替代 37 号方案全部步骤和完整真实业务验收。
