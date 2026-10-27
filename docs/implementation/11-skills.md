# 第 11 步：Skill 选择、正文与资源读取

日期：2026-10-16（Asia/Shanghai）。状态：第 11 步完成，离线验收通过。

## 工作区与规则

实施目录：D:\My_workspace\Multi-agent\Multi-agent。修改前 git status --short 输出为空。只执行 git status，未执行其他 Git 操作，未暂存或提交。上层 AGENTS.md 和项目 .editorconfig 规定 UTF-8 / CRLF；未发现子目录 AGENTS.md 或 .gitattributes。保留既有 BOM 设置，新文件无 BOM。Git 配置查询、git ls-files 和 git diff --check 因 Git 限制未执行，以实际字节、BOM 和新增行空白审计替代。

本步参考 skill-creator 的渐进加载、精确触发与资源拆分规范编写项目业务 Skill；没有向用户的全局 Codex Skill 目录安装内容。使用其 quick_validate.py 分别校验两份 Skill，输出见 11-validate-ecommerce-analysis.log、11-validate-report-writing.log。

## 业务说明与接入位置

新增 skills/ecommerce-analysis/SKILL.md 及 references/metrics.md，组织运营指标、内外数据对照、证据引用和依赖缺失处理；指标参考说明时间边界、分子分母、单位、聚合与截断限制。新增 skills/report-writing/SKILL.md 及 references/template.md，组织报告结构、引用、差异、限制与按请求的文件交付。

prompt/prompts.yml 精简为角色、工具和简短常驻要求，详细分析与文档流程集中到 Skill，不再要求普通问答套用完整报告流程或固定篇幅。Skill 明确不改变服务端输出目录、只读 SQL、文件权限及调用预算；本阶段没有脚本执行能力。

agent/runtime/skills.py 新增 ProjectSkillsMiddleware，复用已安装 deepagents 0.4.3 的 SkillsMiddleware 接口。生产主 Agent 在 _build_agent 中明确接入；没有假设框架为自建执行器自动继承。主 Agent 可发现两个 Skill；默认 db / search 只配置 ecommerce-analysis，knowledge 未配置 Skill。自定义注册项仅接受已知且不重复的 Skill ID。

DelegationRunnable 在同步和异步入口设置独立 ContextVar 权限范围，所有退出路径恢复范围。子执行器的 Skill 发现、文件后端和现有工具权限校验使用同一范围，不能通过直接猜测父级正文路径绕过。新范围不改变原有子任务路由、检查重试或调用计数。

## 请求与加载语义

POST /api/task 新增可选 skill_ids，例如：

~~~json
{"query":"分析近期经营数据","skill_ids":["ecommerce-analysis"]}
~~~

允许两个稳定 ID ecommerce-analysis、report-writing，缺省、null 或空列表表示由模型根据元信息按需选择。未知 ID、目录穿越字符串、重复 ID、非字符串成员或非列表值返回 422，创建运行记录前拒绝。

显式选择后，运行上下文携带不可变 ID 元组。主 Agent 在首次模型调用前从受控路径读取完整正文，校验元信息名称与目录一致且正文非空，再把正文注入本轮模型系统消息。正文保存在本轮上下文中，不作为持久 messages 写入 checkpoint。没有显式选择时先只展示名称、描述和读取路径，正文与资源由真实 read_file 按需加载；报告模板不会强制注入普通查询。

框架会缓存 skills_metadata，且可能把父级状态带入子任务。本步每次 Agent 调用都重新发现其权限范围内的元信息，避免继承父级列表或沿用旧的空缓存。框架对名称与目录不匹配原本只发警告，项目层将其明确拒绝。缺少或不合法的显式正文会在模型调用前报错，不静默退回无正文运行。

## 只读资源与真实读取事件

沿用 run_backend 的 /skills/ 组合挂载，新增 SkillBackend 限制到两个注册目录及其声明的 Markdown 资源。工作区上传文件与服务端说明目录分开；同名上传 SKILL.md 不计为业务 Skill。未知资源、脚本路径、跨 Skill 的 .. 路径、越界与符号链接/联接按现有路径策略拒绝。

框架 write / edit / upload 和业务文件路径校验均保持 Skill 只读。业务 read_file_content 读取合法正文或资源时同样记录状态，避免存在不经过记录的常用正文读取入口。模板文字不能授予路径或执行权限；真实框架测试读取恶意模板后请求越界写入及覆盖 Skill，两次写入均被拦截。

通过统一 make_event / EventSink 保存 skill_state 事件，使用既有 schema 4 events 表，无数据库迁移或额外监控协议：

- selected：用户显式选择，或模型实际开始读取该 Skill；只有元信息扫描不计选择。
- loaded：完整正文已从服务端读入本轮系统说明，或模型 read_file / read_file_content 已读取全文。
- resource_loaded：声明的参考或模板正文已完整读取；不因加载主正文而假装已读附属资源。

部分分页读取只记选择，覆盖全部行后才记 loaded / resource_loaded；错误或越界读取不记成功状态。按本轮、执行对象、子任务、Skill、资源去重，重复读取不会重复宣称加载。事件关联 thread_id、run_id、call_id、parent_call_id，包含读取内容的 SHA-256（规范化读取文本）及选择原因，不保存正文或用户上传文本。提交或持久化失败仍通过现有事件错误与最终 recording_status 暴露，不承诺失败记录已落库。

## 验证范围与记录

离线测试使用 ScriptedModel、临时 SQLite 和本轮独立测试目录，禁止外网、真实凭据及付费服务。因既有 .pytest_cache 目录权限问题，测试显式禁用 cacheprovider 并将 --basetemp 指向 output/ 下独立目录，不更改系统权限。

- 冒烟检查：注册表及生产构建调用，2 passed in 13.20s。
- 初轮 Skill 专项：2 failed, 26 passed in 38.81s。发现框架允许名称/目录不匹配，以及测试把正文里提及报告 Skill 的名字误当成报告 Skill 可见。已增加严格匹配校验，并改为检查未授权正文路径与内容确实不在专家上下文。
- 修复后的 Skill 与工具策略回归：128 passed in 44.61s。

最终检查命令与实际输出（均使用项目 .venv/Scripts/python.exe -X utf8）：

- python -m pytest -q -p no:cacheprovider --basetemp=output/test-step11-full-01 → 302 passed in 278.05s (0:04:38)，见 11-tests.log。
- python -m pytest tests/test_skill_loading.py tests/test_hook_pipeline.py::test_production_builder_and_persisted_child_correlation -q -p no:cacheprovider --basetemp=output/test-step11-final-targeted → 30 passed in 72.36s (0:01:12)，见 11-skills.log。
- python -m ruff check（本步 13 个 Python 文件）→ All checks passed!，见 11-lint.log。
- python -m ruff format --check（同 13 个文件）→ 13 files already formatted，见 11-format.log。
- python -m pip check → No broken requirements found.，见 11-dependencies.log。
- quick_validate.py 分别验证两份业务 Skill → 两次 Skill is valid!，见 11-validate-*.log。
- 实际版本：Python 3.11.15、deepagents 0.4.3、langchain 1.2.10、langgraph 1.0.9、langgraph-checkpoint-sqlite 3.0.3、pytest 8.3.5、aiosqlite 0.22.1。未安装或升级依赖，见 11-environment.log。
- 全部交付文本的 UTF-8、CRLF、BOM、末尾空白和文件哈希审计见 11-text-audit.log。

核心验收通过真实 create_deep_agent / create_agent / task / read_file 链路，检查模型在实际读取后看到了正确正文与资源；显式选择则检查首次生产模型输入已含正文，不以配置参数被接受代替真实加载。覆盖同步和异步、分页读取、未读资源、未授权专家、缓存刷新、相同会话下一轮、两会话并发、错误 API 输入、只读资源及恶意模板。完整命令与实际输出保存在同目录 11-*.log。

真实模型与 MySQL / Tavily / RAGFlow 服务联调：未执行。业务内外对照的服务端前置顺序、取消和事件补发仍按第 12、13 步实施；本步不将 Skill 文本视为已完成运行时流程强制。
