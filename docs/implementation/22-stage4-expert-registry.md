# 22 · 第四阶段：专家定义与公开框架接口

日期：2026-10-24（Asia/Shanghai）。

## 实施范围与结果

本阶段将专家接入、模型角色、输入输出协议、工具、技能、委派权限、审核和资源分组放入不可变定义；工厂只负责校验和编译。默认生产专家仍为 db 与 search，第三种专家通过隔离测试接入验证，不凭空增加业务角色。

修改保留在工作区，未暂存或提交。保留前三阶段已有改动；未启动生产服务、修改生产数据库或执行真实模型/检索问答。对外身份认证与资源归属不属于本阶段。

## 先核对的开源代码

| 已阅读代码 | 具体方法 | 本项目落地 |
|---|---|---|
| [Deep Agents SubAgent / CompiledSubAgent](https://github.com/langchain-ai/deepagents/blob/a3381668c4d3b558742101fa7e6b8fe4a64e56ef/libs/deepagents/deepagents/middleware/subagents.py#L28) | 声明 name、description、system_prompt、tools、model、middleware；也接受预编译 runnable，通过 task 路由并隔离子图消息。 | 用 SubagentSpec 表达完整能力，用公共 SubAgentMiddleware 接入已编译、受控的 DelegationRunnable；工厂不按 db/search 名称选择实现。 |
| [smolagents managed agent setup / validation](https://github.com/huggingface/smolagents/blob/96f33faaf028479119ec8d34507b47694cf14e34/src/smolagents/agents.py#L210) | 初始化 managed_agents，检查名称和描述；统一验证工具与 managed agent 的名称冲突。 | 构建前拒绝重复 ID/显示名、保留名称、工具重名和专家/工具冲突；补充本项目的委派 DAG、审核、网络权限和配额约束。 |
| [Deep Agents 摘要中间件](https://github.com/langchain-ai/deepagents/blob/a3381668c4d3b558742101fa7e6b8fe4a64e56ef/libs/deepagents/deepagents/middleware/summarization.py#L279) | 通过公开摘要中间件管理上下文，触发与保留策略属于摘要配置。 | 不直接调用已安装版本的私有默认值计算函数，显式传入公共构造参数。摘要请求继续经过 BudgetChatModel 和第三阶段请求计量。 |

实际环境仍使用已安装的 Deep Agents 0.4.3，未升级依赖。该版本公共 SummarizationMiddleware 构造器会返回框架内部实现，因此测试验证公共参数与实际执行效果，不读取其内部 trigger/keep 属性。上游固定提交用于设计核对，不等同于声称本地已安装相同提交。

序列委派、任务证据转交、资源槽交接和专家契约锁定是本项目的实现，未声称上述项目直接提供了相同机制。

## 定义、编译和调用链

主要文件：agent/subagent_registry.py、agent/subagents/definitions.py、agent/subagent_factory.py、agent/runtime/experts.py、agent/runtime/delegation.py。

1. definitions.py 声明默认专家；注册表校验定义，保持只读映射。删除原 database_query_agent.py、network_search_agent.py 中的旧构造实现及生产引用。
2. 仅在初始化时为定义引用的模型 profile 创建客户端。模型配置来自 LLM_<PROFILE>_MODEL / BASE_URL / API_KEY，可显式提供 EXTRA_BODY；缺配置拒绝启动，不自动借用其他角色配置。
3. 工厂校验 BaseChatModel、零 SDK 重试、显式不支持的 structured_output/tool_calling 能力，以及已知上下文长度与摘要阈值；未知模型能力不代表已通过真实供应商验证。
4. 工厂依据定义的 tools、allowed_skill_ids、delegate_targets、summary 构造子图；父助手只能调用注册目标，子专家只能调用自己的 delegate_targets。
5. 子专家先收集真实工具依据，再生成 quote_id 报告；所有专家必须显式启用语义审核与 grounded_report。只有审核后的事实报告可返回/发布，不能配置为跳过审核。
6. 结果记录的 source 为真实 agent_id；source_kind 是工作流类别（db/search/other），二者分开。事件和界面名称来自定义，不要求修改中央名称表。

当前明确支持 task / planned_research 输入，以及 fact_report 输出；不支持的协议直接拒绝。planned_research 必须提供受控 research_step 且声明网络权限。自定义工具的实现由服务端开发者审核注册，不能把能力声明当作任意工具代码的网络沙箱。

SubagentSpec 同时定义：

- ModelConfig：执行模型与审核模型的显式 profile，SDK 重试固定为零。
- ReviewConfig：审核、所需工具/字段、来源要求和最多一次修订；fact_report 必须使用引文登记协议。
- ExecutionConfig：输入输出、工作流来源类别、网络权限、是否接收已审核内部资料。
- ResourceConfig：资源组、并发数、队列容量；同组必须采用一致限额。
- SummaryConfig：显式 token 触发、消息保留及工具参数截断设置。
- allowed_skill_ids / delegate_targets：闭合的技能和委派名单。

新增专家只需提供合法定义、真实工具、明确模型 profile 和审核策略，并加入 definitions.py 的集合；工厂、结果库来源枚举和显示名称表无需增加分支。需要新增业务协议或工作流类别时必须显式实现，不能用未知配置自动走默认逻辑。

## 委派、证据和资源

委派图禁止循环、未知目标和经子专家提升网络权限。嵌套委派要求 task 是当前模型轮次唯一工具调用；同时设置 parallel_tool_calls=False，并检查模型实际输出，违规时在工具执行前拒绝。

一个嵌套调用分支共享服务级/本轮子任务槽，避免父专家等待子专家时占满唯一执行槽。父专家等待期间释放自己的资源组槽；子专家取得其组槽，完成后释放，再恢复父专家组槽。并行顶层分支仍受原有服务级、本轮和账户限额约束。队列满直接拒绝，取消释放排队项与已持有资源；运行中不能改变正在占用的资源组限额。

子专家只向授权父专家转交实际收集的工具证据，标注 delegated_from 并按调用/attempt 去重。父专家自行登记任务所属 quote_id，重新生成和审核报告，不能直接借用子专家的引文 ID 或把子模型陈述当成工具证据。

普通 task 专家为原始需求建立 required=false 支持项，避免每次委派重复增加终稿完成门槛；原始必需需求仍由完整需求注册表判定。

## 私有依赖清理

- 主助手使用公共 create_agent 和明确列出的 TodoList / Filesystem / SubAgent / Summarization / PatchToolCalls 中间件，保留现有任务契约、审核、技能、Hook、checkpointer。
- 根图和子图均不直接使用 _compute_summarization_defaults；不隐式创建 general-purpose 专家。
- 项目技能使用公共 AgentMiddleware、AgentState、PrivateStateAttr、SystemMessage，自行解析已注册目录的 YAML 元信息；不导入框架私有技能解析器或 system-message helper。
- GuardedFilesystemBackend 实现公共 BackendProtocol，组合公共 FilesystemBackend。路径约束由项目执行，不继承框架的 _resolve_path / _to_virtual_path 等私有方法；技能挂载保留只读、范围和读取审计。
- 手工回归脚本随延迟模型工厂调整，HTTP 观察使用公开 http_async_client.event_hooks，不再访问底层 _client。该脚本本阶段未实跑。

## 存储与恢复

- 结果库 schema 升为 7：事务内重建 results，仅移除固定 source 枚举，保留已有数据、显式索引、唯一性约束、current_result_id 和外键完整性。新结果仍校验 subtask/run/thread/event 归属和 source=task.target。
- 隔离数据库验证了有已发布结果和当前指针的 v6→v7 迁移；未打开或迁移生产数据库。
- checkpoint 升为 15，审核缓存契约为 review-v12-expert-contract。保存专家定义清单；恢复时若提示、工具 schema、模型角色、技能、权限或资源配置改变，明确 interrupted 并保留快照，要求新建运行。
- 旧 checkpoint 不以兼容分支继续执行；既有结果/审核原文按历史记录保留。

## 必要验证

用户明确授权本阶段相关代码测试。没有新增模型常识问答题，使用可控模型响应和真实框架工具节点、权限中间件、审核及隔离 SQLite 验证系统行为。

后端联合命令：

    uv run --offline --no-project --python .venv\Scripts\python.exe python -B -X utf8 -m pytest tests/test_stage4.py tests/test_llm.py tests/test_framework_contract.py tests/test_agent_lifecycle.py tests/test_call_budget.py tests/test_skill_loading.py -q --tb=short -p no:cacheprovider

结果：158 passed in 207.03s。其中第四阶段模块 33 项、模型配置 32 项、框架/生命周期/预算 42 项、技能 51 项。日志：[22-tests.log](22-tests.log)。

验证覆盖第三种专家同步/异步完整工具→引文→审核→落库链路；同组/跨组单执行槽嵌套委派；取消、队列拒绝和资源释放；非法定义/委派/模型能力配置拒绝；定义变化中断恢复；历史库迁移；公开技能读取、摘要调用计量及禁止调用私有摘要函数时的主助手构建。

前端 node --test 三组脚本全部通过：app、call-groups、run-events。增加第三种专家名称和调用归属断言；把早期“根据审核日志推导完成”的旧夹具改为服务端 coverage 契约，保留 unknown/partial/complete 区别。日志：[22-frontend.log](22-frontend.log)。

本轮发现的早期失败分别来自旧工厂签名/关闭审核的测试夹具、摘要内部属性断言、遗漏测试运行配置，以及旧前端状态协议；均按当前契约修正，没有为通过测试恢复旧生产分支。

静态检查：33 个相关 Python 文件 Ruff lint / format、Python AST、提示 YAML、JavaScript 语法，以及相关文件 UTF-8/BOM/CRLF/尾随空白检查，见 [22-static.log](22-static.log)。项目只允许 git status，因此没有执行 git diff --check，使用实际字节和逐行检查。

仅执行以上相关检查，未运行全仓库测试。其他历史测试仍有早期报告/覆盖协议夹具，需要按当前协议另行迁移，不能据本次结果声称全仓库测试通过。真实供应商协议、真实数据源和两道业务题的模型效果未在本阶段验收。
