# 阶段三：⑥ 文档解析与定位、⑦ 需求版本及断言缺口

日期：2026-10-27。实施范围依据“完成顺序4修复”对话中四阶段计划的⑥⑦；此前工作区改动经用户确认后保留。没有恢复格式转换功能。

## Codex 参考与适配边界

优先阅读固定提交 970111f6cb62cc02fe36a82ea7d71b37a2889c97 的实际源码：

- [MCP 执行入口](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/tools/handlers/mcp.rs)：参考调用身份、执行绑定、结构化结果与展示限制分离。本项目继续使用受控 Python 工作进程和既有证据适配器；没有接入 Codex MCP 服务或 Rust 私有模块。
- [计划协议](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/protocol/src/plan_tool.rs)：其 pending/in_progress/completed 仅表示计划步骤，不能代替本项目的事实审核与需求覆盖。

⑥的具体解析使用已锁定的 openpyxl、pypdf、python-docx，并新增 xlrd==2.0.2；同步更新 requirements.txt 与 Windows 开发锁文件，未升级其他依赖。⑦属于本项目业务实现，不声称 Codex 已有可直接复制的需求—事实覆盖模型。

解析依据：[openpyxl 文档](https://openpyxl.readthedocs.io/en/stable/tutorial.html)、[pypdf 文本提取说明](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)、[python-docx 文档结构接口](https://python-docx.readthedocs.io/en/latest/api/document.html)。实际执行使用当前环境锁定版本。

## ⑥ 已实施的支持范围

主要位置：tools/document_parser.py、tools/upload_file_read_tool.py、agent/runtime/worker.py、evidence_protocol.py、preflight.py、workflows.py、document-analysis Skill。

| 格式 | 可读取内容及定位 | 明确保留的限制 |
| --- | --- | --- |
| XLSX | 默认遍历全部工作表，保留工作表名称、真实行号、单元格地址、原始类型及值；可指定 sheet/cell_range | 不计算公式，不运行宏、外部链接或跨表运算；公式结果、绘图等未处理内容明确列为缺口 |
| XLS | xlrd 读取工作表存储值及单元格位置 | 不把存储值称为独立重算；无法完整保留公式来源，因此标明读取限制，不作完整读取承诺 |
| PDF | 按真实页码提取文本层，可指定 page_start/page_end | 没有 OCR、图片识别或表格结构重建；空文本页、图像/表单资源保留缺口 |
| DOCX | 按正文顺序读取段落、顶层和嵌套表格，合并单元格只读一次；保留结构路径及行列位置 | 不编造页码；图片、文本框、页眉页脚、修订、批注、脚注及嵌入对象等未处理内容保留限制 |
| UTF-8 文本 | 保留原始正文及行定位 | 不执行文内指令；不提供旧 .doc 或图片解析 |

read_document 的 selection=null 读取有界完整受支持内容；selection.mode=catalog 只返回目录；selection.mode=content 可带精确工作表/单元格范围或 PDF 页范围。未知选择、反向范围、越界页码及不匹配格式明确拒绝。

每次最多读取 20000 个选中单元格、200000 个正文字符；超过限制明确拒绝并提示先看目录、再选范围，不再返回前五行或首表冒充完整内容。完整 XLSX 扫描不信任文件声明的 dimensions，按实际行扫描。目录结果、单次局部读取、公式未计算或未处理结构不会直接被记为完整上传读取；同一字节摘要的分页/单元格范围累计覆盖全部内容且无缺口时，才记为完整读取；此前已完整读取且摘要未变的同一文件不会被后续局部读取抹去记录。

输出包含严格的 extraction coverage、limitations、missing 与 catalog；这些只描述解析范围，不代替逐事实语义审核或业务需求覆盖。原始字节摘要、来源位置、引文登记和证据版本仍沿原有单一路径进入审核。

OCR 没有得到额外业务范围确认，本轮按上述原生文本与表格范围实施。没有增加自动 OCR、解析引擎切换、任意代码执行、跨表统计工具或格式转换。

## ⑦ 已实施

### 用户变更入口与应用时序

新增 GET/POST /api/runs/{run_id}/requirements，沿用当前登录、Origin 与 CSRF 校验。支持运行中的前台上下文和后台研究上下文；没有把该入口注册为模型工具，也没有在当前页面增加需求编辑控件。

POST 使用严格的 RequirementChange：expected_version、instruction、retain、withdraw、additions、reason。retain 与 withdraw 必须恰好划分当前全部必需 rid；模型不能自行增加、撤回或推进版本。新增项绑定已有任务/对象或明确的 main 义务，仍需通过逐字来源及条件校验。空目标要求取消，不能靠全部撤回变成成功。

接受后先保存 pending，在下一次主助手推理边界应用；专家执行中不改变它正在使用的需求集合。如果旧推理返回前收到更新，旧响应在工具执行或最终审核前丢弃，重新以当前需求组装请求。额外推理仍消耗原有调用额度。

该接口修订当前证据任务的覆盖义务，不重新分类任务，也不改变材料、联网权限、来源级别、输出格式或安全策略。来源域变化明确拒绝；需要改变这些执行条件时必须建立新任务契约。

### 版本、预算与覆盖

- query_version 单调推进，保留各版本的用户指令、需求、覆盖、任务身份与撤回原因；新增当前行关联原 rid，旧行不覆盖。
- 当前需求重新登记后全部从 unknown 开始；旧审核缓存和交付草稿失效。旧事实可继续保留为事实依据，不能沿用其旧覆盖结论。
- 已消耗的模型/工具/搜索/读取/重试额度、传输记录、原证据、分支预留、原截止时间和来源缓存均不重置。
- 已结束任务不能重新领取预算。计划可为新需求重新开放，但保留已执行任务、累计对象额度及追加上限；已撤回任务不再阻塞 seal 或占用待执行分支保底额度。
- 新指令中的未承接语义继续登记 unknown，不因撤回或缺少需求行而利用空集合判 complete。

### 逐断言缺口与 dropped

审核模型完成合法的逐事实或逐终稿断言判定后，程序建立 claim gap：保存原始断言、断言版本、来源事实及其快照、review_key、任务/rid 关联和拒绝原因。审核未完成或技术失败不会冒充有效拒绝。未提供精确 rid 的判定保守关联当前审核范围，并明确标记 association=review_scope，不把关联范围当作已证明的支持关系。

同一版本断言重复审核不重复造 gap；实际审核通过时记录 claim_resolved，删除或替换断言时记录 superseded，用户变更需求时记录 requirement_changed。只有这三种程序可追溯原因才能进入 dropped，且保留原状态、版本和详细原因；dropped 不计作 covered。

需求支持与断言缺口分别重算：拒绝一条事实不删除同一需求仍然有效的其他支持。终稿错误断言的缺口保持为编辑/审核义务，不开放新的外发研究动作；来源和预算约束继续生效。审核缓存命中同样更新断言生命周期，避免复用通过判定后遗留开放缺口。

### 当前版本恢复

检查点协议为 26，保存需求历史、pending 更新、完整上传摘要及文档范围累计进度。只处理当前协议，没有添加旧版本迁移或旧任务续跑路径。

后台恢复对照初始任务载荷，允许已审计用户修订的 instruction/operation，但其他契约字段必须一致。持久结果仍证明它自己的事实；历史版本覆盖投影为 unknown，不能与新需求混用。按版本保存的 task identity 用于核对权威结果，原预算、预留和传输扣账保持不变。

## 验证记录

没有新增或修改测试文件。定向检查通过终端 stdin、内存数据、临时目录、真实 Windows 工作进程和 ASGI 入口执行；未调用真实 Qwen、Tavily、MySQL 或业务验收题。

共 59 项定向检查通过：

| 分组 | 数量 | 内容 |
| --- | ---: | --- |
| 真实解析器 | 15 | 多表、单元格、局部读取、目录、公式限制、非法选择、Word 顺序和嵌套表、页眉缺口、PDF 页码及空页、旧格式拒绝、超限拒绝 |
| 需求与缺口 | 15 | pending 边界、版本历史、预算不刷新、旧覆盖失效、撤回审计、陈旧版本/不完整划分拒绝、claim 绑定和去重、保留其他支持、resolved/superseded、当前协议检查点往返、空覆盖非 complete |
| 真实工具/工作进程 | 7 | 同步全工作表、规范证据定位、完整读取记录、保留先前完整读取、局部读取非完整、异步目录非事实证据、未绑定输入拒绝 |
| 真实中间件与 API 边界 | 8 | 同步/异步丢弃旧响应、新指令进入组装请求、登录和 CSRF 拦截、受理 pending、审计查询、重复 pending 拒绝 |
| 真实审核协议 | 5 | 通过实际 _adjudicate 创建/关闭 claim gap、缓存拒绝/通过更新生命周期、终稿断言保留真实支持事实身份及快照 |
| 补充边界与恢复 | 8 | 错误 dimensions 不漏真实单元格、巨大范围预先拒绝、重开计划不刷新预算、新操作目标、初始后台载荷恢复用户修订、历史事实/覆盖分离、权限变化拒绝、后台实时上下文定位 |
| 真实 BIFF 输入 | 1 | 在内存构造二进制 XLS，xlrd 读取数值单元格及行定位，并如实标明公式来源/重算限制 |

回归结果分别记录，不与定向检查相加冒充全套通过：

- tests/test_fact_reports.py、test_review_contract.py、test_answer_review.py、test_subagent_review.py：31 失败、11 通过、5 个夹具错误。使用本轮开始保存的相关模块源码，在独立进程中以导入器加载作修改前对照；失败集合及数量相同。主要在旧 source_id+quote、旧报告/计量契约处失败；没有修改断言或加入兼容解析。
- tests/test_llm.py、test_framework_contract.py、test_call_budget.py：61 通过、1 失败。失败为 test_actual_task_model_tool_and_total_counts 中自定义工具未执行的既有断言，与上一阶段记录一致。

静态检查涵盖本轮 20 个 Python 文件的 AST、Ruff 检查和格式检查；相关代码、依赖、Skill 及本记录检查 UTF-8、CRLF、尾随空白和冲突标记。项目只允许 git status，因此不执行 git diff --check，使用文件字节检查核对空白与换行。

## 交付限制

- 尚未进行真实模型业务验收；上述检查不证明真实模型可完成任意复杂材料分析。
- 解析不等于跨表关联、分组计算、公式重算、PDF 表格重建或 OCR；能力不足时仍应披露缺口。
- 当前协议恢复验证覆盖状态与预算一致性，没有重跑旧版本任务。
- 现有回归仍未全通过，⑧的失败归因/验证基础治理未在本阶段全部解决。
- 本轮未修改用户数据库，没有执行 git status 以外的 Git 操作；提交和版本管理继续由用户处理。
- docs/ 仍被既有规则忽略，本记录保留在本地；⑨的忽略规则治理没有提前混入本阶段。


## 复查后的三项修复（2026-10-27）

本轮仅修复已复现的执行问题，未增加 OCR、公式重算或复杂需求编辑功能。

1. 补充研究任务统一关联当前版本、同对象或其发现祖先对象的原始必需义务，以及无指定对象的全局义务。原来只允许 main 义务，导致“仅有任务事实义务，任务已结束后用户增加续航”等情况无法追加研究。现在支持这种追加；支持行仍为 required=false，保留 parent_rid，不新增完成门槛，不重开旧任务或刷新对象累计预算，也不把其他对象的义务复制进来。
2. PDF 页和 Excel 单元格范围按文件路径、原始字节摘要、解析器及真实内容范围累计。目录和重复区域不能冒充全读；所有区域均覆盖且没有 missing 才满足必需上传读取。XLSX 范围从 XML 中实际单元格坐标提取，兼容标准允许省略坐标的连续单元格，不信任生产工具的 dimension 提示。范围合并按矩形边界计算，不枚举巨大空白区域。文档进度与完整读取摘要随检查点保存；文件变动不能沿用原摘要的读取结果。
3. xlrd 的 logfile 显式设为 stderr，避免旧 BIFF XLS 的诊断输出破坏工作进程 stdout JSON 协议；父进程仍严格拒绝非协议输出，没有加入跳过坏行的兼容路径。

Codex 参考：

- [followup_task.rs（970111f）](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/tools/handlers/multi_agents_v2/followup_task.rs)：后续工作通过同一目标身份触发；本项目借鉴身份延续原则，仍使用既有追加计划及对象预算机制，没有移植 Rust 实现或声称它提供业务覆盖模型。
- [stdio_server_launcher.rs（2cab11c）](https://github.com/openai/codex/blob/2cab11ca40aad193382348a7bda46da2fc47596a/codex-rs/rmcp-client/src/stdio_server_launcher.rs)：标准输入/输出承载协议，stderr 单独处理诊断；本项目据此约束 xlrd 输出。文档分页和表格范围累计是本项目实现。

本轮验证没有新增或修改测试文件。通过终端 stdin、内存文件和临时目录完成：真实用户需求更新→追加计划→领取任务→共享已用搜索/网页预算；对象范围及祖先义务；真实 PDF 分页及空白页缺口；Excel 多表/多区域、重复区域、公式缺口、错误 dimension 及省略单元格坐标；绑定材料的同步/异步工具与证据适配器；检查点往返及恢复后继续补齐剩余区域；旧摘要拒绝；DOCX 页眉及 UTF-8 读取；PYTHONUNBUFFERED=1 下真实 BIFF2 XLS 工作进程。矩形覆盖算法另以 3000 组区域组合对照逐单元格枚举，结果一致。

现有 tests/test_llm.py、test_framework_contract.py、test_call_budget.py：61 通过、1 失败。使用本轮修改前保存的 7 个模块源码，在独立进程通过导入器加载并执行相同用例，仍为 61 通过、1 失败，失败同为 test_actual_task_model_tool_and_total_counts。没有修改测试或添加旧契约兼容路径来消除该失败。

协议 26 包含新的恢复字段，不自动恢复协议 25 的检查点。未调用真实 Qwen、Tavily、MySQL 或业务验收题；这些离线结果不能当作真实业务全链路验收通过。既有回归问题仍见上文，不宣称全套回归通过。
