# 阶段一：Skill 上下文保留与请求容量保护

实施日期：2026-10-26。范围仅为修复计划①②。未修改测试文件，未运行真实业务验收题，未进行提交、推送或其他 Git 操作；只执行 git status。

## Codex 对照依据

固定源码提交：970111f6cb62cc02fe36a82ea7d71b37a2889c97。

- 正文进入会话历史、后续模型请求从历史构造：codex-rs/core/src/session/turn.rs，428–435、571–576 行。
- 对实际模型请求中的参考内容作验证：codex-rs/core/tests/suite/skills_extension.rs，1681–1708 行。
- 宿主 Skill 保留完整正文，不把所有来源统一截断：codex-rs/ext/skills/src/host_prompt.rs，76–96 行。
- 元数据单独预算：codex-rs/ext/skills/src/render.rs，128–154 行。
- 完整上下文硬上限独立于自动压缩策略：codex-rs/core/src/session/context_window.rs，58–110 行。
- 资源有界读取：codex-rs/ext/skills/src/provider.rs、provider/executor.rs。

本次把这些机制适配到现有 Python/LangChain 执行链，并未宣称直接复用了 Rust 内部库或已经切换到 Codex SDK。

## ① 内容保留

1. 完整 Skill 正文继续使用现有的 actor/owner/skill_id 范围缓存；参考资源新增按 actor/owner/skill_id/resource 隔离的快照。
2. 参考资源仅保留最终被工具中间件接受并完整返回的行。分页缺口和截断尾行不会被补成已读内容；读全前 complete=false。
3. loaded_skill_context 统一重建正文及参考资料。专家报告与报告修订使用它；ProjectSkillsMiddleware 在终稿、终稿修订和摘要后的请求中使用同一内容来源。
4. 只恢复当前执行和当前允许范围的内容，不把所有专家的参考资料合并进主助手，也不把 Skill 当作业务事实或额外权限。
5. 后台检查点只保存来源摘要、已读行和审计状态，不保存原始技能正文；恢复时重新核对注册路径、摘要、范围和全文加载声明。源文件变化时拒绝恢复。
6. 主助手的 owner 使用稳定的 research_scope_id，使后台新执行轮次能恢复同一逻辑任务的快照。
7. 检查点版本升为 23。版本 22 及更早文件保留，拒绝自动续跑，不增加旧格式兼容。

## ② 容量保护

阿里云官方模型卡片（2026-10-26 核对）：

- https://help.aliyun.com/zh/model-studio/qwen3-8-max
- https://help.aliyun.com/zh/model-studio/qwen3-8-flash
- 输出及思考参数语义：https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions

当前配置 MAIN/REVIEW 为 qwen3.8-max，SUB 为 qwen3.8-flash。两者上下文 1,000,000 token；普通最大输入 991,808，思考模式最大输入 983,616，最大回答输出 131,072，最大思维链 262,144。

- runtime.yml 增加明确的模型容量、8,192 token 安全空间、524,288 字节整请求预算、8,000 字节 Skill 目录预算。
- 输入估算采用完整 UTF-8 JSON 字节加消息/工具封装余量的保守估算，不使用 GPT tokenizer 伪装成 Qwen 的精确 token 计数，也不把它写成供应商实际计费。
- 模型调用前检查绑定的工具、消息和结构化输出 schema，超限时不扣模型调用额度。
- HTTP 请求发送 hook 再次检查 SDK 实际序列化的完整 body，并使用真实 body 字节数。被拒绝的请求不会记为已发送。
- 最大输入还需为输出、思考和安全空间让出容量。max_completion_tokens 按供应商语义计入完整生成长度；未指定输出上限时保守预留模型最大输出。思考启用但未指定预算时保守预留模型最大思维链，不猜测当前服务默认值。
- 容量不足抛出执行终止信号，不静默截断正文、参考资料或 schema，不换模型、不增加重试路径。
- Skill 单资源大小上限为 1 MiB；超大资料应拆成按需读取的资源。
- 独立审核使用既有证据边界，不自动注入整套业务 Skill；其请求同样经过容量检查。

## 定向执行验证

验证通过终端 stdin 执行，使用临时目录、既有框架、真实 ChatOpenAI 适配器与 httpx.MockTransport；未新建测试文件、未向真实模型接口发送请求。

| 场景 | 观察结果 |
|---|---|
| 同步和异步自动读取 | 实际 HTTP 请求中包含当前 Skill 正文和参考资源 |
| 同步和异步终稿重建 | 原消息历史替换后仍携带两类内容，工具关闭，输出 schema 保留 |
| 同步和异步首次报告及报告修订 | 独立报告请求携带当前专家快照，其他 owner 和主助手内容未混入 |
| 同步和异步真实框架摘要 | 各发生两次摘要；旧正文工具消息已移除，下一次 HTTP 请求仍包含完整快照 |
| 普通问答与显式选择 | 普通问答仅目录；显式选择只加载正文，不提前加载参考资源 |
| 分页及工具截断 | 未返回的行不进入快照，原始行号保留，补齐后才标记全文加载 |
| 被拒绝的工具返回 | 不写入快照，不产生加载声明 |
| 检查点 | 完整正文与参考资源跨执行轮次恢复；部分资源保持原范围；旧版本与变化源文件拒绝恢复 |
| 历史、工具定义、输出 schema 超限 | 预检拒绝，HTTP 发送数为 0，模型预算扣除为 0，账本记录 rejected |
| 异步超限 | 同样不发送、不扣额度 |
| HTTP 最终载荷超限 | 发送 hook 拒绝，MockTransport 未收到请求，transport 仍为空 |
| 目录及超大正文/资源 | 模型调用前拒绝；必需正文没有被静默截断 |
| 模型原生窗口 | 输出与思考预留改变可用输入容量；较大的思考预留会使超限请求被拒绝 |
| 计量记录 | 正常请求具有实际 transport；容量数值在脱敏持久化后仍保留数值类型 |

## 既有回归与限制

- test_llm.py + test_framework_contract.py + test_call_budget.py：61 通过、1 失败。失败是旧自定义专家 evidence 工具未执行的断言；未为此放宽工具注册与权限规则。
- 技能、框架、旧上下文合成、预算、治理和输出边界六组：43 通过、40 失败、39 个夹具错误。包括安全数据库迁移前置条件、HTTP 认证、旧注册表/协议断言等；部分测试仍引用已经移除的 SearchExecutionMiddleware.final_request。
- 恢复、持久化任务、契约、生命周期和取消五组：12 通过、51 失败。包含 JobService.open 缺 scheduler/run_store、RouteDecision 旧字段、安全数据库迁移前置条件，以及旧审核与生命周期断言。
- 上述集合存在重叠，不能相加作为独立测试总数。没有建立同版本完整修改前基线，不能据此声称没有新增失败或全系统回归通过。
- 未修改测试来隐藏失败，未自动迁移用户数据库，未进行真实 Qwen/Tavily/MySQL/Word/PDF 业务验收。业务验收仍按每次一题、汇报后停止执行。
- 本记录所在 docs/ 仍被现有 .gitignore 整体忽略；本轮没有扩展到第⑨项仓库规则修复。
