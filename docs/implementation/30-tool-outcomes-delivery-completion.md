# 顺序③：工具终态、交付错误与业务完成状态

实施日期：2026-10-26。

## 修复结果

- 工具尝试分别记录 execution_status、result_status、stage 和 attempt。准入被拒绝时为 not_started；处理器返回后为 completed；后续证据检查失败时保留 completed 并将结果标为 rejected。已启动操作发生超时或取消时保留 unknown，不假装已经回滚。
- 同一个尝试仅发送一次 after_tool。同步执行线程真正进入处理器时才标记 executed；迟到结果不能再变成获准结果。返回的 ToolMessage 必须属于本次 call_id。
- 程序交付错误保留脱敏 message、error_category、stage、tool_name 和 call_id。后台转换错误额外保留 task_id，能关联提交转换的工具调用。原始错误工具正文仍不复制到诊断中，避免把上传内容当作错误外发。
- 文件交付在调用前登记身份和参数摘要；获准结果、文件摘要与阶段状态随 delivery_draft 持久化。重复进入同一交付步骤时只复用参数及字节核验一致的已完成结果；结果未确定或被拒绝时禁止自动重放。不同参数不得覆盖原调用身份。
- 通知传输与独立记录器失败不改变已经执行的业务结果。记录器失败仍发送工具终态，并在最终事件中披露 recording_status=incomplete；文件不能因此被盲目重复生成。

关键实现：agent/runtime/hooks.py、delivery_actions.py、completion.py、events.py、workflows.py、api/run_store.py、api/server.py、api/static/app.js。

## 完成状态的权威来源

运行生命周期继续由 runs.status 表达：completed 表示本轮执行已经正常结束。业务是否完成统一由服务端 completion 表达，页面不再从审核事件或几个后台任务标志自行推断。

completion 同时包含 status、execution_status、review_status、coverage、delivery_status、artifact 和 error。

| 条件 | completion.status |
|---|---|
| 正文终审通过、需求完全覆盖、所需文件已经交付 | complete |
| 已交付合格答复，但需求存在已披露缺口 | partial |
| 尚不能确认审核、覆盖或交付 | unknown |
| 研究或文件转换仍在后台执行 | pending |
| 当前交付为澄清或后台任务等待补充 | input_required |
| 运行或所需后台任务失败、取消、超时、中断 | 对应失败状态 |

所有已锁定的可执行契约，均在 finalize_answer 检查正文交付指纹及所需文件。文件必须对应审核正文，且仍存在、非空、字节摘要匹配；未完成 PDF 只能关联本轮已登记的转换任务。缺少来源仍允许交付已审核的部分结果，并保留 partial / unknown，不重新引入“来源缺失就整轮失败”的旧问题。

最终事件与生命周期更新在同一数据库事务中保存。RunStore 从本轮权威终态事件读取 completion；查询接口再结合持久化任务最新状态投影。GET /api/runs/{run_id}、/history、/jobs 与取消接口返回一致状态。后台研究结果携带实际执行轮的 completion。

PDF 转换失败或取消后，即使父运行已 completed，业务状态仍反映失败或取消；仅在转换任务提交成功时不会显示完整完成。页面同时展示脱敏错误，使用终态事件序号拒绝较旧任务响应覆盖较新的状态。unknown 的页面文案为“完成情况待确认”。

## 参考源码

固定参考 Codex 提交 970111f6cb62cc02fe36a82ea7d71b37a2889c97 的 [registry.rs](https://github.com/openai/codex/blob/970111f6cb62cc02fe36a82ea7d71b37a2889c97/codex-rs/core/src/tools/registry.rs#L933-L995)。该实现明确区分已完成的工具执行与执行后结果拒绝，并通过原子终态标记保证通知去重。本项目沿用这两个原则；需求覆盖、正文审核、文件摘要和后台任务归并属于本项目业务适配。

## 检查点与存储

检查点版本由 21 升至 22，交付动作身份和不确定状态随 delivery_draft 保存。旧版检查点保留并拒绝恢复，旧任务需重新发起。不自动迁移、重放或重置已消费预算。运行数据库 schema 未改变；不修改真实业务数据库。

## 验证记录

没有新增或修改测试文件。定向检查通过 stdin 驱动，数据库、输入和转换产物全部位于系统临时目录。

- 33 项工具及基础交付检查：同步/异步成功、准入拒绝、错误返回、异常、迟到取消、结果校验拒绝；真实 Markdown 写入；通知失败后复用文件；工具与运行终态去重；文件篡改拒绝；覆盖与后台状态投影。
- 14 项生产 app.js 隔离页面检查：服务端业务状态显示、审核事件不冒充完成、事件乱序、PDF 错误展示和旧任务响应拒绝。
- 59 项生产链路与认证 HTTP 检查：真实代理图、预算包装器、终审和 Markdown 工具；持久化 JobService、PDF 暂存与发布；三个查询接口的一致性；转换成功、失败、取消及迟到发布拦截；脱敏错误的工具调用与后台任务身份；登录及资源访问检查。模型提供固定协议响应，Word 转换器使用受控本地桩。
- 23 项边界检查：同步/异步交付的权限、硬预算、超时类别与阶段；BudgetExceeded 原类型及终止状态保留；不确定写入不重放；检查点保留原动作身份。
- 8 项终态补充检查：跨调用结果拒绝；独立记录器失败后保留已执行文件、发送一次终态并暴露记录故障。

共 137 项定向断言通过，未重复累计链路重跑次数。

6 组已有回归共 97 项，修复前和修复后均为 32 通过、30 失败、35 个夹具错误，失败与错误节点集合一致。旧问题包括安全数据库迁移夹具、旧接口、证据及恢复协议断言。现有 3 组前端脚本在修复前已因 BroadcastChannel 环境桩缺失或 WebSocket close 事件桩不匹配而失败；未修改这些脚本来掩盖问题。

静态与格式检查、JavaScript 语法、Python AST、UTF-8/BOM、CRLF 和逐行空白检查通过。只执行 git status；项目禁止其他 Git 操作，因此用修改前快照、直接字节检查和逐行比较核查改动，不执行 git diff --check。

未运行真实模型、搜索服务、MySQL 或三道业务验收题；未启动真实 Word/PDF 转换和排版。以上证明本次控制链与回归范围，不代表整套历史测试或真实业务验收已经通过。
