# MultiAgent Search

基于 Deep Agents、LangChain 和 LangGraph 的多智能体查询与分析项目，支持联网搜索、MySQL 只读查询、上传文档分析和报告生成。任务协调助手按需逐个委派数据库、上传材料和联网搜索助手，使用实际返回结果完成汇总；最终正文自动保存为 Markdown。

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

# Search, document and database agents
LLM_SUB_MODEL=your-sub-model
LLM_SUB_BASE_URL=https://api.openai.com/v1
LLM_SUB_API_KEY=your-sub-api-key
LLM_SUB_EXTRA_BODY=

TAVILY_API_KEY=your-tavily-api-key
```

PDF 和图片分析共用 `LLM_SUB_*` 的视觉模型与密钥；服务地址必须支持图片输入，并在 `config/runtime.yml` 的对应模型项配置经核实的 `max_image_tokens` 上限。当前配置提供 Qwen3.8 Max/Flash；无需另配 OCR 服务。

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

主助手只需提供专家和完整任务说明。程序签发独立任务 ID，控制工具权限、预算、超时和取消，并记录真实来源；子助手直接返回结论和工具依据。搜索摘要、网页正文、文档位置与数据库实际查询分别保留。

不再要求 `plan_research`、需求锚点、逐条事实 ID、额外审核模型或后台恢复流程。技能为可选说明，不能添加权限。数据库仍校验只读账号与表白名单，上传材料和每轮目录保持隔离；仅正文和 Markdown 交付，不提供文件格式转换。

上传 PDF 和 PNG/JPEG/WebP/BMP/TIFF 后，文档助手会保留 PDF 原生文本，识别扫描文字、图片内容及表格行列、表头和合并单元格。超过两页先查目录，每次最多识别两页；多帧图片按帧分页。页图最长边为 2048 像素，细小文字、复杂表格或遮挡可能形成缺口；结果包含页码、区域位置及识别方式，模型识别不等于事实核验。每次识别计入子模型预算，并与渲染共用单次请求时限；目录查看不调用模型，失败会直接披露。

最终正文由程序写入 `answer.md`，使用 UTF-8/CRLF；认证下载的文件字节与保存的正文一致。部分分工失败会明确标为部分完成；调查未取得有效结果时不会发布成功文件。`completion.status` 表示实际分工与交付状态，不是程序对全部自然语言结论的事实证明。

自动验证禁止联网，使用隔离存储与可控外部响应。六类验收覆盖搜索委派、多分工、上传文档、只读数据库、失败取消和 Markdown 交付。真实服务验证另见[本轮修复记录](docs/implementation/39-basic-multi-agent-repair.md)。

## 离线验证

日常修复只验证受改动影响的实际链路，不为简单问答调用模型，也不默认重复全量测试。以下入口供确需完整回归时使用。

在项目根目录执行以下命令。报告目录必须是 output/ 下尚不存在的子目录，避免覆盖已有证据。

```powershell
uv run --offline --no-project --python .venv\Scripts\python.exe python -X utf8 -B -m tools.verify_project --report-dir output/verification/local
```

验证入口按同一解释器运行依赖一致性、Ruff、完整 pytest 和前端 Node 用例；Node 和 uv 需在 PATH 中，也可用 --node、--uv 指定绝对路径。执行期间屏蔽真实服务凭据和 .env 加载，pytest 仍使用现有 conftest 的禁止联网规则。不迁移生产数据库、不修改测试、断言或应用权限，不自动调用真实模型和搜索。任一检查失败或未收集到用例都会返回非零退出码；第一阻塞点分类仅用于定位，不能将旧契约失败当作通过。

report.md 是汇总，report.json 和 pytest.xml 保留逐用例结果与原始失败位置，阶段日志单独保存。docs/ 中的设计、修复和验收 Markdown 可纳入版本管理；生成日志、截图和 output/ 仍被忽略。当前执行协议与验证结果见 [基本多智能体修复记录](docs/implementation/39-basic-multi-agent-repair.md)。

## 相关文档

- [环境变量模板](.env.example)
- [运行配置](config/runtime.yml)
- [执行预算与资源限制](docs/runtime-governance.md)
- [设计与验证记录](docs/implementation/)

## License

[MIT](LICENSE)。基于 [jayergood/MultiAgent-Search](https://github.com/jayergood/MultiAgent-Search) 二次开发。
