# 步骤 1：验证环境修复记录

后续实现与当前验证结果见 [基本多智能体修复记录](39-basic-multi-agent-repair.md)；本文保留步骤 1 当时的执行记录，不能代表当前源码仍存在相同失败。

日期：2026-10-27。实施范围仅为 37 号方案的步骤 1。

状态：验证环境基础修复已落地；完整验收尚未通过。仍有 183 项失败，均保留为失败；不能据此进入后续步骤或宣称基本多智能体修复完成。

## Codex 作为主要方案来源

先读取并核对 OpenAI Codex 固定提交 `970111f6cb62cc02fe36a82ea7d71b37a2889c97`，再进行对应修改：

| 已读取的源码 | 采用的机制 | 本项目落地 |
| --- | --- | --- |
| [test_codex.rs：TestCodexBuilder::build](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/tests/common/test_codex.rs#L602) | 统一创建临时 home、工作目录和模拟服务连接，集中构建测试运行环境 | `tests/runtime_fixtures.py` 的 `OfflineEnvironment` 统一数据库、检查点、输出、上传目录及测试认证配置；相关夹具显式依赖此环境 |
| [test_codex.rs：build_from_config](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/tests/common/test_codex.rs#L837) | 在同一个构建入口初始化状态数据库和认证管理，复用真实运行组件 | 调用现有 `api.migrate_security.migrate`，持有真实 `DataLease`；保留 `RunStore.open` 的拒绝未迁移行为；HTTP 使用正式 lifespan |
| [login_server_e2e.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/login/tests/suite/login_server_e2e.rs#L108) | 临时凭据目录、可控认证依赖、实际登录请求 | 使用隔离测试口令调用真实 `/api/auth/login`，携带真实会话 Cookie、Origin、CSRF；增加匿名、错误 Origin、错误 CSRF 和未归属线程的拒绝检查 |

采用的是上述集中构建和真实入口测试机制。Codex 使用 Rust，未找到可直接放入本项目的 Python/FastAPI 迁移、密码登录或三个 Node VM 夹具补丁；这些具体接线按本项目现有接口适配，没有接入 Codex 执行内核。

另在 Codex 仓库检索了数据库迁移夹具、认证测试及 WebSocket/CloseEvent/BroadcastChannel 测试。浏览器 VM 的具体错误未找到直接可用的实现，因此核对本项目 `app.js`、`run-events.js` 和已安装 Starlette 的真实接口后修正。

## 实际变更

1. 集中临时环境：显式迁移 `runs.db` 与 `jobs.db`，通过 `RunStore.create_thread` 建立测试资源归属。运行时不自动迁移，不直接写入伪造 owner。
2. HTTP/WebSocket：进入正式认证生命周期后登录。修复 Starlette WebSocket 测试默认使用 `ws://testserver` 而不继承 HTTP base URL 的问题，显式使用与认证一致的地址和 Origin。文件下载夹具改用所属 run 的 `/api/runs/{run_id}/files/download`。
3. 假客户端：对齐 `Overwrite.value`、每次执行的 `run_id` 检查点、当前文档读取返回形状和 SDK 所在的实际导入位置。SDK 适配器测试在显式内联 worker 夹具中运行真实适配函数；独立进程回收与取消检查继续由现有阶段二/四用例覆盖。
4. 工具/引用夹具：复用正式 `bind_framework_tools` 和版本化 evidence adapter；使用真实 `make_record`、`register_options` 签发引用 ID。最后 5 项终稿审核夹具在设置运行上下文及任务归属后构造报告，不再由模型或夹具自行编造引文 ID。
5. 历史数据库：冻结真正的 v1/v2/v3/v4/v6 表结构，在隔离副本中迁移并保留身份、历史及外键检查；不再将当前安全数据库仅改一个版本号冒充旧版数据库。历史副本只用于测试。
6. 浏览器：抽出共享 VM 夹具，执行真实页面的登录事件和动态技能目录加载；提供 BroadcastChannel、Headers、计时器等浏览器接口；假 socket 的 close 传递 CloseEvent.code；完成状态与下载链接对齐当前服务端契约。
7. 验证入口：增加可选 `--baseline-report`，保留所有用例的状态变化、新增及移除名称；任何失败仍令验证返回非零。默认离线验证超时改为 1200 秒，本轮实际完整执行为 593.380 秒；此前大量用例初始化即失败时只需 72.480 秒。此调整仅覆盖验证命令，不修改应用执行预算或超时。
8. 修正本次涉及的格式问题；`tests/manual/stage1_acceptance.py` 只修正基线已有的 Ruff 格式失败，未执行真实业务题。

所有 Python 操作通过 uv 运行现有环境；未升级依赖。测试收集阶段清除外部服务及应用凭据，禁用 dotenv 和追踪，保留外部网络拒绝保护。业务服务和数据库均使用假客户端及临时数据。

## 退役用例的验证义务

原有 8 项 PDF/Word 转换场景迁移为 8 项当前有效场景，另新增 2 项验证环境保护用例，总数由 623 变为 625。没有跳过、xfail 或失败白名单。

| 原验证义务 | 当前检查 |
| --- | --- |
| 长 PDF 转换的提交/发布 | 退役转换模块不存在，未注册的转换 handler 明确拒绝，任务列表为空 |
| 取消转换不发布迟到文件 | 退役转换不能创建任务或产出文件；通用后台取消、迟到完成和租约保护保留在既有有效后台任务用例中 |
| COM 清理、只读模板和临时转换文件 | COM/转换已移除；验证 Markdown 写入本轮目录、模板字节不变、源目录不增加文件、本轮不产生额外临时文件 |
| 五类 HTML 本地资源/脚本加载输入 | Markdown 惰性保存原文，不创建 PDF，也不加载或执行其中资源；既有浏览器清洗行为不作修改 |

名称对应关系及所有新增/移除名称记录在 `output/verification/step1-final/report.json` 的 `baseline_comparison` 中，不能将名称变化解释成原用例未经执行即通过。

## 执行证据

| 检查 | 基线 | 修复后 |
| --- | --- | --- |
| Python 回归 | 623 项：220 通过、245 失败、158 个夹具错误 | 全量 625 项：437 通过、183 失败、5 个夹具错误；随后仅复验最后修改的 5 项引用夹具，5/5 通过 |
| 按精确用例名合并的最终证据 | 403 项未通过 | 442 通过、183 失败、0 个夹具错误；不是第二次全量执行 |
| 原有通过用例 | 220 | 全部仍通过，无通过变失败 |
| 前端 | 3 个文件失败 | 3/3 通过；最后下载 URL 夹具修改后再次验证通过 |
| uv 依赖一致性、全仓 Ruff、全仓格式 | 依赖与 Ruff 通过，格式失败 | 均通过；最后引用夹具修改后重查 Ruff 和格式通过 |

原始全量命令：

```powershell
& C:/Users/Dell/.local/bin/uv.exe run --offline --no-project --python .venv/Scripts/python.exe python -X utf8 -B -m tools.verify_project --report-dir output/verification/step1-final --uv C:/Users/Dell/.local/bin/uv.exe --baseline-report output/verification/step1-before/report.json
```

最后 5 项夹具复验：

```powershell
& C:/Users/Dell/.local/bin/uv.exe run --offline --no-project --python .venv/Scripts/python.exe python -X utf8 -B -m pytest tests/test_answer_review.py -q --tb=short --junitxml=output/verification/step1-answer-review.xml
```

全量执行中，生命周期 12/12、后台任务 12/12、事件存储 12/12、事件重放 6/6、运行存储 11/11、并发 8/8、取消 18/18、阶段四 33/33、新增验证环境 2/2 均通过。通过的是这些具体测试，不代表方案六项业务验收已经完成。

原始与合并证据：

- [基线](../../output/verification/step1-before/report.md)
- [原始完整回归](../../output/verification/step1-final/report.md)
- [最后 5 项复验](../../output/verification/step1-answer-review.xml)
- [保留每项执行来源的合并报告](../../output/verification/step1-reviewed/report.json)
- [183 项失败逐项清单](38-step1-failure-inventory.md)

## 未完成项目与已确认缺陷

183 项失败中，125 项匹配既有旧契约首个阻塞规则，56 项仍待逐项复核，2 项已确认实现缺陷。分类不改变测试结果，不将“旧契约”自动视为可忽略，不将待复核项自动判为生产缺陷。

已确认实现缺陷：

1. **裸 SELECT 未在 SQL 校验处拒绝。** `validate_sql("SELECT")` 能到达数据库适配器；测试使用明确拒绝连接的假 connect，真实数据库未被访问。保留 `test_unsafe_sql_rejected_before_database_connect[SELECT]` 的失败，不将异常类型或有效断言放宽来取得通过。
2. **业务文档读取与 Skill 元数据解析接线不一致。** `read_file_content` 将包含 extraction 展示头的正文传给 `stage_read`，随后 `parse_metadata` 无法在开头找到合法 YAML。测试已读取真实有效的临时 Skill，此失败不是缺文件或未进入实际逻辑。

本轮未扩大到这两项生产行为修复。剩余清单还包含废弃辅助接口、旧计划/事实输入以及不同终态/审查预期，仍须迁移或核查。因此只能确认验证环境基础修复及上述执行证据，不能宣称步骤 1 的所有出口或整个修复方案已通过；步骤 2 至 5 未实施。

## 工作区与文件校验

修改保留在工作区。Git 仅执行 status，遵守项目禁止其他 Git 操作的约定，未执行 git diff --check；以修改前字节副本和逐行比较检查新增/修改行的空白，并检查相关文件实际 UTF-8、BOM、CRLF 字节。最终字节审计见 `output/verification/step1-reviewed/file-audit.json`。
