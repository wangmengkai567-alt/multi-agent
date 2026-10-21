# MultiAgent Search

基于 Deep Agents、LangChain 和 LangGraph 的多智能体查询与分析项目，支持联网搜索、MySQL 只读查询、上传文档分析和报告生成。任务协调助手按需调用数据库查询助手与联网搜索助手，通过 Skill 组织任务流程。

## 环境要求

Windows、uv。项目使用 uv 管理 Python 3.11；PDF 导出另需安装 Microsoft Word。

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

**3. 启动服务**

```powershell
.venv\Scripts\python.exe api\server.py
```

打开 [任务页面](http://localhost:8000)，或查看 [API 文档](http://localhost:8000/docs)。在终端按 `Ctrl+C` 停止服务。

会话和任务记录由 SQLite 自动保存在 `data/`，无需单独安装数据库服务；生成文件保存在 `output/`。

联网子任务先搜索、定向读取，再用子模型独立生成结构化事实报告。报告生成与格式修订不提供工具，不携带完整研究对话；报告格式或无效工具参数共用子任务原有的一次修订额度，不自动修补工具参数。报告通过结构、真实摘录和语义审核后，才交给主助手汇总。执行记录按阶段保存模型请求耗时、输入规模和服务返回的用量；失败或超时不会放行未核验草稿。

使用 Qwen3.8 Flash 作为子模型时，可配置 `LLM_SUB_EXTRA_BODY={"enable_thinking":false}`，避免默认深度思考增加结构化报告的等待时间。该参数需由模型服务支持；主模型与审核模型的参数分别配置。
Qwen3.8 Max 审核模型可配置 `LLM_REVIEW_EXTRA_BODY={"thinking_budget":1024}`，在保留推理的同时限制其长度；审核失败仍拒绝放行，不自动延长请求时限。

## 相关文档

- [环境变量模板](.env.example)
- [运行配置](config/runtime.yml)
- [分层预算、受控重试与后台任务](docs/runtime-governance.md)
- [设计与验证记录](docs/implementation/)

## License

[MIT](LICENSE)。基于 [jayergood/MultiAgent-Search](https://github.com/jayergood/MultiAgent-Search) 二次开发。
