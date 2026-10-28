# 步骤 3：统一子助手执行与返回

日期：2026-10-28。项目根目录：D:/My_workspace/Multi-agent/Multi-agent。

依据 [37 号修复方案](37-basic-multi-agent-repair-plan.md) 的步骤 3 实施。复用三个现有助手、顺序调度、工具边界和受管理工作进程；本轮补齐统一返回、证据归属和失败信息。步骤 4 仅接通未完成事项对现有交付完成度的影响，不扩展其他汇总协议。

## 主要方案来源

修改前实际读取 openai/codex 固定提交 970111f6cb62cc02fe36a82ea7d71b37a2889c97 的以下源码，以其已有机制作为主要方案来源。

| 本项目职责或问题 | Codex 已有实现 | 采用方式与适配边界 |
| --- | --- | --- |
| 程序管理子助手身份和父子关系 | [spawn.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs)、[api.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/agent/api.rs) | 沿用程序签发的子任务 ID，接收工具记录时同时验证任务、运行、会话及原有完整性；其他分工的合法记录也不能借用 |
| 身份、执行状态和失败原因一同回传 | [api.rs 的 AgentTurnOutcome](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/agent/api.rs#L235)、[control/completion.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/agent/control/completion.rs) | 成功与普通失败共用返回结构；失败仍保留已经真实取得的来源，结果正文为空；取消及总超时记录结构一致的快照，并继续传播控制信号 |
| 从执行事件确定终态，不把一次等待结束当作任务完成 | [status.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/agent/status.rs)、[wait.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/tools/handlers/multi_agents_v2/wait.rs) | 区分有效返回、失败和停止原因；取消与成功登记使用同一把运行锁，停止后的正文不得发布为成功 |
| 执行资源由程序守卫负责清理 | [control/spawn_guard.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/agent/control/spawn_guard.rs) | 继续复用 ExitStack、调度租约和 ToolScope / OwnedProcess；没有另建执行器或后台清理路径 |

Codex 没有本项目的 Python 工具证据适配器、文档页表行覆盖模型，也不提供本项目的业务完成度判断。这些部分基于上述程序归属与终态机制，复用现有来源规范化、文档覆盖算法和交付检查完成适配。没有引入 Codex Rust 内核、SDK、框架升级、嵌套委派或替代执行路径。

## 修改前的实际缺口

通过真实 DelegationRunnable、HookToolMiddleware 和受控工具执行获得以下复现，证据为 output/verification/step3-execution-20261028/diagnostic-before.json。

1. 成功返回包含正文和来源，失败返回只有身份、状态和错误；缺少统一结果字段及未完成事项。
2. 同一分工先取得有效工具结果、后遇到工具拒绝，仍返回 completed，主助手及交付看不到该拒绝形成的缺口。
3. 已取得来源后发生请求超时，工具证据文件存在，但该分工的 sources 为空，主助手也收不到已取得的来源。
4. 同运行另一分工的完整、有效记录可以被接收为当前分工的来源，因为原有验证只确认来源所属任务存在，没有确认是当前任务。

## 实际修改

- agent/results.py：统一结果序列化；先验证每条记录属于当前分工，再使用原有完整性和来源校验。按真实工具记录生成 unfinished，保留 limitations；同调用成功重试可消除此前失败尝试形成的缺口，不增加任何自动重试。
- agent/runtime/delegation.py：成功和失败调用同一序列化入口；失败保留已取得的合法来源和原始证据，清除成功正文。身份登记和成功提交与停止操作共用 budget_lock；同步、异步路径使用相同判断。
- agent/runtime/evidence_protocol.py：工具记录保存脱敏错误原因，并将该字段纳入现有完整性指纹。任务结果不再只能知道“失败”而看不到原因。
- agent/subagent_factory.py：三个助手统一直接返回正文、真实来源和未知项；程序管理的身份、状态和来源归属不要求模型填写。
- agent/runtime/workflows.py：将有效分工中的 unfinished 显示到最终答复并影响部分完成状态，避免主助手忽略错误后仍交付为全部完成。

方案中列出的 reporting.py 当前已不存在；不为对齐文件名重建无用模块。顺序执行仍由现有 SerialDelegationMiddleware 和每运行 concurrency=1 的 child_gate 实现，继续复用全局子助手及专家资源池。

统一返回字段为 task_id、expert、status、result、sources、unfinished、limitations、error、error_category。来源增加实际来源键、检索方式和 provenance，保留既有 URL、位置、输入材料等字段。

单次请求超时保留既有 status=failed，并通过 error_category=timeout 明确原因；总时限保持 timed_out。取消及服务中断继续使用原有 cancelled / interrupted 状态，不将控制信号转成可继续执行的模型反馈。成功返回的 status=completed 表示执行取得有效结果；存在 unfinished 时最终交付仍是 partial。

文档能力说明与真实未读内容分开处理。对同一分工内同一路径、内容摘要和解析器的分段读取，复用现有范围覆盖算法累积；完整读取后不再将先前目录或选区读取误算为未完成。其他分工的读取不能补齐当前分工；材料原有 missing 仍保留。此处不保证全部自然语言结论的正确性，也不代替模型说明未知业务事项。

## 首轮验证证据（续验前的历史记录）

证据目录：output/verification/step3-execution-20261028/。

- baseline-pytest.xml：修改前 43 项，41 通过、2 失败。失败均为现有测试以 Windows 默认 GBK 读取 UTF-8 结果文件，未进入后续证据断言；没有将其当作源码回归。
- intermediate-pytest.xml：初版 242 项，240 通过、2 失败。失败发现把完整文本的能力说明错误计入未完成事项；已修正分类，并保留该轮失败记录。
- diagnostic-final.json：最终源码下直接执行生产委派和工具边界，检查统一字段、实际工具调用、错误与超时的来源保留、跨任务来源拒绝、取消、总超时、排队取消、迟到正文拒绝和调度资源归零；实际读取临时工作簿，检查部分读取、分段累积、材料位置和跨任务覆盖隔离。
- http-acceptance.json：正式认证 HTTP 入口、真实主助手与子助手构建器、实际搜索工具边界、受控外部返回；检查主助手接收的完整结构、分工身份、真实来源、拒绝记录、partial 完成度、缺口正文、认证下载字节一致及资源释放。
- pytest.xml：最终源码下 9 个相关文件共 242 项全部通过，0 失败、0 错误、0 跳过；覆盖正式三助手顺序执行、材料与来源归属、禁网、数据库只读及表白名单、预算、工具超时、取消、工作进程回收、事件持久化与下载。Ruff 和 5 个生产 Python 文件的格式检查均通过。
- workspace-audit.json：UTF-8、原有 BOM、CRLF、变更行空白、生产源码指纹及测试未修改的检查结果。

最终回归显式设置 PYTHONUTF8=1，通过 uv run --offline --no-sync python -m pytest 执行。默认 GBK 下两项现有测试的隐式解码依赖仍存在；本轮未新增或修改测试，不修改生产 JSON 的 UTF-8 编码来适配该测试问题。

所有诊断都在临时工作目录执行，HTTP 场景显式迁移临时数据库并认证；外部服务提供受控响应，文档场景使用真实文件解析工作进程。没有访问生产凭据或生产数据库，没有重跑原第 1 道真实业务题。此前 Tavily HTTP 432 失败仍是完整真实业务验收的未完成事项。

仅执行 git status，没有运行其他 Git 命令；按限制以原始字节和变更行检查代替 git diff --check。文本保存为 UTF-8、保留已有 BOM 设置并使用 CRLF。未改写历史数据库或此前验收结果。

## 获准三项问题的续验与补修

用户批准的三项问题保持不变：三助手订单核对、检索后正文超时的部分交付、执行中取消及迟到返回。只使用这三项业务问题，不增加简单问答。第一项通过后才进入第二项，第二项通过后才进入第三项；本地执行边界诊断和已有回归不冒充真实模型验收。

续验中的真实缺陷和本轮修改：

- 上传文件与绑定文本能力混淆：`agent/runtime/capabilities.py` 按实际绑定材料生成参数枚举，没有绑定文本时不披露 `read_bound_material`，运行入口保持相同权限判断。`agent/subagents/definitions.py` 说明上传文件直接使用 `read_document`，已有原文、位置和范围不需再当作文本引用重读。只读文件工具同时明确 glob 的目录与相对模式用法。
- 数据库结构接口信息不足：原 `get_table_data` 只给 CSV 表头和样本，助手为回答“表结构”追加查询受禁止的系统库。`tools/db_tools.py` 现在复用同一只读工作进程和已验证权限的游标，执行程序固定的 `SHOW FULL COLUMNS FROM` 与有界 SELECT，返回真实字段名、类型、可空性、键、默认值、注释和样本。不开放任意 SHOW 或系统库查询，不扩大表白名单。数据库助手根据已返回字段完成所派查询，未知业务定义明确保留。
- 主助手自行指定未披露的数据库方法：运行完成后读取隔离检查点，确认主助手在分工中写入 DESC、SHOW CREATE TABLE、系统库、猜测字段及额外状态过滤，并在失败后重复委派。该次主助手没有读取 Skill，不能将此归因于 Skill。`agent/runtime/capabilities.py` 现在从专家实际注册且当前可用的工具生成委派能力说明；`agent/prompts.py` 明确分工范围、顺序与次数由用户要求决定，具体方法和参数由专家根据实际工具与结果选择。没有硬编码验收题答案或禁止所有后续委派。
- 工作进程超时分类丢失：worker 已回传 `timed_out=true`，但父进程异常类型变为 `WorkerOperationError`，旧 hook 只认 `TimeoutError`。`agent/runtime/hooks.py` 改为复用 `is_transport_timeout`，执行事件和子任务结果均明确报告 timeout；重试策略仍按原有独立分类执行，本轮验收配置为零重试。

具体修改前继续查看 openai/codex 固定提交 970111f6cb62cc02fe36a82ea7d71b37a2889c97：

| 具体缺陷 | 已查看的 Codex 实现与采用方式 |
| --- | --- |
| 向模型披露与实际可用资源不符的工具 | [spec_plan.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/tools/spec_plan.rs)、[registry.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/tools/registry.rs)：根据实际上下文和配置构建工具集合，执行时仍校验；适配为现有中间件的材料枚举与能力收窄 |
| 主助手需要知道专家实际能力及分工边界 | [role.rs 的 spawn_tool_spec](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/agent/role.rs#L271)、[multi_agents_spec.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/tools/handlers/multi_agents_spec.rs)：从真实角色配置生成委派说明、描述有限职责；本项目进一步复用已注册工具的说明披露具体能力 |
| 区分可纠正工具错误与终止错误 | [function_call_error.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/tools/src/function_call_error.rs)：继续保留明确失败，不能靠忽略被拒绝的操作将 partial 改成 complete |
| 跨执行边界保留超时原因 | [exec.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/exec.rs)、[exec_output.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/protocol/src/exec_output.rs#L46)：采用显式超时标记，不依赖异常名称或错误文本猜测 |

所查看及搜索的 Codex 源码没有本项目 `get_table_data` 的 MySQL 结构读取适配器；该处在以上工具与权限机制下修改现有实现，SQL 元数据字段依据 [MySQL 官方 SHOW COLUMNS 文档](https://dev.mysql.com/doc/refman/8.4/en/show-columns.html)。没有将领域 SQL 改动声称为 Codex 现成实现。

验证方式本身的修正：此前我的证据观察程序在应用运行中打开 JSON 文件，Windows 读取句柄未共享删除权限，阻止应用通过原子替换保存同一文件。续验改为等待运行终态、执行任务完成、调度资源归零及工作进程关闭之后才收集应用 JSON，并使用短的临时运行目录。这是验证程序的错误，历史失败不计为业务成功。没有通过忽略写失败或修改生产文件持久化规则规避它。

本轮证据目录为 `output/verification/step3-ordered-validation-20261028/`。`final-regression.xml` 记录 5 个相关文件共 153 项现有回归全部通过；随后主助手范围与能力披露补修后，`parent-scope-regression.xml` 对两个受影响文件的 25 项回归再次全部通过（属于前述回归的子集，不累加为独立用例）。未新增或修改 tests/ 文件。`schema-rpc-diagnostic.json` 检查真实元数据、样本上限、越权拒绝、只读不变和进程回收；数据库使用隔离只读 SQLite，并以游标适配实际 MySQL 元数据语句，不能视为生产 MySQL 验收。`timeout-native-rpc.json` 与 `timeout-delegation-boundary.json` 使用实际工作进程和生产委派/hook，验证超时标记、已有来源、失败正文及资源释放；未请求模型。

真实模型逐次结果、运行 ID、未完成项及最终源码匹配情况见[本轮续验报告](../../output/verification/step3-ordered-validation-20261028/report.md)。早期失败、断言诊断和取消通过记录全部保留，不覆盖成成功；续验未完成前不得宣称三项验收或整个步骤 3 已通过。

最终状态：本轮三项获准验收已按 1→2→3 顺序全部通过，运行 ID 分别为 `fb8308e8-b4ef-448b-a01f-0b34d748e380`、`9774bd56-b38b-49a3-a7b8-36c3fde661c0`、`1d82e8c3-457a-410d-948c-83a27fae6e31`。三项使用同一份最终生产源码，实际业务结果、超时归类、来源保留、Windows 进程回收、迟到结果拒绝及下载均经证据核对。10 个生产 Python 文件的 Ruff/格式检查与 12 个变更文件的 UTF-8、BOM、CRLF 和变更行空白检查通过；文档最终保存后重新检查。
