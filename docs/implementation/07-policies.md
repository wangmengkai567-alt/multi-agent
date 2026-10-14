# 第 07 步：工具执行规则与整轮调用限制

日期：2026-10-14（Asia/Shanghai）。状态：第 07 步完成，离线验收通过。

## 修改前状态与操作约束

实施目录：`D:\My_workspace\Multi-agent\Multi-agent`。修改前执行 `git status --short`，输出为空。

遵守上层 `D:\My_workspace\Multi-agent\AGENTS.md`：只通过 `git status` 查看状态，未执行其他 Git 操作；所有代码、配置和文档修改保留在工作区，由用户自行提交。此规则优先于实施计划中的“每步提交”。未修改 `.env`、真实数据库、用户上传文件或旧轮次输出。

## 集中配置与实际接入

新增 `config/runtime.yml` 和 `agent/runtime/policies.py`。配置通过严格 Pydantic 类型、字段、范围及目录校验，在服务启动和 Agent 初始化时加载，非法配置在创建模型或 checkpoint 数据库之前拒绝。每轮绑定启动时的配置快照，无用户可关闭的权限开关。

| 设置 | 默认值 |
|---|---|
| 整轮总调用预算 | 80 |
| 主模型 / 子模型 / 自检模型 | 20 / 30 / 5 |
| 工具 / 重试 | 40 / 6 |
| 允许数据库表 | orders |
| 最大结果行数 / SQL 字符数 | 100 / 12000 |
| 数据库连接、读取、写入超时 | 各 10 秒 |
| 单次上传文件上限 | 10 MiB |
| Skill 只读来源 | 项目 skills/，虚拟路径 /skills/ |
| 单次工具调用最多重试 | 1 次，间隔 0.25 秒 |

`before_run` 初始化本轮策略；`before_tool` 依次校验参数、SQL 与路径权限，然后原子预留次数，最后才进入业务 handler。拒绝返回原 call_id 的错误 ToolMessage，不执行 handler。模型调用在真正请求底层模型之前预留，防止反复被拒绝的工具请求造成无限模型循环。

## SQL 的分层控制

新增并安装直接依赖 `sqlglot==26.33.0`，同步更新 requirements 和 Windows 开发锁文件，没有升级原框架。

SQL 按 MySQL 方言解析为 AST，检查单条 SELECT / UNION 查询；逐个作用域检查真实表来源，区分 CTE 和派生表，连未使用的 CTE 中的越权表也拒绝。检查整棵树，拒绝写语句、跨库访问、变量与赋值、锁、递归 CTE、导出文件、注释/优化器提示和不在安全集合中的函数。通过后重建 SQL 并添加或压低顶层 LIMIT；不会把未经校验的原始 SQL 直接交给数据库。`get_table_data` 的表名必须精确匹配配置，不再插值任意表名。

数据库只读取 `MYSQL_READONLY_USER`、`MYSQL_READONLY_PASSWORD` 与 `MYSQL_DATABASE`；不会回退使用旧 `MYSQL_USER` / `MYSQL_PASSWORD` 或管理员账号。每次连接在业务查询前执行 SHOW GRANTS，接受指定数据库中的 SELECT / SHOW VIEW 和全局 USAGE，拒绝写权限、全局 SELECT、授权转授、角色及无法识别的权限格式。此版本不尝试展开复杂角色来猜测其是否只读。

核验通过后设置会话 MAX_EXECUTION_TIME、固定预期 SQL 字符串模式，再启动只读事务。连接、读写 socket 都配置超时，查询结果使用 `fetchmany(max_rows)`，CSV 使用标准 csv 模块处理逗号与换行。游标、事务与连接有明确清理路径；错误抛给 Hook 分类，不伪装为成功查询文本。

`list_sql_tables` 只对配置表逐个参数化核对，不返回其他表。新增 `sql/read_only_user.example.sql` 供管理员人工配置，包含明确的密码占位符；本步未执行此 SQL、未创建或改动真实账号。.env.example 已改为只读账号字段。

实现依据：SQLGlot 的 [AST 文档](https://github.com/tobymao/sqlglot/blob/main/posts/ast_primer.md) 与 [作用域源码](https://github.com/tobymao/sqlglot/blob/main/sqlglot/optimizer/scope.py)；MySQL 的 [SHOW GRANTS 文档](https://dev.mysql.com/doc/refman/8.0/en/show-grants.html) 与 [执行时间控制文档](https://dev.mysql.com/doc/refman/8.0/en/optimizer-hints.html)。运行契约以已安装版本和离线测试为准。

## 文件与旁路入口

`utils/path_utils.py` 统一导出 `utils/secure_paths.py` 的严格路径规则，移除无上下文时读取任意 CWD 和自动跳转到 updated/ 的旧行为。解析后必须位于明确允许的目录；拒绝上级跳转、UNC、盘符相对路径、NTFS 数据流、Windows 保留名称、符号链接和 junction，包含根目录祖先中的重解析点。

新增 `agent/runtime/backend.py`，为主 Agent 和三个子 Agent 提供按当前 RunContext 创建的受控文件后端。默认框架文件工具现在操作本轮实际工作目录，避免同会话 checkpoint 中的旧文件状态绕过本轮隔离。没有脚本执行后端；execute 也会被策略拒绝。读取、搜索和遍历均过滤越界链接。

- 用户上传快照放在本轮 `uploads/`，源文件只读；Skill 从独立 `/skills/` 挂载只读。用户上传的 SKILL.md 不会变成任务说明。
- 写入、编辑以及后端 upload_files 均拒绝修改 Skill 或用户源文件；生成报告仍写入本轮输出目录。
- 上传接口校验会话标识和全部文件名，拒绝重复文件名、越界路径及超限文件，先暂存整批文件，再更新正式上传文件。
- 下载、目录列表与 `/output/{path}` 共用相同的解析和重解析点检查；移除宽松的静态 output 挂载，合法输出仍可下载，越权请求返回明确 HTTP 错误。
- 本轮主提示词区分业务工具的相对路径与框架工具的虚拟路径，明确 uploads/ 与 /skills/ 只读。
- Markdown/PDF 写入显式使用写权限检查。PDF 转换的临时 HTML 放在可写输出目录，使用独立隐藏的 Word 实例并清理；禁止可能让 Word 读取目录之外内容的嵌入资源、活动 HTML 和本机文件链接。
- 文件读取与转换依赖错误抛给 Hook，避免把错误字符串当作成功结果。

这是服务内的路径权限控制，不宣称提供操作系统级沙箱或抵御其他本机进程同时修改文件系统的能力。第一版 PDF 支持文本、表格和网页引用；嵌入图片与活动 HTML 会明确拒绝。

## 预算、重试和结果记录

RunContext 的 RLock 保护所有分类计数。每次原子预留检查分类与总预算；多分类预留整体成功或整体拒绝，不存在半扣数。次数在请求开始前扣除，失败或取消不会退还已启动请求的次数。

新增 `agent/runtime/models.py` 的 BudgetChatModel，主 Agent 显式用 main_model，三个专家显式用 sub_model；自检调用可用 review 包装。框架自动摘要使用同一个包装后的模型，也受分类与总预算约束。自检模块仍待第 09 步，这里只提供并验证其受控调用入口。模型 SDK 内部重试设置为 0，避免隐式额外请求漏计。

工具首次调用预留 tool；每次重试在 handler 前同时预留 tool 和 retry，因此一个工具第一次失败、重试成功时总预算占用 3（tool=2、retry=1）。这就是本版整轮总预算的口径。主模型重复越序或非法请求虽然不执行工具，模型请求本身仍计数。

重试仅对配置中的幂等查询工具开放，只接受明确临时网络故障、超时、部分 HTTP 临时状态和 MySQL 连接错误。非法参数、权限、缺配置、未知异常及标准错误 ToolMessage 不进行猜测重试。RAG 创建临时会话的工具不在重试名单中。Tavily 移除原来对所有异常重试的装饰器，单次请求显式超时并关闭客户端；RAG 客户端按需构建，HTTP 请求也有超时并保留异常。

每个实际尝试都经过 before_tool / after_tool，并保存 attempt、错误类别、执行标志和耗时；成功、拒绝和取消仍清理调用上下文。整轮结束事件增加 call_counts，成功和失败结果都能从历史记录读取最终计数，不新增数据库表或改变 SQLite schema。

## 验证与实际输出

新增 `tests/test_tool_policies.py` 与 `tests/test_call_budget.py`，共 116 项测试，包括不安全 SQL、嵌套/CTE/UNION 越权、只读账号与缺配置、行数与超时适配、文件路径与 Windows junction、Skill/上传只读、真实框架文件工具和 task 路由、HTTP 文件入口、临时故障重试、并发原子预算、实际模型包装、自检分类与框架摘要模型、PDF 假 COM 清理。

此前测试随新契约更新：上传快照路径增加 uploads/；路径拒绝断言明确验证 PathDenied；生产构建测试核对三个助手的模型与工具计数；终态事件断言保留答案/错误并验证新增 call_counts。

从项目根目录执行：

```powershell
& '.\.venv\Scripts\python.exe' -m pip install sqlglot==26.33.0
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
& '.\.venv\Scripts\python.exe' -m pytest tests/test_tool_policies.py tests/test_call_budget.py -q --tb=short
```

```text
Successfully installed sqlglot-26.33.0
116 passed in 6.40s
```

联合 Hook 检查：

```powershell
& '.\.venv\Scripts\python.exe' -m pytest tests/test_tool_policies.py tests/test_call_budget.py tests/test_hook_pipeline.py -q --tb=short
```

```text
141 passed in 14.51s
```

```powershell
& '.\.venv\Scripts\python.exe' -m pip check
& '.\.venv\Scripts\python.exe' -c "from importlib.metadata import version; print('sqlglot=' + version('sqlglot')); from agent.runtime.policies import load_runtime_policy; p=load_runtime_policy(); print('runtime_config=valid'); print('total_budget=' + str(p.budgets.total))"
```

```text
No broken requirements found.
sqlglot=26.33.0
runtime_config=valid
total_budget=80
```

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q --tb=short
```

```text
208 passed in 53.84s
```

改动涉及的 Python 文件检查：

```powershell
& '.\.venv\Scripts\python.exe' -m ruff check agent/runtime agent/main_agent.py agent/llm.py api/server.py tools/db_tools.py tools/markdown_tools.py tools/pdf_tools.py tools/upload_file_read_tool.py tools/tavily_tool.py tools/ragflow_tools.py utils/path_utils.py utils/secure_paths.py utils/word_converter.py tests/test_tool_policies.py tests/test_call_budget.py tests/test_hook_pipeline.py tests/test_runs.py tests/test_run_concurrency.py tests/test_events.py
& '.\.venv\Scripts\python.exe' -m ruff format --check agent/runtime agent/main_agent.py agent/llm.py api/server.py tools/db_tools.py tools/markdown_tools.py tools/pdf_tools.py tools/upload_file_read_tool.py tools/tavily_tool.py tools/ragflow_tools.py utils/path_utils.py utils/secure_paths.py utils/word_converter.py tests/test_tool_policies.py tests/test_call_budget.py tests/test_hook_pipeline.py tests/test_runs.py tests/test_run_concurrency.py tests/test_events.py
```

```text
All checks passed!
25 files already formatted
```

全套 208 项没有失败、跳过项。最终格式检查曾提示新增终态断言需要格式化，已运行 Ruff 修复并复查通过。未运行 `git diff --check`，因为用户只允许 `git status`；没有执行暂存、提交、推送或其他 Git 操作。

初次全套回归中，旧 PathDenied 断言为 1 failed / 195 passed；加入最终计数后，旧终态 data 严格相等断言为 1 failed / 207 passed，均已按新行为修正。补充 PDF 只读来源测试也发现内部重新校验已解析 Skill 绝对路径时被误拒绝，已在允许目录内保留只读权限并重新校验。初次代码检查的未使用导入与格式问题已修复，未忽略失败。

真实模型、MySQL、Tavily、RAGFlow 和 Microsoft Word 联调：未执行。测试使用假模型、假客户端、假数据库连接和假 COM，真实 framework、临时 SQLite、临时文件和 ASGI HTTP 路由参与执行，不使用真实密钥，不触发付费服务或启动后台业务服务。

## 后续范围与使用要求

使用数据库查询前，需由管理员配置经过核验的只读账号，再填写新的只读环境变量；缺配置时工具明确拒绝。表名单可修改 runtime.yml 后重启服务，禁止通过放宽 SQL 读写规则来绕过验收。

本步完成的是工具、文件入口和调用预算控制。完整子 Agent 注册表、自检业务逻辑、真实 Skill 选择/加载、业务流程前置条件、整轮超时和取消接口仍按第 08–13 步实施。允许的 SQL 是保守子集，未支持的只读语法也会拒绝，不宣称兼容全部 MySQL 查询。
