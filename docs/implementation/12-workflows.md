# 第 12 步：运营分析流程的服务端前置条件

日期：2026-10-16（Asia/Shanghai）。状态：第 12 步完成，离线验收通过。

## 工作区与约定

实施目录：D:\My_workspace\Multi-agent\Multi-agent。修改前 git status --short 输出为空。遵守上层 AGENTS.md，只执行 git status，未执行其他 Git 命令，未暂存或提交。项目 .editorconfig 及 AGENTS.md 规定 UTF-8 / CRLF；已有文件保留 BOM，新文件无 BOM。没有 .gitattributes 或下层 AGENTS.md 影响本次源文件。Git 配置查询、git ls-files、git diff --check 因项目限制未执行，以文件字节、BOM、新增行空白和文件哈希审计替代。

## API 模式与实际启用条件

POST /api/task 新增 business_mode，默认 ordinary；普通任务继续自由选择助手。只选择 ecommerce-analysis Skill 不会自动开启硬性顺序。

显式内外对照请求：

~~~json
{
  "query": "对照本月内部经营数据与外部市场资料",
  "business_mode": "internal_external",
  "skill_ids": ["ecommerce-analysis"]
}
~~~

internal_external 必须同时显式选择 ecommerce-analysis；无效模式或遗漏 Skill 返回 422，在创建运行记录前拒绝。服务端在 before_run 初始化本轮步骤状态；自建执行器的真实执行入口也能初始化。状态绑定 RunContext，不从自然语言猜测流程，不继承其他会话或旧轮次的标记。

可选 required_sources 指定还需要的查询来源（db / search / knowledge），required_uploads 指定必须读取的上传文件名。两者均检查类型、重复值与允许范围；上传要求只接受合法文件名，不允许路径。普通任务默认两者为空，不强制全部查询；显式设置时，只核对所要求的来源和文件。内外对照固定要求 db / search，并与显式 required_sources 合并；存在本轮上传文件时也要求实际读取。

例如普通报告只要求外部资料和指定文件：

~~~json
{
  "query": "根据用户材料和公开资料生成报告",
  "business_mode": "ordinary",
  "required_sources": ["search"],
  "required_uploads": ["brief.txt"]
}
~~~

文件仍需通过既有上传入口提供，并由主运行器复制到本轮只读 uploads/。普通任务不因上述报告要求而被强制先查询数据库。

## 执行链路与可追溯依据

新增 agent/runtime/workflows.py，使用固定 Hook 和现有 ResultService / RunStore，不增加模型调度循环、依赖包、数据库表或迁移。步骤状态和决策通过统一 workflow_state 事件持久保存；依赖失败正文、检查结果及原文引用继续由第 09、10 步的 results / subtasks 保存。

主模型提出 task → 原 before_tool 校验与预算预留 → 委派记录 queued → 在所属事件循环查询本轮持久结果 → before_delegate 检查 → 执行专家。异步入口直接 await；同步框架执行器通过既有线程桥接访问 EventSink 所属循环，不在线程中另开数据库连接。

首次搜索前读取 SQLite 中当前 run_id / thread_id 的合格结果，要求数据库来源、published、passed、对应子任务 completed / passed，并校验原文 SHA-256。模型任务说明中写“已查询”、伪造 result_id 或篡改内存步骤状态均不能满足要求。数据库仍未就绪、不合格、未检查、检查未完成、工具失败、预算耗尽或跨轮结果都不能替代本轮合格数据库依据。

门禁批准的数据库 result_id 由服务端保存在当前调用的工作数据中；ResultService.search_input 再从数据库读取并仅注入批准的数据库摘要。持久 result_handoff 关联消费方 call_id 与实际 result_ids。报告和最终结果还会核对搜索是否真实关联当前有效数据库结果，不能把先前独立搜索追认成已完成的内外对照。

拒绝通过 HookRejected 返回结构化 error、code 和 next_step，明确说明先调用 db 并等检查通过。真实搜索执行器和业务函数尚未启动。拒绝发生在主 task 已预留调用预算之后，重复越序请求继续消耗 tool、main_model 和整轮总预算，最终在预算耗尽时停止；不重复扣委派次数。

## 数据库不可用与降级交付

仅当本轮已有持久的 dependency_unavailable 数据库结果、所属子任务已结束、没有仍运行的数据库任务且无合格数据库结果时，才允许搜索进入明确降级。内容不合格、检查失败、预算耗尽不会自动伪装成“数据库依赖不可用”。

降级搜索任务实际收到“本轮没有可用内部指标”的说明及数据库失败 result_id，result_handoff 保存 degraded 与 db_failure_ids。没有存储时流程直接拒绝，不进行不可追溯的查询。

降级报告正文和最终答复由服务端构造：固定降级声明、已通过检查的外部检索材料引用块、外部结果 ID 与数据库失败记录 ID。主模型自行生成的内部指标不被直接交付。材料明确标为外部助手返回、未完成内部数据对照，不将关键词相似当成内部事实证明。外部内容的事实准确性仍依赖原查询与检查，不声称服务端验证了每句自然语言。

降级文件只能重新生成，禁止通过 edit_file 恢复可能含内部指标的旧正文；PDF 转换前必须存在本轮受控生成且 SHA-256 未变化的降级 Markdown。执行结束时运行记录仍使用既有 completed 终态，另保存 completed_degraded 流程状态及明确降级答案，供后续前端步骤区分完整与降级交付。

若之后数据库重新查询成功，最终报告会要求搜索重新关联合格数据库结果，不能沿用此前仅外部材料的降级关联。

## 报告与用户文件前置条件

Hook 覆盖 generate_markdown、convert_md_to_pdf 及框架 write_file / edit_file。显式流程或要求存在时，所有这些写入入口均按产出检查，避免通过框架默认文件工具绕开。普通且无显式要求的任务保持原行为。

报告前重新读取持久状态，核对所需来源及内外对照关联；同时校验上传文件的真实读取证明。read_file / read_file_content 只有在实际执行成功且返回可用内容后才记录证明；模型口述已读不计。文本分页读取覆盖全部行才计完成；文件 SHA-256 绑定读取证明，文件变化后必须重读。错误、空结果、越界路径或未读取的上传文件不能满足要求。这里验证读取及来源状态，不声称证明了模型已理解文件的全部含义。

同样的检查接在 API 的最终答复出口；主模型提前结束而前置条件未满足时，运行保存 failed 和具体原因，不把其正文保存成成功答案。独立运行入口也接入最终检查。Skill、路径、SQL 只读策略与次数限制持续生效。

## 验证记录

测试使用项目 .venv/Scripts/python.exe，完全离线；ScriptedModel、实际框架 task / tools、临时 SQLite 与独立输出目录，不读取真实密钥或触发外部付费服务。延用此前缓存目录权限问题的处理：禁用 pytest cacheprovider，并为各次检查提供 output/ 下独立 --basetemp，不修改系统目录权限。

- 原有结果传递及生产构建冒烟回归：18 passed in 67.58s (0:01:07)。
- 首轮流程专项：30 passed in 128.65s (0:02:08)。
- 补齐降级 PDF、无持久存储与总预算限制：4 passed in 16.40s。

最终命令与实际输出（使用项目 .venv/Scripts/python.exe -X utf8）：

- python -m pytest -q -p no:cacheprovider --basetemp=output/test-step12-full-01 → 335 passed in 550.32s (0:09:10)，见 12-tests.log。
- python -m pytest tests/test_workflow_order.py -q -p no:cacheprovider --basetemp=output/test-step12-final-targeted → 33 passed in 219.59s (0:03:39)，见 12-workflows.log。
- python -m ruff check（本步 9 个 Python 文件）→ All checks passed!，见 12-lint.log。
- python -m ruff format --check（同 9 个文件）→ 9 files already formatted，见 12-format.log。
- python -m pip check → No broken requirements found.，见 12-dependencies.log。
- 实际环境：Python 3.11.15、deepagents 0.4.3、langchain 1.2.10、langgraph 1.0.9、langgraph-checkpoint-sqlite 3.0.3、aiosqlite 0.22.1、pytest 8.3.5。未安装或升级依赖，见 12-environment.log。
- 全部交付文本、日志的 UTF-8、CRLF、BOM、末尾空白和文件哈希见 12-text-audit.log。

专项覆盖同步 / 异步真实框架越序拦截、合格数据库摘要传递、普通搜索、各类不合格结果、依赖失败与降级、四种文件入口、实际文件生成、上传分页读取与变更失效、并发请求先后、重复拒绝预算耗尽、未关联搜索、原文篡改、两轮与两会话隔离、API 参数校验和最终输出拒绝。最终命令和实际输出保存在同目录 12-*.log。

真实模型与 MySQL / Tavily / RAGFlow 服务联调：未执行。本步不实现第 13 步的总超时、取消接口或事件补发，也未提前开展第 14 步前端联调。
