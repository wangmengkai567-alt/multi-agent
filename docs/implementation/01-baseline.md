# 第 01 步：代码基线与开发环境

日期：2026-10-13（Asia/Shanghai）。状态：第 01 步已完成；未开始第 02 步，未启动项目服务。

## 1. 修改前的代码基线

- 仓库：`D:\My_workspace\Multi-agent\Multi-agent`（内层仓库；外层目录也有 .git，实际提交使用本仓库）。
- 分支：`main`。
- HEAD：`aeeeca939b34dc6290603c9e3bde384c961120c0`。
- 最近提交：`aeeeca9 🧹 Cleanup: remove debug prints, fix naming, sanitize demo code`。
- `git status --porcelain=v1` 原始输出：

```text
?? docs/
```

原有 docs 下的实施计划等用户文档保留，不纳入本步提交。新增的本记录单独提交。

源码当前配置三个助手：数据库、网络搜索、知识库。没有旧工作区中的反思检查、共享摘要和会话历史模块。tests 仅有历史 __pycache__，未找到可执行的现有测试源码。

## 2. Python 与实际支持版本

CI 和 Docker 均使用 Python 3.11，Ruff target-version 为 py311。README 写 Python 3.10+，但已安装的当前锁定依赖元数据表明：

```text
numpy 2.4.2: Requires-Python >=3.11
pandas 3.0.1: Requires-Python >=3.11
deepagents 0.4.3: Requires-Python <4.0,>=3.11
langchain 1.2.10: Requires-Python <4.0.0,>=3.10.0
langgraph 1.0.9: Requires-Python >=3.10
```

因此当前依赖组合的 Python 下限是 3.11，不能按 README 的 3.10 声明安装。此步实际验证 Windows x64 + CPython 3.11.15；未验证其他 Python 版本、Linux 或 Docker。

虚拟环境：`D:\My_workspace\Multi-agent\Multi-agent\.venv`。

解释器：`D:\My_workspace\Multi-agent\Multi-agent\.venv\Scripts\python.exe`。

`.python-version` 固定为 3.11.15。环境由本机 uv 0.11.26 创建，使用独立环境，未修改其他项目或系统 Python 包。

## 3. 运行依赖核对与补齐

通过 AST 检查 agent、api、tools、utils、rawflow 下 21 个 Python 文件，未导入业务模块，未读取 .env。

第三方导入包括 Deep Agents、LangChain、LangGraph、FastAPI、dotenv、YAML、Tavily、MySQL、RAGFlow、Markdown、DOCX、PDF、pandas 与 Windows COM；现有清单已列出这些分发包。

发现源码直接导入 `langgraph.checkpoint.sqlite.SqliteSaver`，但清单缺少单独分发的 SQLite checkpoint 扩展。已补齐：

```text
langgraph-checkpoint-sqlite==3.0.3
aiosqlite==0.22.1
```

aiosqlite 是 SQLite 扩展使用的后端依赖，也供第 03 步异步 checkpoint 使用；此步仅声明依赖，未修改同步保存器实现。

安装前解析发现 SQLite 扩展 3.0.1 要求 checkpoint <4，与已有 4.0.0 不兼容；使用实际解析成功的 3.0.3。原有核心版本保持不变：

```text
deepagents==0.4.3
langchain==1.2.10
langgraph==1.0.9
langgraph-checkpoint==4.0.0
```

新增 `requirements-dev.txt`：pytest 8.3.5、pytest-asyncio 0.26.0、httpx 0.28.1、ruff 0.11.13，并包含现有运行清单。

新增 `requirements-dev-win-py311.lock`，从实际安装环境导出全部 146 个包的精确版本，包括补全的间接依赖。该锁文件限定本次 Windows/Python 环境；不作为 Linux 兼容承诺。

## 4. 测试配置与隔离范围

`pyproject.toml` 指定 tests 路径、项目导入路径、严格标记和配置检查、pytest-asyncio 严格模式及函数级事件循环。

`tests/conftest.py` 在测试收集前禁用 dotenv 加载、清理当前进程的模型与服务凭据变量、拒绝 Python socket 网络连接。每个测试默认切换到临时目录，提供临时 SQLite 连接，结束后关闭连接。

本步新增 3 项测试环境验证：

1. 临时 .env 文件不能被 load_dotenv 加载。
2. Python socket 连接在拨号前被拒绝。
3. 相对文件写入与 SQLite 数据库使用临时目录。

**这 3 项不是 Agent 功能测试。已有业务测试数为 0，没有据此声称查询或多 Agent 链路通过。**

检查时设置 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`，仅显式加载 asyncio 插件，避免其他已安装包自动加载测试插件。后续测试也应使用同一设置。

隔离不是操作系统沙箱：原生数据库驱动、直接调用其他进程、强行写绝对路径可能绕过 Python socket 与当前目录限制。后续业务测试必须注入假客户端、指定临时数据路径；当前禁止在测试配置中导入 api.server 和 agent.main_agent，以免触发外部客户端构建或打开项目 checkpoint。

## 5. 已执行的验证与输出

所有命令从仓库根目录执行，以下是本次实际结果。

### 依赖安装与一致性

```powershell
& 'C:\Users\Dell\.local\bin\uv.exe' pip install --python .venv/Scripts/python.exe --link-mode copy -r requirements-dev.txt
```

```text
Resolved 143 packages
Installed 143 packages
```

146 个锁定包包括虚拟环境中已存在的 pip、setuptools、wheel 等；与安装命令新增数量的统计口径不同。

```powershell
& '.\.venv\Scripts\python.exe' -m pip check
```

```text
No broken requirements found.
```

```powershell
& 'C:\Users\Dell\.local\bin\uv.exe' pip install --python .venv/Scripts/python.exe --dry-run -r requirements-dev-win-py311.lock
```

```text
Checked 146 packages
Would make no changes
```

已从环境导入 `SqliteSaver` 与 `AsyncSqliteSaver`：输出 `SQLite saver imports: OK`。此检查只验证扩展可导入，不验证应用异步执行，未打开应用数据库。

### 测试收集与执行

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
& '.\.venv\Scripts\python.exe' -m pytest --collect-only -q
& '.\.venv\Scripts\python.exe' -m pytest -q
```

```text
3 tests collected in 0.04s
3 passed in 0.38s
```

### 本步新增测试代码检查

```powershell
& '.\.venv\Scripts\python.exe' -m ruff check tests
& '.\.venv\Scripts\python.exe' -m ruff format --check tests
```

```text
All checks passed!
2 files already formatted
```

AST 语法检查：`Python files: 21`，`Syntax errors: []`。这不是导入测试。

`git diff --check` 无输出，退出码 0。检查确认 .venv、.env、data、output、updated、pytest/ruff 缓存被 Git 忽略。

### 原有业务代码的检查基线

```powershell
& '.\.venv\Scripts\python.exe' -m ruff check agent api tools utils rawflow --output-format json
```

退出码 1，105 项问题：

```text
E402: 15    E701: 1     E722: 3     E731: 1
F401: 9     F541: 1     F811: 2     I001: 22
UP006: 8    UP007: 10   UP008: 1    UP015: 2
UP035: 8    W291: 5     W292: 17
```

这些来自未修改的原有业务代码，第 01 步仅记录，不修改规则、忽略问题或大批自动重写代码。不能声称全仓 lint/CI 已通过。

## 6. 已确认的运行风险与未执行项目

| 项目 | 当前结论 | 后续安排 |
|---|---|---|
| 同步 SqliteSaver + 异步执行 | 源码使用该组合，尚未进行应用运行验证 | 第 03 步修复并验证 |
| 导入即构建主 Agent、打开 SQLite | main_agent 模块存在这些副作用 | 第 03 步迁入生命周期 |
| 外部客户端初始化 | llm、Tavily 等会在导入时读配置或构建对象 | 测试先用替身，不导入真实服务入口 |
| 后台任务引用与异常收集 | server 直接 create_task，尚无任务集合管理 | 第 03 步处理 |
| 文件路径与 SQL 强制限制 | 当前仍是原型实现 | 第 07 步实施 |
| Skill、Hook 与框架能力 | 未进行最小接口验证 | 第 02 步处理 |
| Word PDF 转换 | 依赖 Word COM 和本机 Word 安装 | 本步未调用 Word，未验证转换 |
| 业务查询、付费模型、MySQL、RAGFlow、Tavily | 未调用 | 后续显式的功能验证阶段 |
| Docker、GitHub CI | 未运行，已有 lint 问题可能使 CI 失败 | 不在本步报告通过 |

没有启动 FastAPI/Uvicorn，也没有创建或迁移业务 checkpoint。

## 7. 复现环境

Windows x64，从仓库根目录执行：

```powershell
uv venv --python 3.11.15 --seed .venv
uv pip install --python .venv/Scripts/python.exe --link-mode copy -r requirements-dev-win-py311.lock
& '.\.venv\Scripts\python.exe' -m pip check
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
& '.\.venv\Scripts\python.exe' -m pytest -q
```

本机 uv 不在 PATH 时可使用实际路径 `C:\Users\Dell\.local\bin\uv.exe`。锁文件固定安装版本，但仍需要访问包索引与下载 Python；未提供离线 wheel 仓库，也没有对所有包加文件哈希。

本步变更文件：.python-version、.gitignore、requirements.txt、requirements-dev.txt、Windows 环境锁文件、pytest 配置、测试隔离配置与 3 项隔离验证，以及本记录。没有修改业务行为。
