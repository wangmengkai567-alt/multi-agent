# MultiAgent Search

面向电商运营分析的多助手查询项目，基于 MultiAgent-Search 二次开发。主助手通过 Deep Agents 的 task 路由调用数据分析、市场情报和内部知识三个助手，统一执行权限检查、调用预算、结果检查与事件持久化。

首版使用单服务进程、SQLite 和本地文件目录。第14步的验收范围、实际命令与外部依赖限制见 [验收记录](docs/implementation/14-acceptance.md)。

## 启动

已验证环境为 Windows、Python 3.11.15。框架锁定为 deepagents 0.4.3、LangChain 1.2.10、LangGraph 1.0.9；完整依赖见 requirements-dev-win-py311.lock。

在项目根目录使用 PowerShell：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev-win-py311.lock
Copy-Item .env.example .env
```

编辑 .env，设置 OPENAI_BASE_URL、OPENAI_API_KEY、LLM_QWEN_MAX。搜索需要 TAVILY_API_KEY；知识库需要 RAGFLOW_API_URL、RAGFLOW_API_KEY 和可访问的助手。数据库必须另配 MYSQL_READONLY_USER、MYSQL_READONLY_PASSWORD、MYSQL_HOST、MYSQL_PORT、MYSQL_DATABASE，运行时核验只读权限，不能使用旧的管理员字段回退。账号配置示例见 sql/read_only_user.example.sql。

```powershell
.venv\Scripts\python.exe api\server.py
```

打开 [任务页面](http://localhost:8000) 或 [接口文档](http://localhost:8000/docs)。关闭终端服务时会中断未完成任务并清理连接。实际部署请按环境设置监听地址和访问控制。

## 页面使用

- 输入问题，选择新会话，或在已有会话中发起新一轮。每轮有独立输出目录与运行记录；同一会话正在执行时返回忙碌提示，不覆盖原任务。
- 普通任务可自由选择助手；两个 Skill 均可选。“内部数据与市场对照”自动选择运营分析 Skill，服务端要求先取得合格内部结果再搜索。数据库不可用时只允许明确标注的降级分析。
- 查看 Skill 选择、正文与资源读取，助手开始，工具完成，结果检查、补充查询和失败原因。技术 ID、关联关系和原始状态在展开详情中。
- task 调用只接受 description 和 subagent_type，完整任务说明必须放入 description；额外的 prompt 等字段会在助手执行前拒绝，避免框架静默丢失约束。
- 运行状态区分等待、执行、完成、失败、取消、超时、中断与部分完成。部分完成由持久事件中的降级、未通过检查或记录不完整状态判断，不修改服务端原始终态。
- “取消本轮”按 run_id 取消；“重新读取记录”同步服务端状态。刷新页面会恢复浏览器保存的任务列表，并从服务端重读完整答案与事件。断线后 WebSocket 按连续序号补发并去重。
- 文件列表只显示当前轮次的文件，下载地址进行一次 URL 编码。移除视图只清除浏览器列表，不取消运行或删除服务端历史。

浏览器只保存 thread_id、run_id、问题和当前选中项；答案与事件以 SQLite 为准。清除浏览器存储后不会自动列出所有历史会话，可通过已知 run_id 的接口读取记录。首版沿用同一会话一个活动 WebSocket 的约定，不承诺多个浏览器标签页同时订阅同一会话。

## API

```powershell
$body = @{
    query = '检索公开电商资料，并给出来源'
    skill_ids = @('ecommerce-analysis')
    business_mode = 'ordinary'
} | ConvertTo-Json
$run = Invoke-RestMethod http://localhost:8000/api/task -Method Post -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
Invoke-RestMethod "http://localhost:8000/api/runs/$($run.run_id)/history"
```

| 接口 | 用途 |
|---|---|
| POST /api/task | query；可选 thread_id、skill_ids、business_mode、required_sources、required_uploads；返回 thread_id 和 run_id |
| GET /api/runs/{run_id} | 原始状态、完整答案、失败原因与时间 |
| GET /api/runs/{run_id}/history | 运行、事件、子任务、可追溯结果 |
| GET /api/runs/{run_id}/events?after_seq=0&limit=200 | 增量补读，使用 next_seq 和 has_more 分页 |
| POST /api/runs/{run_id}/cancel | 取消当前运行；终态保持不变 |
| WS /ws/{thread_id}?run_id={run_id}&after_seq=0 | 本轮持久事件与实时更新 |
| POST /api/upload | multipart files 和 thread_id；上传源文件只读 |
| GET /api/files?path=session_{thread_id}/run_{run_id} | 本轮文件列表 |
| GET /api/download?path=... | 经路径校验的文件下载 |

business_mode 为 ordinary 或 internal_external；后者必须显式包含 ecommerce-analysis。required_sources 可选 db、search、knowledge；required_uploads 指定本轮必须完整读取的上传文件。总超时、请求超时、预算、SQL 表白名单、上传大小及有限重试统一配置于 config/runtime.yml。

当前配置总时限为 300 秒、模型单请求上限为 60 秒；第14步针对所配模型的 30 秒请求超时作了调整。每轮截止时间覆盖模型、检查与重试，不因补充查询重新计时。

## 功能状态

| 范围 | 已实现 | 验证状态 |
|---|---|---|
| 异步 checkpoint、运行互斥、SQLite 历史和事件 | 是 | 离线集成回归；真实模型普通查询 |
| 七类 Hook、SQL/路径规则、调用预算 | 是 | 真实框架与业务函数计数验证；只读账号成功查询受本地配置限制 |
| 三助手 task 路由、结果检查、最多一次补查 | 是 | 可控模型与真实框架集成；外部服务实测见验收记录 |
| 合格结果传递、内外对照顺序与降级 | 是 | 服务端持久记录、实际执行输入与越序零执行验证 |
| 两个 Skill 的正文和资源读取 | 是 | 真实框架文件读取；真实模型和页面联调 |
| 总超时、取消、中断、重连补发 | 是 | 后端与生产前端脚本回归；页面刷新恢复联调 |
| 新前端、完整答案、本轮下载 | 是 | 前端脚本测试与浏览器操作验证，详见验收记录 |
| 自动恢复重启前执行、分布式调度、动态助手 | 否 | 后续范围 |
| 登录鉴权、多进程部署、跨轮自动复用摘要 | 否 | 后续范围 |
| PDF 在所有平台的完整转换链路 | 现有工具 | 本次未做端到端验收，不作为已验证功能 |

不能用模型自述代替查询依据，也不能用界面“已完成”证明外部服务已成功。真实数据库、RAGFlow 和搜索的具体通过/失败记录，以验收文档为准。

## 开发验证

```powershell
.venv\Scripts\python.exe -m pytest -q --tb=short -p no:cacheprovider --basetemp=output/pytest-local
node tests/frontend/run-events.test.cjs
node tests/frontend/app.test.cjs
.venv\Scripts\python.exe -m pip check
```

pytest 在收集前移除真实凭据并阻止外部网络，使用临时数据库和目录。Node 测试直接执行生产前端脚本，验证恢复、状态转换、请求参数和下载链接。真实联调脚本 tests/manual/acceptance14.py 须显式运行，不会被 pytest 自动执行：

```powershell
# 终端一：使用 output/step14-live 的独立数据库，监听 127.0.0.1:8765
.venv\Scripts\python.exe tests/manual/acceptance14.py --serve
# 终端二：会实际调用配置的模型与外部服务，并保存脱敏执行日志
.venv\Scripts\python.exe tests/manual/acceptance14.py --smoke
# 验收结束后触发正常退出，核对端口释放
[System.IO.File]::WriteAllBytes((Join-Path $PWD 'output\step14-live\stop'), [byte[]]@())
Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
```

详细设计与逐步记录位于 docs/implementation/；总体进度见 [实施计划](docs/多Agent项目分步骤实施计划.md)。本工作区约定 Git 仅执行 status，代码与文档修改保留工作区，由用户自行管理版本。

## License

MIT。原项目：[jayergood/MultiAgent-Search](https://github.com/jayergood/MultiAgent-Search)。
