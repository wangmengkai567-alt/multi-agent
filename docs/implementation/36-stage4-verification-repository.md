# 阶段四：⑧ 验证基础、⑨ 仓库记录

日期：2026-10-27。项目根目录：D:/My_workspace/Multi-agent/Multi-agent。

## 状态与范围

依据“完成顺序4修复”对话的四阶段方案。已实现统一验证入口、Windows 回归 CI、逐项失败归因和 docs 忽略规则修复；实跑还发现并修复一处 Windows 摘要历史文件名缺陷。**阶段四尚未完成业务验收，回归也未全绿。**

本轮未新增、修改或删除 tests/ 中的文件，未恢复旧协议，未放宽权限、证据或终稿检查。仅执行 git status，未暂存、提交、推送或操作分支。

## 优先阅读的 Codex 源码

固定提交：970111f6cb62cc02fe36a82ea7d71b37a2889c97；以下均读取了实际源码。

| 范围 | 参考 | 本项目适配 |
| --- | --- | --- |
| 共享运行环境 | [test_codex.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/tests/common/test_codex.rs) | 对照其临时 home、显式环境和模拟服务绑定；使用临时完整实例、正式离线迁移与真实认证 lifespan 验证当前链路，未让旧夹具自动迁移 |
| 实际请求核对 | [skills_extension.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/tests/suite/skills_extension.rs) | 在真实 OpenAI 兼容客户端的 MockTransport 边界检查实际 JSON、工具披露、正文与审核输入；不以构造提示词等同实际发送 |
| 文档与产物管理 | [.gitignore](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/.gitignore) | 取消 docs/ 整体忽略，按生成产物范围忽略日志和截图，保留设计/修复/验收 Markdown |
| 历史文件的标识 | [thread_id.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/protocol/src/thread_id.rs) | 参考其可序列化 UUID 标识与历史文件的关系；本项目图帧直接使用已签发且通过校验的 run_id，不把带冒号的复合范围键放入文件名 |

这些属于机制适配；未接入 Codex SDK、app-server 或 Rust 执行内核，不声称 Codex 包含本项目业务需求/证据覆盖实现。

## ⑧ 已实施

### 统一验证入口

新增 tools/verify_project.py。通过同一 Python 解释器依次执行 uv pip check、全仓 Ruff 检查、全仓格式检查、完整既有 pytest 和全部既有前端 Node 用例。各步骤失败后仍记录其余步骤，最终返回非零状态；不会跳过失败用例、改成 xfail、关闭断言或把归因标签算作通过。

- 显式设置 UTF-8，避免 Windows 默认 GBK 使中文错误日志损坏。
- 验证子进程不继承模型、搜索、数据库和应用凭据，禁用 .env 加载及追踪；pytest 仍使用原 conftest 的禁止联网和临时工作目录规则。
- Node、uv 缺失或命令超时作为明确失败记录；不自动选择其他解释器、包管理器或执行路径。
- 报告目录必须是 output/ 下新建的子目录，已有证据目录拒绝覆盖。目录中保存步骤日志、pytest.xml、逐项 report.json 和汇总 report.md。
- 自动规则只标注首个可识别阻塞点；未识别项明确 needs_investigation，不自动解释为旧测试。本轮56项人工复核单独保存，未修改原始报告。
- 修正 README 仍声称 PDF 导出需要 Word 的过时描述，明确仅正文与 Markdown。

PowerShell 命令：

```powershell
uv run --offline --no-project --python .venv\Scripts\python.exe python -X utf8 -B -m tools.verify_project --report-dir output/verification/local
```

Node/uv 不在 PATH 时用 --node 和 --uv 提供其实际绝对路径。重复执行须使用新的报告目录。

### CI

.github/workflows/ci.yml 保留现有静态检查，新增 Windows Offline Regression。使用固定 uv 版本、Python 3.11、现有 Windows 开发依赖锁和 Node 24，调用同一验证入口。失败时仍上传证据；没有 continue-on-error 或失败白名单。当前历史测试未迁移，因此该回归任务仍会失败；不能将“已接入 CI”写成“CI 已通过”。没有推送或触发远端工作流，本轮仅检查了本地配置与对应命令。

### 回归逐项归因

完整基线：623 项，**220 通过、245 失败、158 个夹具错误**。每个失败/错误都有原始位置及单独归因，见 [403项清单](36-stage4-failure-inventory.md)。首个阻塞点的人工复核结果：

| 类别 | 数量 | 明确依据 |
| --- | ---: | --- |
| 夹具/初始化 | 225 | 未显式迁移临时数据库、未进入认证 lifespan、缺少采集 owner/契约、测试模块导入或 Skill 元数据错误 |
| 旧契约 | 177 | 旧 source_id+quote、旧 RouteDecision、未绑定工具适配器、旧检查点身份、已移除转换模块/API、旧专家集合及旧交付行为 |
| 配置预期 | 1 | 仍硬编码剩余10秒即收尾；当前无其他预留时的有效阈值是 shutdown_seconds=5秒 |

归因说明的是该用例首先停在哪里；未运行的下游断言仍未验证。这225项不能靠运行时自动迁移或绕过登录消除，177项不能靠恢复旧接口消除。迁移测试夹具与断言仍需明确授权，本轮遵守“不新增或修改测试”的约束。

前端3个既有文件均失败：两个 VM 夹具没有 BroadcastChannel；一个假 WebSocket 的 close() 不传 CloseEvent。全仓格式检查还指出 tests/manual/stage1_acceptance.py 与 tests/test_tool_policies.py 两个既有文件，本轮未改动它们。

### 当前契约的隔离链路验证

通过终端 stdin 执行，未创建测试文件。临时完整实例先运行正式 migrate，再进入 server.lifespan；模型使用真实 ChatOpenAI HTTP 序列化和内存 MockTransport 响应，未访问供应商。

1. 未登录请求401；登录成功后，无 CSRF 的修改请求403。
2. 经 /api/task 运行实际主图，发出 task_route、direct_draft、direct_review 三个 HTTP 请求；直接加工正文时没有披露工具，给定正文进入实际生成和审核请求。
3. 正式审核事件 passed；completion.status=complete、coverage=complete；Markdown 由程序生成并通过真实认证下载入口下载，44字节与审核正文的 UTF-8/CRLF 内容逐字节一致。
4. 账本有3次传输、3份供应商形状用量，输入300、输出150 tokens；这些是内存服务提供的合成用量，仅验证计量通路。
5. 关闭与重新打开同一迁移实例后，原运行仍为 completed；没有使用或修改生产 data/。

证据：output/stage4-current-flow.json。它证明当前传输、认证、正式交付和初始化通路，不能证明真实模型语义能力或复杂搜索成功。

### 实跑发现并修复：摘要历史文件名

首次真实业务题日志复现：主图线程 ID 为 research_scope_id:run_id，框架将其直接拼成 /conversation_history/<thread_id>.md。冒号违反 Windows 文件名规则，GuardedFilesystemBackend 拒绝写入，摘要时历史原文未成功保存。

最小复现通过真实 backend.write 调用得到 PathDenied；不是模型响应或过期夹具导致。修复 agent/main_agent.py：每次执行的图线程 ID 使用 validate_identifier(context.run_id)。run_id 已唯一标识执行帧；research_scope_id 继续承担逻辑研究范围身份，预算、证据和检查点所属关系没有迁移。没有字符替换、哈希文件名、路径校验豁免或旧命名兼容路径。

修复后使用实际 run_deep_agent 配置、LangGraph 与 Deep Agents SummarizationMiddleware 触发摘要，确认 conversation_history/execution-1.md 被写入并保留先前原文；无 offload 警告。含冒号、路径穿越的两种写入仍被拒绝。证据：output/stage4-summary-fix.json。真实业务进程在修改前已经加载源码；该题没有因工作区修复而自动重启，不能作为修复后的真实业务通过证据。

## ⑨ 已实施

.gitignore 不再排除整个 docs/；保留 AGENTS.md 本地忽略，按 docs/**/*.log、docs/implementation/*.png、docs/generated/ 忽略生成产物，output/ 等既有运行目录继续忽略。

git status --short --untracked-files=all 已显示设计、修复、验收 Markdown 可被发现，包括此前被整体隐藏的历史记录；生成日志和截图未出现在未跟踪清单。没有自动暂存这些文档，版本管理仍由用户决定。

## 第1道真实业务题：失败并停止

执行既有 tests/manual/project_acceptance.py --phase 1，在 output/project-acceptance/stage4-20261027 下创建隔离实例，使用真实模型；未进入第2题，也未自动重跑第1题。

问题：研究 Deep Agents、Open Deep Research、smolagents 的委派/工具/结果/失败取消机制，结合上传代码摘录生成有源码定位的 Markdown 报告。

运行 d4d4f49b-7426-4241-8192-f038db18dae5，耗时192.53秒，status=failed、coverage=unknown。只有5次主助手 read_file 与3次 plan_research，没有实际检索、文档读取或专家结果。3次计划分别因自行指定非用户授权搜索域、无已审核事实的发现对象、非逐字需求 anchor 被拒绝。终稿继续输出无事实支持的事实块，最终被 Fact claim requires fact support only 拒绝。没有通过终审，也没有 Markdown 交付。

失败不是“检索服务不可用”的证据，因为该题未执行检索。路径修复解决已复现的历史写入问题，不证明这些模型决策错误已经解决。原始日志及 history/result/请求计量保留在 output/ 下，未覆盖。按每题结束后汇报并停止的约定，后续真实题和再次实跑没有执行。

## 验收边界与未完成项

- ⑨及验证设施已落实；403项失败归因已记录，不等于403项已通过。
- 旧测试迁移受显式约束，本轮未实施；全套回归及新增回归 CI 仍失败。
- Windows 摘要历史写入缺陷已修复并通过实际框架离线验证；真实业务题仍失败，修复后未重新实跑。
- 后续应先按逐项清单迁移测试环境和契约，再单独处理业务计划决策失败；不能放宽来源授权、逐字需求或事实支持校验来过关。
- 相关改动检查 UTF-8、BOM、实际 CRLF、尾随空白和冲突标记；项目只允许 git status，因此没有执行 git diff --check，使用直接字节扫描。

## 最终收尾检查

在摘要路径修复后的最终源码上执行统一入口，报告为 output/verification/stage4-final/report.md：仍为220通过、245失败、158个夹具错误，逐用例结果与修复前基线完全一致，没有新增失败；前端仍3个文件失败。uv依赖一致性和全仓Ruff检查通过；全仓格式检查仍仅上述两个未改动测试文件失败。相关实现与新文件的格式检查通过。

另外通过stdin验证了验证器本身的UTF-8/CRLF输出、保留非零退出码、缺失执行器明确失败、未知归因不擅自归类、完整解析623条JUnit记录。CI YAML解析通过，未设置continue-on-error。7个本轮改动/新文件均通过UTF-8、实际CRLF、尾随空白和冲突标记检查，原主助手文件BOM设置保留。

未再次调用真实模型或进入下一道业务题。提交与版本管理仍由用户处理。
