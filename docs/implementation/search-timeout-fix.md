# 通用助手名称与搜索超时修复

本文记录上一轮修复。当前检索额度、任务去重及汇总方式以 [search-budget-fairness.md](search-budget-fairness.md) 为准；下面的整轮 2/6 次及保留工具 schema 方案已被更新。

## 原问题与证据

用户问题：明天武汉适合穿什么衣服？

原轮次 18680ffe-51d9-47e0-adc9-ae48c2c743fc 的十次 internet_search 调用均为 completed，服务端 response_time 为 0.94–2.05 秒。第一轮最后一条搜索结果在 11:08:35.664 UTC 返回，11:09:35.769 UTC 保存 TimeoutError；第二轮补查最后一条搜索结果在 11:10:39.129 UTC 返回，11:11:39.398 UTC 保存 TimeoutError。两次都发生在工具返回后的子助手模型请求阶段，不是 Tavily 网络请求超时。

重复检索、较大工具响应及当前模型的等待时间共同增加了整条流程的成本。后续真实复现还发现检查模型超时，以及额度拦截误伤成功证据的问题，均已分别修正；不能只把助手改名或延长请求时限。

## 最终行为

- 统一显示任务协调助手、数据库查询助手、联网搜索助手；内部路由仍为 db / search，旧任务记录不重写。
- 主角色与联网助手职责改为通用查询；页面标题和问题示例移除电商定位。电商 Skill 保留为可选业务方法，不强制加载。
- 每轮注入带时区的参考时间、今天和明天的绝对日期，减少相对日期混用。
- 普通搜索整轮最多预留两个不同检索调用；明确研究/报告/内外对照任务整轮允许六个，配置项为 calls_per_run / research_calls_per_run。并行调用原子预留，同一调用的受控网络重试仍占原槽位。重新委派或质量修订均不重置额度；同一子任务修订复用已有证据，并发子任务之间证据隔离。
- 简单联网子任务首次必须实际检索；额度用尽后禁止工具调用并要求总结。执行层拦截额外调用，额度拦截保留审计记录，但不将其误判为已成功搜索的网络失败。
- 默认最多返回三条检索结果；模型所见单条片段最多 1000 字符，正文总量最多 6000 字符，并明确片段截断。完整响应通过 ToolMessage artifact 纳入既有脱敏、哈希和结果存储，来源检查仍使用真实响应。
- 搜索请求使用独立的 search.timeout_seconds=20，不再借用 SQL 超时。模型请求仍为 60 秒，总任务仍为 300 秒，没有通过增加这两项上限掩盖问题。
- 模型超时明确标注任务协调、子助手或检查阶段。子助手模型整理超时后，本轮不再次委派同一失败助手；用户新一轮任务不受此限制。
- 检查请求受剩余任务时间与单请求时限约束；不跳过来源检查，也不把检查失败当作成功。JSON 包裹代码围栏可正常解析；结构错误最多重试检查一次，保留相同任务、结果与工具依据，不重新搜索。仍无法检查时阻止本轮重复委派搜索助手。
- 前端使用本地固定版本 Marked 和 DOMPurify 渲染并清理 Markdown，标题、表格、列表和代码正常展示；单个 ~ 保留为温度范围符号，仅 ~~ 作为删除线。额度拒绝显示为未执行的新请求，不再误报网络失败。
- 新增可选 LLM_EXTRA_BODY，默认模板留空，不向其他服务商强加参数。当前本地 MiMo 接口已用真实请求验证 thinking.type=disabled 可用，并为该环境配置快速响应；需要长推理时可自行调整此项。

## 验证

新增 tests/test_search_runtime.py，覆盖原文保留、片段预算、模型与网络超时区分、同轮失败助手拦截、检查超时、原子检索额度、强制结束检索、额度拒绝保留有效证据，以及服务商扩展参数的校验。跨年日期边界另由 test_acceptance14.py 覆盖。

真实回归使用 tests/manual/search_regression.py，通过生产 ASGI 应用、真实模型与 Tavily 完成同一个问题；存储隔离到 output/search-regression/，未启动新监听端口或重启用户的服务。

上一版实测（额度范围修正前）：81.98 秒、3 次检索（含质量补查），一个联网搜索子任务 completed / passed，无数据库调用、无电商 Skill 加载，六项检查均通过。本机记录：[summary.json](../../output/search-regression/20261017T113149-d662d8/summary.json)。前两次未通过的实测记录保留在同一 output/search-regression/ 下，未覆盖或伪报成功。

上一版完整离线回归：403 passed in 463.30s，退出码 0。命令：

```powershell
.venv\Scripts\python.exe -m pytest -q -x --tb=short -p no:cacheprovider --basetemp=output\pytest-generic-search-final-frozen
```

两组 Node 生产前端测试通过；17 个改动 Python 文件的 Ruff 检查通过。全部 56 个受管理改动及未忽略文本（另包含实际 .env）通过 UTF-8、CRLF 和行尾空白检查；真实回归 summary.json 也显式使用 UTF-8 / CRLF。


## 使用

当前服务以 reload=False 启动，修改后需要停止并重新启动服务，新名称、提示词和模型选项才会生效：

```powershell
.venv\Scripts\python.exe api\server.py
```

本轮日志、额度范围和 Markdown 修复的最新验证见 [search-recovery-fix.md](search-recovery-fix.md)。
