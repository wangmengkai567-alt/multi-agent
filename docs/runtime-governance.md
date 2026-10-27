# 执行治理与持久化任务

本次修复对应运行 959b53ba-7be9-4ca4-97ab-15ae6262cd5d。历史状态和原始结果不改写。

## 调用与分工

服务端通过 plan_research 一次登记本轮全部独立分工，生成不可变 task_id。task 工具的 description 使用 {"task_id":"已登记 ID"}，执行器从登记记录取得原始分工。重复规划返回原计划，不能用改写描述重置额度。已经关闭的任务不能重新接纳；后台恢复可复用已检查通过的结果。

事件保留模型请求 ID、服务端执行 ID、父调用 ID。生产运行在模型消息进入框架时为每次工具请求签发唯一 ID，供应商原始 ID 仅作 provider_call_id 元数据；不同助手或回合重复使用供应商 ID 不会合并执行记录。助手返回事件维持原执行父级。前端按实例显示分工、执行编号、检查结果和工具次数，每次工具请求的不同生命周期事件聚合，展开后可查看详细过程。历史事件仍使用原始记录，通过请求 ID 关联展示。

## 来源

工具为实际 URL 提供 source_id。模型可写 [source:ID]，由程序填入本任务真实取得的完整 URL，不推定裸域名与 www 域名等价。未登记 ID、未取得 URL 必须修订；反馈定位具体来源和断言。只缺引用时复用已有证据，不重搜；事实仍需通过语义检查。最终答复的引用修订和事实修订各最多一次，分别计入预算；最终检查失败即为有限交付，不能因子任务通过而误报完整结果。

工具证据在返回时原子保存为 UTF-8/CRLF JSON，Windows 长路径得到处理。检查失败的草稿不作为已核实事实传给主助手。

## 分层预算

config/runtime.yml 分别配置前台 budgets、持久化任务 job_budgets，以及单次工具 tool_seconds、模型 request_seconds、前台 total_seconds、后台 job_seconds。tools_per_turn 限制一次模型回合可执行的工具次数，每次重试还消耗 tool/retry 及整轮总额度。后台运行保留独立的时长和调用次数预算，重启不刷新已使用次数。

near_ratio 和 finalize_seconds 触发受控交付。达到工具、模型或整轮边界时保留具体错误；硬超时维持 timed_out。只有程序从已检查资料生成的有限结果可写入失败轮次的 answer，并通过 partial_result 事件展示。

当前按调用次数和时长计量，不把未知供应商价格换算成虚构费用。

## 重试

Hook 统一决定传输重试，模型 SDK 隐式重试关闭。瞬时超时、断连和 5xx 使用指数退避及随机抖动；429 读取实际 Retry-After，Tavily HTTP 适配保留 SDK 原本会丢失的状态与响应头。等待超过剩余预算时明确拒绝重试。

408 按超时处理；409 不自动重试；其他 4xx 不重试。运行中的查询工具列入只读白名单。写操作默认不自动重试，只有实现声明并实际保障幂等键、事务或补偿安全时才具备资格，模型传入一个键不构成保障。

同步等待超时不宣称底层操作已经回滚。受控同步工作线程数量有限，迟到返回不能变更终态。现有长时 Word/PDF 转换使用下面的持久化协议。

## 持久化任务

页面可选择“后台公开调研”，或由协调助手通过 submit_research_job 提交完整公开调研目标。提交立即返回 task_id、working 和 poll_after_seconds；受理不代表业务完成。当前注册的长任务包括公开调研及 Word/PDF 转换。

- GET /api/runs/{run_id}/jobs：本轮提交或关联的任务。
- GET /api/jobs/{task_id}?thread_id=...：所属会话的任务状态与结果。
- POST /api/jobs/{task_id}/cancel?thread_id=...：显式取消。
- POST /api/jobs/{task_id}/resume：提交 thread_id 和 user_input，恢复 input_required 任务。

状态为 working → completed / failed / cancelled，或 working → input_required → working。等待人工输入期间暂停执行时长预算。后台服务使用 SQLite 保存任务、幂等请求键、恢复次数与执行租约；同一任务由一个持有租约的工作者执行，失去租约的迟到工作者不能发布结果。服务关闭保留可恢复任务，显式取消保留 cancelled。

公开调研使用 LangGraph 检查点和原子运行快照，恢复时保留分工、证据和计数，只读查询可能在不确定的崩溃边界再次执行，但不能刷新预算。后台调研禁止嵌套任务及文件写操作。需要上传文件或内部业务前置检查的任务使用前台流程，不将其悄悄转成公开调研。

PDF 转换在原业务前置检查通过后提交。输入按内容摘要冻结，输出先写任务隔离目录；发布时在数据库事务中核验状态与租约，再原子替换本轮目标文件。取消后完成的底层 Word 操作只能留下隔离的暂存文件，不能发布最终文件。既有直接脚本调用保留同步兼容路径。

前端按建议间隔轮询，无需模型持续等待；显示等待输入、失败、取消和下载链接。“完成了吗？”等状态追问关联已有任务，保留原始目标，不新建同一份后台研究。

## 验证

- tests/test_execution_governance.py：超时、指数退避、429、写操作门槛、每回合上限、稳定任务身份、来源修订、未知工具、Windows 长路径。
- tests/test_durable_jobs.py：受理、幂等、会话归属、重启、输入暂停、租约、终态保护、PDF 原子发布与取消。
- tests/frontend/call-groups.test.cjs：同类助手和同名工具并发、乱序回放、终态与后台状态。
- tests/manual/governance_live.py：真实模型及 Tavily 的隔离城市比较，完整检查和有限结果严格区分。
- tests/manual/governance_verify.py：离线回归并将日志保存为 UTF-8/CRLF。

验证证据位于 output/runtime-governance-validation。实现阶段的失败运行保留，最终结果见验收报告；不以受理或有限结果代替完整调研成功。
