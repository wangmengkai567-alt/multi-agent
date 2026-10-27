# 顺序④：Skill 与工具披露精修

实施日期：2026-10-26（Asia/Shanghai）。

## 范围与参考源码

已读取用户引用的“查找多 Agent 协作参考项目”对话，包括完整七项分析与四步实施顺序。本次只实施顺序④：保留既有正确机制，按当前任务权限与阶段披露技能和工具，不重做预算、审核或业务完成协议。

先阅读 openai/codex 固定提交 970111f6cb62cc02fe36a82ea7d71b37a2889c97 的以下源码，再修改对应实现：

| Codex 源码 | 采用的机制 | 本项目落点 |
|---|---|---|
| [selection.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/skills/src/selection.rs) | 先解析显式选择，再与启用范围匹配 | 保留结构化 skill_ids；专家只加载显式/必需集合与 allowed_skill_ids 的交集 |
| [host_prompt.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/ext/skills/src/host_prompt.rs) | 只为选中的 Skill 读取并构建正文片段 | 保留首轮显式正文注入、缓存及作用域隔离，不提前注入 references |
| [catalog_prompt.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/ext/skills/src/catalog_prompt.rs) | 元信息目录、完整读取正文、按需读取引用资源 | 目录仅解码有界 YAML 头；记录框架最终返回的内容，分页和截断不能冒充全文 |
| [required.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/ext/skills/src/required.rs) | 在推理前验证指定范围内所需 Skill 可用 | 显式或业务必需 Skill 缺失、元数据无效、正文空或不可读时，在首次模型请求前失败 |
| [registry.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/tools/registry.rs) | 工具注册按 ToolPolicy 收窄，调用仍经注册和执行校验 | 共享能力判定供模型工具清单、Hook 准入与直接委派入口使用 |
| [render.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/ext/skills/src/render.rs) | 目录开销有界，披露省略信息 | 本项目固定五项注册、单描述上限 1024 字符；目录自然小于 8000 字符，不另建动态大目录分配器 |

这些参考模块是 Rust 内部实现，本次将其机制适配到现有 Python/LangChain 公共中间件接口，没有新增 Codex CLI、子进程或运行时依赖。没有照搬上游的回退规则。

## 实际修复

### 工具清单与实际准入一致

新增 agent/runtime/capabilities.py，并在主代理及专家工厂中安装 CapabilityDisclosureMiddleware。

- 根据锁定契约、联网权限、采集阶段和后台服务可用性收窄模型工具清单。
- 禁网时不提供联网研究与规划工具，也不在 task 的描述和 subagent_type 枚举中列出联网专家。
- 已不可用的专家从本轮委派能力中移除；直接请求仍保留既有失败原因。
- 直接答复、澄清与已经关闭的调查阶段不能再开启研究。最终答复原有 tools=[] 仍有效。
- 模型不再拿到写文件、编辑、生成文件和转换文件的能力；程序获准交付继续通过原有权限、预算、正文及产物校验。
- task 的模型视图按请求复制，不修改共享的已注册工具、执行器或其他轮次的 schema。
- Hook 与 DelegationRunnable 使用同一范围判定；强行请求未披露能力也不能绕过准入。未知工具、参数、路径、来源证据与预算检查继续保留。
- 文件系统提示改用明确的只读说明，保留 grep 的字面量语义，不增加正则或脚本执行能力。

### Skill 目录、正文与资源分开

agent/runtime/skills.py 的目录发现只读取最多 32 KiB 的 YAML 头，使用二进制行读取，正文即使包含非法 UTF-8 也不会在目录阶段被解码；真正选择并加载时仍拒绝不可读正文。完整资源读取沿用 10 MiB 上限。

显式选择和业务必需 Skill 在模型调用前校验；专家仍按 allowed_skill_ids 过滤。未选且元数据不可用的可选条目从模型目录中移除，并记录 unavailable 事件，不用替代技能继续执行。目录发现不会记录正文 loaded，也不会预先读取参考资料。

新增 SkillReadMiddleware，位于框架文件结果处理外侧。SkillBackend 和 read_file_content 保留本次实际读取的快照，等结果经过框架分页、截断或转存后，才核对模型真正拿到的完整行。正文全部覆盖后才记录 loaded，参考资料全部覆盖后才记录 resource_loaded。长逻辑行的续行被截掉、结果被转存为预览、失败或拒绝的调用，都不会被误记成全文读取。

读取覆盖按专家、委派调用、技能和资源隔离；同一作用域内资源摘要改变时拒绝混合不同版本的分页。UTF-8 BOM 与换行统一参与规范化读取，正文与参考资料保持只读。loaded 只代表已读取，不代表执行或业务完成。

Skill 读状态依旧是本轮的临时状态，没有新增持久化字段、数据库迁移或检查点版本变化；恢复后按原机制重新发现和加载。

### 修正过时的 Skill 说明

更新现有五个 SKILL.md 及文档证据参考：材料证据由 document 使用 read_document/read_bound_material 取得，公开检索遵循 plan_research/research_step，文件由程序在终审后生成。修正文档解析范围为 Word 正文段落及顶层表格。移除“主模型直接采集未审核事实”“模型调用文件生成工具”等旧指引。

## 验证与边界

未新增或修改测试文件。定向验证从 stdin 运行，材料、结果和 Markdown 产物均位于系统临时目录；模型提供受控协议响应，没有真实模型、搜索、MySQL 或 Word 请求。

已通过的检查覆盖：

- 同步/异步真实文件工具：分页后全文、框架字符截断、长行续行被裁切、显式正文与参考资源分离。
- 同步/异步委派：禁网时 task 的枚举和说明收窄，强行选择 search 被拒绝，允许的专家仍可执行，共享原工具 schema 不变，交付阶段不能再开启委派。
- 同步/异步范围：显式集合与专家允许集合取交集，父子/调用读取记录隔离，大正文被框架转存为预览时不登记全文。
- 五个 Skill 分别缺失、元数据无效、正文为空：全部在模型请求前拒绝；业务必需项缺失同样拒绝。可选不可用目录、BOM、只读写入拒绝与资源版本变化均有检查。
- 真实 _build_agent 构建链：显式 Skill → 意图契约 → 直接答复零工具 → 内容审核 → finalize_answer；同步与异步均通过。
- 真实 build_subagents 构建链：范围内 Skill 和参考资料 → 原始绑定上传文件 → 采集终止 → 独立事实报告 → 语义审核；同步与异步均通过。
- 程序 Markdown 交付：模型未获得写文件工具，程序仍只执行一次真实生成，产物及最终交付校验通过；同步与异步均通过。

既有回归运行 tests/test_skill_loading.py 和 tests/test_tool_policies.py，共 155 项：

| 时点 | 通过 | 失败 | 夹具错误 |
|---|---:|---:|---:|
| 修改前 | 89 | 27 | 39 |
| 修改后 | 88 | 28 | 39 |

新增失败是 test_skill_and_upload_directories_readonly_and_separate：旧夹具把普通文本 instructions 写成 SKILL.md，没有 YAML 元数据。现在该读取会按非法技能拒绝；使用合法 Skill 的同等只读、上传目录分离及写入拒绝定向检查通过。未修改该测试，也未增加接受非法 Skill 的兼容分支。其余失败及夹具错误与修改前相同，不能据此声称整个项目回归通过。

Ruff lint/format、Python AST、实际 UTF-8/BOM/CRLF 和逐行空白检查通过；测试文件摘要与修改前快照一致。遵循项目只允许 git status 的限制，未执行 git diff --check，以修改前快照逐行比较和文件字节检查核查本轮修改。

未重跑用户的三道业务验收题，以上结果不等于真实模型业务验收通过。修改保留在工作区，由用户自行提交。
