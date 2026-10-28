# 同一对话多客户端订阅修复

日期：2026-10-28。项目根目录：D:/My_workspace/Multi-agent/Multi-agent。

本轮修复第三个问题：浏览器标签A连接后，标签B连接同一对话会关闭A；前端按正常重连策略重连A时又关闭B，从而持续互相替换。起始保留第二个问题的三个未提交文件；用户明确授权保留这些改动、继续本轮修复并新增或调整相关回归测试。此前改动的原始字节已另存用于交付前核对。

## 实际读取并采用的 Codex 实现

主要来源为 openai/codex 固定提交 e742db9f7ba98e63e502b310857d49d047888518，通过 GitHub 连接器读取相关源码正文及测试，证据目录保存已读函数片段和固定链接：

| 来源 | 实际机制 | 本项目采用方式 |
| --- | --- | --- |
| [app-server/src/thread_state.rs](https://github.com/openai/codex/blob/e742db9f7ba98e63e502b310857d49d047888518/codex-rs/app-server/src/thread_state.rs) | ThreadEntry.connection_ids 是连接集合；try_add_connection_to_thread/try_ensure_connection_subscribed 加入订阅；unsubscribe_connection_from_thread/remove_connection 仅移除对应连接；还有订阅者时不清理整个对话 | active_connections 从每对话一个WebSocket改为每对话一组WebSocket；断开仅删除自身，集合为空时删除条目 |
| [app-server/src/outgoing_message.rs](https://github.com/openai/codex/blob/e742db9f7ba98e63e502b310857d49d047888518/codex-rs/app-server/src/outgoing_message.rs) | ThreadScopedOutgoingMessageSender 持有多个连接ID，订阅列表为空时直接返回；send_server_notification_to_connections 逐个发出定向通知；现成测试验证两个明确目标获得相同内容和时间戳 | 按对话订阅集合分发同一通知；每个连接独立处理运行筛选、重放及游标 |
| [app-server/src/transport.rs](https://github.com/openai/codex/blob/e742db9f7ba98e63e502b310857d49d047888518/codex-rs/app-server/src/transport.rs) | 每连接独立writer；可断开的客户端队列满或关闭时，只断开该连接，其他目标继续处理 | 复用本项目已有每连接锁和5秒发送超时，向各客户端并发投递；失败/超时只清理相应连接，记录错误并关闭该连接 |
| [app-server/src/request_processors/thread_processor_tests.rs](https://github.com/openai/codex/blob/e742db9f7ba98e63e502b310857d49d047888518/codex-rs/app-server/src/request_processors/thread_processor_tests.rs) | removing_auto_attached_connection_preserves_listener_for_other_connections 验证移除A后B仍订阅且监听不被取消；closed_connection_cannot_be_reintroduced_by_auto_subscribe 验证关闭连接不被重新注册 | 回归覆盖多个订阅者、重复断开和旧回放失败不影响后来的连接；发送前确认连接状态仍为当前登记对象 |

本轮直接采用其“一个对话对应多个连接、每连接独立投递和清理”的方式。上游是Rust及每连接writer队列，本项目是Python/Starlette，继续使用已有asyncio锁、超时和数据库重放协议；没有接入Rust运行时或增加兼容路径。未把取消前端重连作为修复方式。

## 生产修改与边界

生产修改仅在 api/monitor.py 的 ConnectionManager：

- connect 增加订阅者，不再覆盖或关闭其他连接；每个连接原有run_id、cursors、lock均独立保留。
- disconnect 仅移除对应WebSocket；重复断开或旧端点迟到清理不会移除其他连接。
- send_to_thread 获取订阅者快照并并发发送，空集合不发送；运行筛选继续按每个连接的run_id执行。锁内再次验证连接状态，已移除的连接不再开始投递。
- _catch_up 继续从持久化事件补齐缺口，游标只在发送成功后推进；同一连接的回放、实时补齐和pong继续受同一个锁保护。
- 单连接异常/发送超时记录日志并清理该连接；其他连接继续接收。业务完成记录和持久化历史不因客户端故障回滚。
- shutdown 先取消并等待已登记投递任务，再有界关闭全部登记连接，清空索引、数据库引用与事件循环引用。

未修改WebSocket路由、认证、所有权、事件协议、数据库结构、前端重连策略或前两个问题的生产修复。旧的“未登记连接也直接发送”分支已删除；相关历史测试改为走真实connect登记入口。既有重连测试迁移为允许多个连接共存的断言。

## 复现和验证

使用 diagnosing-bugs 流程。此前审计已定位单连接槽位替换，因此本轮直接固定双连接复现，不重复泛化猜测或加入临时生产日志。证据目录：[output/verification/multi-client-websocket-20261028/](../../output/verification/multi-client-websocket-20261028/)。

- 修复前运行9项新用例：8失败、1通过（before.xml/log，55.43秒）。最小复现是依次connect两个Socket后验证第一个没有被close；实际结果第一个已关闭。真实认证ASGI双连接也复现被替换后端点读取已关闭连接的RuntimeError。
- 修复后同批9项、6项既有事件回放及1项既有失败投递用例全部通过，共16项（after.xml/log，50.57秒）。
- 随后补充实际5秒发送超时，以及真实认证ASGI双连接的错误Origin、退出登录后的两个连接关闭、匿名连接拒绝断言，纳入最终回归。
- 新用例使用真实临时SQLite、真实认证中间件、WebSocket路由、连接管理器和事件存储；可控Socket用于固定回放与实时投递的交错，不调用模型或外部服务。验证不同游标/运行和对话筛选、乱序与重复通知、旧回放失败、断开后重连、单客户端故障、慢客户端隔离及关闭清理。
- 前端三个既有测试全部通过（frontend.log），覆盖断线重连、游标、去重、页面恢复和历史展示。

最终相关回归 **83项全部通过**（regression.xml/log，544.88秒），无skip/xfail。覆盖上述新用例，以及认证事件回放、历史、持久化、并发运行、生命周期与关闭、三助手协作、第二个问题的17项范围读取回归和第一个问题的3项超时落库回归。最终10项多客户端新回归已包含在83项内，不与前面的16项单独验证重复相加。另有3个既有前端测试全部通过。

Ruff与格式检查通过（lint.log、format.log）。源文件审计见source-audit.json及production.patch：生产AST变化只限ConnectionManager；上轮三个未提交文件保持逐字节一致；UTF-8、原BOM、CRLF及变更行空白均通过。汇总见validation-summary.json。最终回归开始后未再修改生产或测试源码。

交付前核对UTF-8、BOM、CRLF、变更行尾随空白及原有三个文件字节。按用户只允许git status的限制，不执行git diff --check或其他Git操作，改用原始字节快照、AST及difflib审计。未改写历史或生产数据库。
