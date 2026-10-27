# MultiAgent Search

基于 Deep Agents、LangChain 和 LangGraph 的多智能体查询与分析项目，支持联网搜索、MySQL 只读查询、上传文档分析和报告生成。任务协调助手按需调用数据库查询助手与联网搜索助手，通过 Skill 组织任务流程。

## 环境要求

Windows、uv。项目使用 uv 管理 Python 3.11；交付支持正文和 Markdown，不提供文件格式转换。

## 快速启动

在项目根目录使用 PowerShell。

**1. 首次安装**

先确认 `uv --version` 可用。已有 `.venv` 无需重复创建，已有 `.env` 请保留。

```powershell
uv venv --python 3.11
uv pip install -r requirements-dev-win-py311.lock
Copy-Item .env.example .env
```

**2. 编辑 `.env`**

填写模型服务地址、密钥和实际可用的模型名称；使用联网搜索时还需配置 Tavily 密钥。

```dotenv
# Main agent
LLM_MAIN_MODEL=your-main-model
LLM_MAIN_BASE_URL=https://api.openai.com/v1
LLM_MAIN_API_KEY=your-main-api-key
LLM_MAIN_EXTRA_BODY=

# Search and database agents
LLM_SUB_MODEL=your-sub-model
LLM_SUB_BASE_URL=https://api.openai.com/v1
LLM_SUB_API_KEY=your-sub-api-key
LLM_SUB_EXTRA_BODY=

# Subtask and final-answer review
LLM_REVIEW_MODEL=your-review-model
LLM_REVIEW_BASE_URL=https://api.openai.com/v1
LLM_REVIEW_API_KEY=your-review-api-key
LLM_REVIEW_EXTRA_BODY=
TAVILY_API_KEY=your-tavily-api-key
```

MySQL 为可选功能：需要时填写 `.env.example` 中的 `MYSQL_*` 配置，`MYSQL_DATABASE` 填已有库名，并在 `config/runtime.yml` 的 `sql.allowed_tables` 中配置允许查询的表名。不使用时保留账号、库名为空，表白名单为 `[]`。

**3. 配置访问认证并离线迁移**

在 `.env` 中设置独立的 `APP_ACCESS_PASSWORD`（使用密码管理器生成至少 32 字符的随机口令），保留默认 `APP_ORIGIN=http://127.0.0.1:8000`。停止所有旧服务，再执行：

```powershell
uv run --offline --no-project --python .venv\Scripts\python.exe python -B -m api.migrate_security
```

该命令先备份 `runs.db` 和 `jobs.db`，再将历史会话、运行、后台任务和上传资源显式归属到唯一持久所有者。迁移未完成时服务拒绝启动。详细的迁移边界、远程 HTTPS 配置与恢复说明见 [单人认证与资源归属](docs/implementation/26-single-owner-security.md)。

**4. 启动服务并登录**

```powershell
uv run --offline --no-project --python .venv\Scripts\python.exe python -B api\server.py
```

打开 [任务页面](http://127.0.0.1:8000)，输入访问口令登录。浏览器地址须与 `APP_ORIGIN` 完全一致；公开 API 文档入口已关闭。在终端按 `Ctrl+C` 停止服务。

会话和任务记录由 SQLite 自动保存在 `data/`，无需单独安装数据库服务；生成文件保存在 `output/`。

联网子任务先搜索、定向读取，再用子模型独立生成结构化事实报告。报告生成与格式修订不提供工具，不携带完整研究对话；报告格式或无效工具参数共用子任务原有的一次修订额度，不自动修补工具参数。报告通过结构、真实摘录和语义审核后，才交给主助手汇总。执行记录按阶段保存模型请求耗时、输入规模和服务返回的用量；失败或超时不会放行未核验草稿。

使用 Qwen3.8 Flash 作为子模型时，可配置 `LLM_SUB_EXTRA_BODY={"enable_thinking":false}`，避免默认深度思考增加结构化报告的等待时间。该参数需由模型服务支持；主模型与审核模型的参数分别配置。
Qwen3.8 Max 主模型和审核模型均可配置 `LLM_MAIN_EXTRA_BODY={"thinking_budget":1024}`、`LLM_REVIEW_EXTRA_BODY={"thinking_budget":1024}`，在保留推理的同时限制其长度；审核失败仍拒绝放行，不自动延长请求时限。

项目的 pytest 默认禁止联网并使用隔离存储和假客户端，检查通过不能证明真实模型与检索服务端到端可用。真实验收需另外运行同类问题，并核对子任务 `check_status`、最终 `answer_validation` 和交付状态；`run.status=completed` 也可能只是返回有限结果，不能单独作为业务验收通过的依据。

## 离线验证

在项目根目录执行以下命令。报告目录必须是 output/ 下尚不存在的子目录，避免覆盖已有证据。

```powershell
uv run --offline --no-project --python .venv\Scripts\python.exe python -X utf8 -B -m tools.verify_project --report-dir output/verification/local
```

验证入口按同一解释器运行依赖一致性、Ruff、完整 pytest 和前端 Node 用例；Node 和 uv 需在 PATH 中，也可用 --node、--uv 指定绝对路径。执行期间屏蔽真实服务凭据和 .env 加载，pytest 仍使用现有 conftest 的禁止联网规则。不迁移生产数据库、不修改测试、断言或应用权限，不自动调用真实模型和搜索。任一检查失败或未收集到用例都会返回非零退出码；第一阻塞点分类仅用于定位，不能将旧契约失败当作通过。

report.md 是汇总，report.json 和 pytest.xml 保留逐用例结果与原始失败位置，阶段日志单独保存。docs/ 中的设计、修复和验收 Markdown 可纳入版本管理；生成日志、截图和 output/ 仍被忽略。当前已知失败及后续限制见 [阶段四修复记录](docs/implementation/36-stage4-verification-repository.md)。

## 相关文档

- [环境变量模板](.env.example)
- [运行配置](config/runtime.yml)
- [分层预算、受控重试与后台任务](docs/runtime-governance.md)
- [设计与验证记录](docs/implementation/)

## License

[MIT](LICENSE)。基于 [jayergood/MultiAgent-Search](https://github.com/jayergood/MultiAgent-Search) 二次开发。
