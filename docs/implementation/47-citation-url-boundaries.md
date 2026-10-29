# 合法括号 URL 引用误判修复

日期：2026-10-29。项目根目录：D:/My_workspace/Multi-agent/Multi-agent。

本轮修复审计中的第一个问题：实际搜索来源包含配对括号时，引用提取在左括号处截断，子助手因此被误判为引用了不存在的来源。起始工作区干净；生产修改仅在 agent/runtime/output.py。未新增或修改测试文件。

## 实际读取并采用的 Codex 实现

通过 GitHub 连接器搜索并读取 openai/codex 固定提交 e75e4af22938c8715b2a1e0d3fc142836ab4d962 的源码正文：

- [terminal_hyperlinks.rs：web_links_in_text / trailing_url_end，第 548–637 行](https://github.com/openai/codex/blob/e75e4af22938c8715b2a1e0d3fc142836ab4d962/codex-rs/tui/src/terminal_hyperlinks.rs#L548)：对候选地址一次统计四类分隔符的配对余额，从末尾剥除多余闭括号及句尾标点；配对括号与地址中的查询问号保留。
- [同文件 preserves_balanced_parentheses_in_bare_web_urls，第 968 行](https://github.com/openai/codex/blob/e75e4af22938c8715b2a1e0d3fc142836ab4d962/codex-rs/tui/src/terminal_hyperlinks.rs#L968)：上游明确覆盖 Wikipedia 的 Function_(mathematics) 和带 ?q=(alpha)? 查询串的链接。
- [clipboard_html.rs：render_markdown](https://github.com/openai/codex/blob/e75e4af22938c8715b2a1e0d3fc142836ab4d962/codex-rs/tui/src/clipboard_html.rs#L14)：读取了其通过 pulldown-cmark 取得 Markdown 链接目标、再应用 web_destination 策略的实现。

本项目直接采用 trailing_url_end 的配对余额算法，以 Python 等价移植；同时让现有候选提取规则允许 ASCII 圆括号。上游函数用于终端链接边界处理，本项目将相同机制应用于子助手和最终答复共用的 cited_urls。来源归属及完整 URL 比较继续由原有委派和交付入口执行。未增加依赖。

既有中文标点、序列化换行、行内代码及代码块排除规则保留。扫描候选地址的时间复杂度保持线性。上游原文、函数片段及用例保存于证据目录的 upstream-source.json。

## 复现和验证

证据目录：[output/verification/citation-url-20261029/](../../output/verification/citation-url-20261029/)。

- 修复前最小断言失败：期望 https://example.com/wiki/Python_(programming_language)，实际提取为 https://example.com/wiki/Python_。
- 用保存的修复前源码重放真实认证 HTTP、生产图构建器和 task 委派链路：合法来源被判为 Expert cited a URL absent from its actual tool results，分工和主任务失败，下载返回 404。
- 修复后同类完整链路：合法括号链接及查询串在子助手和最终正文中完整保留；分工、主任务均 completed，完成状态 complete，认证下载返回 200，下载字节等于保存的正文。
- 分别伪造子助手引用和最终正文引用：均 failed，无成功 answer.md，下载返回 404。主助手伪造场景的子助手仍正常完成，确认最终交付校验也实际执行。
- 13 个边界场景通过，包括上游的配对括号/查询问号、嵌套括号、Markdown 标题、自动链接、多个链接、中文说明、代码排除及多余尾部闭括号。

完整链路结果见 http-replay.json/log；边界断言见 boundary-cases.json。HTTP 重放使用临时数据库、现有 ScriptedModel 和可控搜索响应；禁止外部网络，未调用真实模型或搜索服务，未迁移或修改生产数据库。

既有相关回归命令：

```powershell
uv run --offline --no-project --python .venv\Scripts\python.exe python -X utf8 -B -m pytest tests/test_output_boundary.py tests/test_basic_boundaries.py tests/test_basic_coordination.py tests/test_search_scope.py -q --tb=short
```

首次结果为 30 通过、1 失败，原始日志和 XML 保留于 regression.log/xml。失败项 test_actual_windows_worker_close_reaps_descendants 在 PID 标记文件已创建但内容尚为空时执行 int(marker.read_text())，抛出 ValueError；该测试未执行引用提取。原测试不作修改，单独复跑 1/1 通过，记录见 worker-recheck.log/xml。两次结果分别保留，不将首次失败记录改写为全量通过。

Ruff 检查及格式检查通过。生产 AST 审计确认仅新增 _trailing_url_end、修改 cited_urls，其余节点不变；UTF-8、原 BOM 设置、CRLF 和行尾空白检查通过。源码差异与字节审计见 production.patch、source-audit.json。按项目限制仅执行 git status，未执行其他 Git 操作。
