# 第14步：前端联调与首版功能验收

日期：2026-10-17（Asia/Shanghai）。第14步实现与首版功能验收完成。本记录区分可控集成验收、真实模型联调与外部服务限制；不将缺配置的服务标为成功。

## 工作区与修改范围

修改前上层 git status --short 为 A AGENTS.md、AM Multi-agent，内层工作区干净。保留用户暂存内容。遵守 AGENTS.md，只执行 git status，没有执行提交、暂存或其他 Git 操作。由于同一限制，没有运行 git config、git ls-files 或 git diff --check；改用文件字节、行尾空白、UTF-8 解码、BOM 与 SHA-256 审计。项目 .editorconfig 要求 CRLF；本次文本保留 UTF-8 无 BOM，全部交付文本（含测试与日志）逐一检查。

前端从 index.html 的内联脚本移至 api/static/app.js，复用第13步的 RunEventStream：

- 浏览器保存 thread_id、run_id、问题和选中项；刷新后通过 history 读取服务端完整答案、事件和终态。事件按 run_id/event_seq 去重和排序；连接从已读取的连续序号继续补发。
- 提供普通/内外对照模式、两个 Skill 选项、新会话/已有会话选择。内外对照自动选择运营分析 Skill，实际前置条件仍由后端检查。
- 展示 Skill 正文与资源读取、助手执行、工具返回、检查、补查、结果传递与失败原因；技术 ID 和原始数据置于展开详情。
- 区分 queued、running、completed、failed、cancelled、timed_out、interrupted。存在降级、未通过检查或记录不完整时，将已结束结果显示为部分完成，保留原始服务端终态。取消/超时且已有子任务结果时明确提示部分结果仍可追溯。
- 取消接口按本轮 run_id 调用；以接口返回的终态处理取消/完成竞争。409 显示该会话正在运行的轮次；422/网络失败保留输入。移除视图不删除历史或取消执行。
- 文件查询限定当前 session/run，异步切换视图后丢弃旧文件响应；修复旧前端对下载路径二次编码的问题。

## 真实联调发现并修复的问题

1. 首轮 Skill 文件生成失败：提示词暴露物理目录前缀，工具 schema 又声称 path 为绝对路径，模型将前缀重复拼接，最终触发 Windows 路径失败。main_agent.py 现在只说明本轮虚拟根和相对子目录，generate_markdown 的 schema 与之保持一致。未放宽路径规则。Markdown 正文显式规范为 UTF-8/CRLF，避免模型内容带 LF 或混合换行。
2. asyncio.timeout 产生的 TimeoutError 以 CancelledError 为 cause，子任务检查仅按 cause 分类，错误地显示“工具失败”且原因空白。现在先分类顶层异常，再在必要时检查 cause；空原因保留异常类型。回归证明模型超时归为 dependency_unavailable，不触发内容补查。
3. 真实检查模型曾返回 passed，同时填写 issues 和 revision_instruction，被原有一致性校验拒绝。直接复核日志 [14-review-diagnostic.log](14-review-diagnostic.log) 保留原始返回与 ValidationError。现已在系统说明中明确通过时三个字段必须为空，并提供合法 JSON 示例；未放宽解析与一致性校验。检查失败还会记录异常类型，区分超时与结构错误。
4. 当前模型多次触发单请求 30 秒上限。config/runtime.yml 的 request_seconds 调至 60.0，总时限仍为 300.0 秒，SQL/搜索各自超时及预算保持原配置。旧配置的执行记录保留在 14-live-request30.log；新配置重新联调，取消/超时/策略专项使用新配置重新执行。
5. 检查实际 checkpoint 后发现，主模型把完整说明放在额外的 prompt 字段，description 只有一句摘要。已核对已安装 Deep Agents 0.4.3 的 task/atask 签名：模型只应传 description 和 subagent_type；框架忽略了额外字段。现通过工具执行前策略拒绝未知参数，并在常驻提示中明确全部约束放入 description，禁止静默丢失任务。同步/异步真实 task 路由回归证明拒绝时子助手调用数为零，修正后完整说明原样到达。真实错误请求见 [14-routing-diagnostic.log](14-routing-diagnostic.log)，专项 159 passed，见 [14-task-arguments.log](14-task-arguments.log)。
6. 补查指令受既有脱敏规则保护，可能为 [REDACTED]。前端改用公开的 issues 显示补查原因，不修改脱敏规则；生产脚本测试覆盖此情况。
7. 真实搜索服务把响应 query 标准化为 search，检查器缺少实际请求参数，误把标准化回显当作未执行 site: 查询。工具证据现在单独记录搜索 request_parameters，保留原始响应 content；SQL 仍独立使用 query_reference，搜索词不会写入 executed_sql。检查说明区分请求与响应，真实来源 URL 的一致性规则未放宽。新增真实框架回归让服务回显 search、实际请求 site:docs.tavily.com search，证明两份证据分别传入检查模型。

两项回归见 tests/test_acceptance14.py。首轮原始失败记录保留在 [14-live-initial.log](14-live-initial.log)，不会用后续通过输出覆盖。

一次完整回归为 361 passed、1 failed：test_next_round_keeps_previous_answer_and_files 的旧断言要求模型提示包含物理 run 目录。现在通过实际 RunContext 目录名、上一轮文件与上传快照检查隔离，提示只检查虚拟根与 uploads 相对路径；保留原测试的跨轮数据保护断言。失败原文见 [14-tests-before-assertion-update.log](14-tests-before-assertion-update.log)。

## 验收场景与证据

可控集成测试使用真实 Deep Agents task 路由、Hook、工具包装、SQLite 和 HTTP/WebSocket；外部模型、搜索与数据库响应由测试控制，因此表中“可控通过”不代表真实外部服务已成功。

| 首版场景 | 匹配当前实现的验证入口 | 真实联调范围 |
|---|---|---|
| 显式选择 Skill | test_skill_loading.py 验证正确正文、哈希、资源与真实框架读取；前端 app.test.cjs 验证请求参数与读取显示 | 报告写作真实模型、正文/参考资源事件、生成文件与下载；见 live 日志 |
| 普通查询 | test_workflow_order.py 的 ordinary 场景，无数据库顺序限制 | 真实模型普通查询 completed，未强制加载 Skill |
| 子 Agent 调用 | test_delegation.py、test_run_history.py：task 请求、业务执行、工具、检查、返回及 call_id 关联 | 实际 search 助手检索、检查通过、返回主助手及整轮完成均已确认；db 依赖失败明确保存 |
| 内外对照 | test_result_handoff.py、test_workflow_order.py：保存的本轮 result_id 与搜索实际输入相匹配 | 本地缺独立只读账号，仅验收数据库不可用后的显式降级 |
| 越序调用 | test_out_of_order_search_has_zero_executions_then_db_handoff_unblocks_report，包含同步/异步 | 可控业务搜索函数调用数为零；未冒充真实数据库成功 |
| 信息不完整 | test_subagent_review.py：缺来源、空结果、补查指令和至多一次重试 | 可控路径；真实服务失败不冒充内容不足 |
| 依赖不可用 | test_subagent_review.py、test_workflow_order.py、test_acceptance14.py：不可用原因、降级与无捏造指标 | MySQL 无只读配置；RAGFlow 为模板地址；搜索真实返回与模型超时均留痕 |
| SQL、文件越界 | test_tool_policies.py：非只读 SQL、越界工具/上传/静态/下载访问在业务前拒绝 | 可控函数计数为零；本次未使用管理员账号探测真实数据库 |
| 两会话并发 | test_run_concurrency.py、test_result_handoff.py、test_run_history.py | 浏览器任务与 HTTP 模型联调并行，使用不同 run/thread |
| 同会话重复提交 | test_run_concurrency.py：409 和当前 run_id；app.test.cjs 验证忙碌处理 | 浏览器重复提交显示忙碌，保留输入与原轮次 |
| 次数耗尽、超时、取消 | test_call_budget.py、test_run_cancellation.py：新动作拒绝、晚到结果与终态保护；前端各状态分别验证 | 页面实际取消进入 cancelled，未显示成功 |
| 刷新与重连 | test_event_replay.py、test_run_history.py、run-events.test.cjs、app.test.cjs | 页面刷新恢复完整答案及 Skill 正文/资源读取事件 |

## 检查命令与输出

| 检查 | 实际结果 | 原始记录 |
|---|---|---|
| task 参数修复后的全套离线回归 | 364 passed in 582.77s（0:09:42），退出码 0 | [14-tests-final.log](14-tests-final.log) |
| 最终搜索请求/响应证据修复专项 | 78 passed in 210.78s，含新增回归，退出码 0 | [14-search-evidence.log](14-search-evidence.log) |
| task 参数、工具策略与流程专项 | 159 passed in 166.80s，退出码 0 | [14-task-arguments.log](14-task-arguments.log) |
| 调整请求时限后的可靠性/策略/并发专项 | 128 passed in 62.78s，退出码 0 | [14-reliability-final.log](14-reliability-final.log) |
| 生产前端应用脚本 | PASS，退出码 0 | [14-frontend-app.log](14-frontend-app.log) |
| 生产事件流脚本 | PASS，退出码 0 | [14-frontend-events.log](14-frontend-events.log) |
| JavaScript 语法 | 通过，退出码 0 | [14-js-syntax.log](14-js-syntax.log) |
| Ruff | All checks passed，10 个 Python 文件 | [14-lint.log](14-lint.log) |
| Ruff 格式 | 10 files already formatted | [14-format.log](14-format.log) |
| 依赖检查 | No broken requirements found | [14-dependencies.log](14-dependencies.log) |

全套命令：.venv/Scripts/python.exe -m pytest -q --tb=short -p no:cacheprovider --basetemp=output/pytest-step14-final-contract。此后补齐搜索请求参数证据，未重复宣称全套在该增补后重新执行；对其影响的检查、结果传递、业务流程与联调回归执行上述78项专项。独立联调脚本不被 pytest 收集，其验收输入与脱敏导出另经真实 CLI 和 Ruff 验证。前端在最终源码上运行 Node，覆盖乱序旧生命周期不能覆盖终态、工具失败后的部分完成、脱敏补查原因及恢复状态转换。全部交付文本的 UTF-8、BOM、CRLF、行尾空白与 SHA-256 见 [14-text-audit.log](14-text-audit.log)。

已完成路径专项：12 passed in 80.12s，见 [14-focused.log](14-focused.log)；此轮发生在新增超时回归之前，超时回归由最终全套运行覆盖。

新配置的超时、取消、策略、两项联调修复与并发专项：128 passed in 62.78s，见 [14-reliability-final.log](14-reliability-final.log)。

## 真实模型、环境与依赖

模型：项目 .env 配置的 mimo-v2.6-flash，使用原有 OpenAI 兼容接入和模型/工具包装，未替换成可控模型。单请求时限由30调为60秒，总时限、预算、SQL与路径权限未放宽。Python、操作系统及完整包版本见 [14-environment.log](14-environment.log)。核心版本：deepagents 0.4.3、LangChain 1.2.10、LangGraph 1.0.9、SQLite checkpoint 扩展 3.0.3、FastAPI 0.129.2。

人工显式运行 tests/manual/acceptance14.py，不会被 pytest 收集；服务只监听 127.0.0.1:8765，runs/checkpoints 和输出隔离在 output/step14-live。没有打开生产 data/runs.db 或 data/checkpoints.db，没有修改 .env。联调仅使用合成订单笔数与公开网络查询。

当前 MySQL 未配置 MYSQL_READONLY_USER / MYSQL_READONLY_PASSWORD；不允许回退使用已有管理员字段。RAGFlow 实际只读 list_chats 请求返回 502，地址仍为 your-ragflow-server-ip；见 [14-ragflow.log](14-ragflow.log)。因此“真实数据库成功查询并交给搜索”和“真实企业知识问答”未执行成功，不属于已验证项。PDF 完整转换、本次未做准确率/性能对照实验，均未验收。

60 秒请求配置下已确认：ordinary 任务 completed；显式 report-writing 正文加载后生成 acceptance.md，HTTP 下载成功、测试数据标注明确、实际字节为 CRLF；search 经真实互联网工具、规则/模型检查 passed 后发布可用结果并 completed。每条运行及自动断言见 [14-live.log](14-live.log)。历史失败见 14-live-initial.log、14-live-before-review-fix.log、14-live-request30.log；直接专家与检查模型诊断分别见 14-expert-live.log、14-review-diagnostic.log，均为真实调用，不计为通过结果。

最终普通搜索见 [14-live-real-search.log](14-live-real-search.log)：工具真实执行、检查 passed、摘要发布与整轮 completed 均通过。实际请求 query=site:docs.tavily.com search、max_results=2、include_raw_content=false，响应 query=search；原文 SHA-256 校验通过，executed_sql 为空，见 [14-search-evidence-live.log](14-search-evidence-live.log)。这证明补齐了请求证据，没有把服务回显改造成请求值。

最终内外对照最小降级见 [14-live-internal-external.log](14-live-internal-external.log)：db 依赖失败保存、搜索拿到本轮 db_failure_ids、实际搜索、检查通过和明确降级答案五项断言均通过。主模型 task 字段及子任务说明哈希关联见 [14-task-contract-live.log](14-task-contract-live.log)。真实普通/降级检查按场景分别补跑，命令退出码见 [14-live-command.log](14-live-command.log)，未把此前失败的整组命令重标为通过。

原行业资料验收中出现二手来源和统计口径冲突，检查触发一次补查后达到5分钟总时限，记录保留在 14-live-internal-external-timed-out.log。一次验收请求把必需范围字段与摘要字数上限混在一起，也导致拒绝；后续明确将 URL/范围字段排除于摘要长度限制之外。保留这些真实失败，未关闭检查、提高补查次数或强行发布不合格结果。导出日志省略 raw_text 工具原文正文，保留原文哈希、位置、状态、检查结果及完整最终答案；原始证据仍保存在隔离的运行数据库/输出目录中。

## 浏览器检查

使用本地验收服务的真实页面操作，不注入假事件或伪造终态。已确认 Skill 选择、正文与参考资源读取、完整答案、刷新恢复、重复提交忙碌、实际取消及取消后下载链接。浏览器观察见 [14-browser.log](14-browser.log)，取消截图见 [14-browser-cancel.png](14-browser-cancel.png)。点击取消与任务完成竞争时有一次按钮已禁用，随后观察确认该轮 completed；另用实际运行中的任务完成取消验收。

真实降级运行的73条持久事件已输入生产前端脚本：服务端 completed 显示为“部分完成”，完整答案与降级声明保持一致，见 [14-live-frontend.log](14-live-frontend.log)。这项状态验证使用实际事件；原有 Node 用例另覆盖可控错误、取消、超时与恢复。实际 HTTP 重复提交为409且返回原 run_id，见 [14-live-busy.log](14-live-busy.log)。

## 服务清理

各轮独立验收服务均通过 stop 标记正常退出，输出确认 ACCEPTANCE_SERVER_STOPPED。最终服务 PID=17720，退出码0；8765无监听、该进程不存在，端口重新绑定成功后探针已关闭。见 [14-server-final.log](14-server-final.log)、[14-cleanup.log](14-cleanup.log)。没有启动生产8000端口服务，未停止无关进程。

## 后续功能

尚无动态助手、任意 Skill 脚本、分布式调度、多进程 SQLite 并发编排、重启自动恢复执行、完整登录鉴权和跨轮自动复用。清除浏览器本地存储后不会自动枚举所有历史轮次；当前通过已知 run_id 读取记录。单会话一个活动 WebSocket 的既有约定仍适用。
