# 步骤 2：主助手直接分工与参数错误修复

日期：2026-10-28。项目根目录：D:/My_workspace/Multi-agent/Multi-agent。

依据 [37 号修复方案](37-basic-multi-agent-repair-plan.md) 的步骤 2 实施。当前代码能够直接向 db、document、search 分工；本次补齐生效 Skill 中的旧前置要求及委派参数错误边界。只验证本步与受影响的执行保护，不据此宣称基本多智能体的完整真实业务验收已经通过。

## 主要方案来源

本轮修改前读取 openai/codex 固定提交 970111f6cb62cc02fe36a82ea7d71b37a2889c97，采用其委派与错误边界机制，适配现有 Python、Deep Agents、LangChain、LangGraph 实现。

| 本项目问题或职责 | Codex 来源 | 本次采用方式 |
| --- | --- | --- |
| 模型描述分工，程序控制身份与执行关系 | [spawn.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs)、[thread_spawn.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/thread_spawn.rs) | 保留 task 的助手类型与完整任务说明；程序签发独立子任务 ID，绑定运行、父调用和助手定义；清除 Skill 中的研究计划登记前置要求 |
| 创建前解析参数，拒绝未声明字段 | [spawn.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs) 中 parse_arguments 与 deny_unknown_fields | 在读取目标元数据前完成参数校验；模型可见 Schema 显式禁止额外字段，运行时保留既有额外参数拒绝 |
| 可纠正错误返回模型，致命错误终止执行 | [function_call_error.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/tools/src/function_call_error.rs) | 缺字段、错误类型、未知目标等通过现有错误 ToolMessage 返回；不让进度上报或字典查找异常抢先终止运行；保留取消、预算终止和持久化错误边界 |
| 子助手使用当前运行权限 | [child_config.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/agent/child_config.rs) | 保留从 RunContext 绑定联网、材料、工具权限与预算，未知助手、禁网分工仍拒绝 |
| 计划只表示步骤与进度 | [plan_tool.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/protocol/src/plan_tool.rs) | 分工不要求原文字面名称、需求锚点或事实 ID；不重新增加计划授权层 |

Codex 源码是机制的主要来源。research Skill 的业务文案、既有工具名称及 Python 错误消息属于本项目适配；没有引入 Codex 内核、SDK、自动替代工具或另一套委派路径。

## 修改前的实际缺口

1. 生效的 skills/research/SKILL.md 仍要求主助手先登记 plan_research，搜索助手使用 research_step。通过真实 load_selected_skills / loaded_skill_context 读取后，两项废弃指令都会进入助手上下文；现有委派测试没有检查这些正文。
2. 主助手对模型提出的 task 直接索引 description；缺少字段时抛出 KeyError，模型收不到可纠正的工具错误。
3. HookToolMiddleware 在参数校验前按 subagent_type 查找字典；传入列表时抛出 TypeError，尚未执行任何工具即终止。
4. 缺少目标的拒绝未计入 task_rejections，无法可靠表达“尝试委派但没有取得有效结果”。
5. prompt/prompts.yml 已没有运行时代码或测试调用方，但保留完整旧计划、后台任务与报告协议；当前有效提示词位于 agent/prompts.py。

原始隔离复现经生产 run_deep_agent 与真实构建器执行：缺 description 得到 KeyError，目标为列表得到 TypeError，两项均执行 0 次工具。修复后同一命令均向主助手返回错误，随后由新的模型调用纠正参数，分别执行 1 次实际工具并只创建 1 个有效分工。程序没有自动重试错误委派。

## 实际改动

- agent/main_agent.py：只有字段类型可用于助手进度展示时才生成助手请求事件；其余模型请求作为原始工具请求记录，由执行边界校验，进度记录不负责执行授权。
- agent/runtime/hooks.py：去除校验前的目标字典查找；校验后确认目标、任务说明、允许助手与权限，再绑定来源类型；所有被拒绝的 task 都记录拒绝原因。
- agent/runtime/capabilities.py：task 的模型可见 Schema 禁止额外字段，继续只披露当前允许助手。
- skills/research/SKILL.md 及其来源参考：直接委派 search，明确使用 internet_search / read_web_page；保留来源、范围和未知项，引用使用实际查询或材料位置。
- 删除无调用方的 prompt/prompts.yml，保留 agent/prompts.py 作为有效提示词来源。

任务身份签发、材料绑定、模型及工具预算、顺序调度继续复用现有 delegation.py、subagent_factory.py 和运行上下文。未新增嵌套委派，不修改生产数据库、凭据、预算配置或真实验收记录。

## 验证证据与限制

证据目录：output/verification/step2-delegation-20261028/。

- pytest.xml：8 个相关文件首轮 192 项，191 通过、1 失败。失败为 test_stream_is_closed_when_parser_raises，仍依赖空 task 参数抛 KeyError；与本次可纠正错误契约冲突，不能计为通过。
- diagnostic-after.json：另外 7 项边界均通过，覆盖缺目标、对象类型目标、空说明、额外 prompt、未知助手、列表说明，以及不纠正错误时不生成成功文件。
- http-acceptance.json：真实认证 HTTP 入口、正式生产构建器、显式 research Skill、参数纠正、实际搜索工具、独立任务身份、成功返回及 Markdown 认证下载全部通过；主助手与搜索助手的已加载正文均无旧计划工具指令。
- 两项原始异常复现修复后通过；运行时 Skill 加载复现修复后通过。
- Ruff 与三个改动生产 Python 文件、一个授权调整测试文件的格式检查通过。
- skills-pytest.xml：Skill 相关回归 51 项全部通过。lifecycle-pytest.xml：授权调整的资源释放用例复验 1 项通过。regression-summary.json 按用例身份合并各阶段最终结果，共 243 项通过、0 失败、0 跳过；保留首轮 191 通过、1 失败的原始报告，不把初轮失败记成初轮通过。

现有用例验证三个助手分别及同回合提出分工、文档实际读取、SQL 只读及表白名单、禁网、来源归属、预算、超时、取消、历史事件和下载一致性。外部服务在隔离环境提供可控响应；不将其描述成 Tavily 或生产数据库的真实业务调用。

依据用户提供的 AGENTS.md 与 37 号方案第 7.2 节，先提供 test-migration-proposal.patch，收到用户“授权仅调整这个用例”后，仅修改上述一个用例的异常触发方式：进度解析主动抛出受控异常，继续验证流关闭、错误记录、无成功结果、会话与线程上下文释放。全部原有资源释放断言保留，未修改其他测试。

仅执行 git status；按项目限制未运行 git diff --check，使用实际字节与新增行空白审计。文本保持 UTF-8、既有 BOM 设置与 CRLF。历史文档及验证目录中的旧协议名称作为历史证据保留，不作为运行路径。

本轮没有重跑原第 1 道真实题。39 号记录中的 Tavily HTTP 432 失败仍是完整业务验收的未完成事项，不以本步离线通过替代。
