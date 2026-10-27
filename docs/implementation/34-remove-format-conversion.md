# 移除格式转换，保留正文与 Markdown 交付

日期：2026-10-26。

按用户明确要求移除格式转换，不再要求 Word，不提供 PDF 输出；不自动把其他格式请求降级为 Markdown。文件输出契约只包含 none 和 markdown，其他格式使用既有澄清流程说明支持范围。

## 清理范围

- 删除 tools/pdf_tools.py、utils/word_converter.py、agent/runtime/artifact_jobs.py。
- 删除 PDF 转换后台任务注册、Word 工作进程操作、Word 归属确认和安装注册预检；保留数据库、文档读取、搜索的进程隔离和取消回收。
- 删除转换专用的交付、完成状态、预算预留、前端提示及后台下载链接处理。
- 更新任务路由、主提示词和 report-writing Skill，使已审核内容直接生成 answer.md；正文不要求任何文件生成。
- 从 requirements.txt 与 Windows 开发锁文件中删除 Markdown 以及确认专用于转换的 25 个包，包括 md2pdf、WeasyPrint、xhtml2pdf、ReportLab 及其专用依赖。共享依赖保留，既有版本不升级；未强制卸载用户当前虚拟环境中的包。
- pywin32 保留：它仍用于 Windows Job Object 进程隔离，不再用于 Word。pypdf、python-docx 等输入解析依赖保留。
- 保留后台研究任务、上传 PDF/Word/Excel 的读取、内容审核、来源缺口披露及 Markdown 文件摘要校验。
- 未修改测试文件，未修改用户数据库，没有执行提交或推送。

## 验证

通过终端 stdin 和临时目录执行，没有创建测试文件或调用真实模型服务。

- 同步 Markdown 交付：真实生成 CRLF 文件、返回下载链接、完成状态校验通过；重复交付只占一次工具调用。
- 异步 Markdown 交付：同样通过；两条路径均在禁止启动子进程的条件下完成，证明没有依赖转换程序。
- 普通正文：不生成文件；路由及执行契约只暴露 none/markdown，拒绝 PDF 输出值。
- PDF 输入：使用临时 PDF 和真实文档工作进程读取，pypdf 解析及页定位正常。
- 转换模块已删除，其他受控工作进程仍可启动。
- uv 使用现有开发锁约束及打包工具 pin 解析依赖，120 个包与更新后的锁文件一致，没有升级无关版本。
- 现有回归：test_llm.py、test_framework_contract.py、test_call_budget.py，共 61 通过、1 失败（21.82 秒）。失败仍为 test_actual_task_model_tool_and_total_counts 中自定义专家工具未执行的断言，与删除前同组结果一致；未修改断言掩盖失败。
- 最终检查包括 Python AST、Ruff、JavaScript 语法、YAML、UTF-8/CRLF、尾随空白及已删除转换功能的引用扫描。

本记录更新了上一轮记录中的 Word/PDF 范围；上一轮其余进程管理、依赖检查和恢复修复继续保留。docs/ 仍被现有仓库规则忽略，本记录仅作为本地说明保存。
