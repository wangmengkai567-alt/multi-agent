# 指定范围读取与全文覆盖分离修复

日期：2026-10-28。项目根目录：D:/My_workspace/Multi-agent/Multi-agent。

本轮修复第二个问题：只要求读取 Data!A1，工具已返回42且没有真实解析缺口，结果汇总仍因其他单元格和工作表未读而将任务标为 partial。起始工作区干净；用户已明确授权本轮相关回归测试修改。

## 实际读取的 Codex 实现

主要来源为 openai/codex 的固定提交 26ad281fd1ca712e4f0c7920a6273cf18c961b8a。通过 GitHub 连接器实际读取源码正文：

| 来源 | 源码行为 | 本项目采用方式 |
| --- | --- | --- |
| [exec-server/src/file_handle.rs](https://github.com/openai/codex/blob/26ad281fd1ca712e4f0c7920a6273cf18c961b8a/codex-rs/exec-server/src/file_handle.rs) | read_block 接收 offset/len，返回读取结果；FileReadBlock 单独携带 bytes 和 eof。read_block_at 成功返回时，eof 可以为 false，只有实际 I/O 等错误进入错误路径 | 将“选定范围读取成功”与“全文已覆盖”分开判定，不将未读到全文边界自动视为失败或未完成 |
| [exec-server/src/file_handle_tests.rs](https://github.com/openai/codex/blob/26ad281fd1ca712e4f0c7920a6273cf18c961b8a/codex-rs/exec-server/src/file_handle_tests.rs) | 使用真实临时文件和确定性协调验证读取、资源释放与重复打开；读取断言分别检查返回字节及 eof | 本项目回归使用真实生成的 XLSX/PDF、真实解析器和读取元数据。未将上游测试描述为已覆盖 eof=false 的专门回归 |
| [core/src/agent/status.rs](https://github.com/openai/codex/blob/26ad281fd1ca712e4f0c7920a6273cf18c961b8a/codex-rs/core/src/agent/status.rs) | TurnComplete 无错误投影为 Completed，有错误投影为 Errored；该投影未依赖文件 EOF | 保留执行状态及读取覆盖信息各自的职责，真实执行失败仍按原协议处理 |

还读取了同提交的 exec-server/src/server/file_system_handler.rs 和 exec-server/src/local_file_system.rs；其中 read_file 是全文读取委托，并非本轮范围读取机制来源。

未找到可直接复制的本项目 Python/XLSX/PDF 业务补丁。直接采用的是 Codex 的“读取成功与文件边界分离”机制，并在本项目已有 coverage、unfinished、limitations 和 required_uploads 协议中实现；没有引入 Codex SDK、Rust 运行时、兼容层或其他执行路径。

## 生产修改

只修改 agent/results.py 的 task_evidence：

- 继续按文件路径、摘要和解析器汇总真实 extents、read_ranges、complete 和 missing；跟踪是否执行过 supported_content 全文内容读取。
- 成功的 selection 读取存在实际范围、没有真实 missing、也没有全文读取约束时，不再仅因文件其他范围未读而产生 unfinished。
- 此时增加明确的读取限制：“本次仅分析工具返回的选定范围，未读取整个文件；范围及位置见来源。”限制传递给主助手并由现有交付逻辑写入最终答复和 answer.md。
- 全文读取、required_uploads 要求、只有目录没有内容、公式未计算、PDF 无文本等真实 missing 继续保留原有未完成判定。失败工具调用不能因另一个成功范围而消失。
- 同一子任务多次范围读取的并集覆盖全文时，仍使用原有矩形并集验证，不继续声称全文未读。

解析器与证据协议不变：选定范围的 coverage.complete 仍为 false，完整文件 extents 不缩小，实际来源只包含被读取位置。after_tool_workflow 仍只有在全文真实覆盖时登记 upload_reads，局部读取不伪造全文已读记录。输入快照、任务归属、证据完整性及下载内容一致性校验不变。

全文必读的机器约束沿用已有 required_uploads；本轮不增加自然语言关键词推断，也不宣称程序可以证明模型选择的范围满足任意自然语言任务。模型对范围选择负责，实际范围及限制继续对主助手和用户可见。

## 复现与验证

根因已在上一轮隔离审计定位，因此按 diagnosing-bugs 流程直接固定失败回归，没有重复泛化假设或添加临时日志。证据目录：[output/verification/document-scope-20261028/](../../output/verification/document-scope-20261028/)。before/ 保留修改前生产源码；upstream.json 保存本轮读取的主要 Codex 源码正文及固定链接。

- 修改前同一批17项用例：5失败、12通过（before.xml/log）。失败明确复现了单元格、工作表、PDF选页、目录后选区以及真实HTTP链路的 partial 误报。
- 修改后17项全部通过（after.xml/log）。随后在HTTP回归中补充落盘证据断言，纳入最终相关回归。
- 正向覆盖：选定单元格、整张工作表、PDF页；完整读取；分次范围并集覆盖全文；目录后读取选区。
- 保护边界：全文必读仍为 partial；公式未计算、无文本PDF页仍保留 missing；目录不作为内容来源；失败读取仍未完成；跨任务、跨运行及篡改证据继续拒绝；未知工作表、反向范围、页码越界与超过20000单元格继续显式报错，不返回静默预览。
- 认证HTTP回归使用真实临时数据库迁移、登录、调度、文档工作进程、解析器、委派、历史及下载。模型使用可控脚本，外部网络由既有测试环境禁止。校验落盘来源只含 Data!A1=42、选区/全文边界元数据仍准确、主助手可见范围限制，以及下载字节与最终答复、摘要和CRLF完全一致。

相关回归共182项，首轮181通过、1失败，无skip/xfail（regression.xml/log，433.21秒）。覆盖工具策略、三助手协作、取消与总时限、事件落库与回放、认证历史，以及输入快照、证据归属和失败资源释放。最终源码的17项新回归及前一次超时落库修复的3项回归均在其中通过，不与前述单独运行重复相加。

首轮失败是既有 test_total_deadline_covers_all_execution_stages[main_model]：在1.5秒总时限内尚未进入 reached 检查点，测试等待3秒后失败。其临时数据库记录为 timed_out，错误 Run has ended: timed_out；没有 before_run 或文档工具事件（deadline-initial-state.json）。本轮未修改该测试、计时阈值或服务器执行路径。

三个阶段 main_model/sub_model/retry 单独复跑全部通过（deadline-recheck.xml/log，25.02秒）。另用 before/ 中原始源码在独立进程内替换汇总函数作对照，三个阶段同样全部通过（deadline-baseline.xml、deadline-baseline-checked.log，24.71秒），探针确认 task_evidence 调用为0；此对照没有回写生产源码。结果表明该失败位于既有启动计时路径，未执行本次改动。首轮失败保留，不能将整组首轮结果写成182项全绿。

Ruff检查、格式检查通过（lint.log、format.log）；修改前后字节及AST审计确认生产代码仅 task_evidence 有变动（source-audit.json、production.patch）。验证汇总见 validation-summary.json。最终验证后未再修改生产或测试源码。

按项目限制只执行 git status，不执行 git diff --check 或其他 Git 操作；使用修改前字节快照、difflib 变更审查及变更行空白检查替代。最终格式、UTF-8、BOM与CRLF审计见证据目录。未改写历史或生产数据库，未修复第三个多浏览器连接问题。
