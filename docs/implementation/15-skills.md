# 第 15 步：扩展信息调研、文档分析与数据分析 Skill

日期：2026-10-17（Asia/Shanghai）。状态：扩展完成，离线检查通过。

## 变更范围

新增三项项目业务 Skill，每项含正文与一份按需读取的参考资源：

- research：专题调研、多来源核查、竞品或方案比较；references/source-review.md 用于来源链、时效、口径与冲突核查。
- document-analysis：已有文档摘要、条款和证据提取、版本比较；references/evidence-map.md 用于引用定位、证据对照和读取覆盖检查。
- data-analysis：数据质量、分组统计、趋势、异常和指标解释；references/quality-checks.md 用于覆盖范围、重复、缺失、比率及比较口径。

每项的描述同时给出触发条件与常见排除场景；主正文提供流程和资源读取时机。没有新增脚本、依赖、工具或默认外部请求。原有 ecommerce-analysis 的描述收紧到经营复盘、诊断与内外对照，避免与通用数据分析和调研混淆；原有报告写作 Skill 继续按需组合。

## Runtime 接入

agent/runtime/skills.py 的 SKILL_FILES 注册正文和参考资源，SKILL_IDS 随注册表扩展到五项。沿用现有 API ID 校验、只读挂载、分页读取覆盖、正文哈希和 skill_state 事件，不另建旁路加载器。通用 Skill 提示按用户意图和上下文选择，允许不读取任何正文，并要求按阶段组合、按需读取资源。

主助手可发现五项元信息；显式 skill_ids 在首次模型调用前注入对应正文，附属资源仍按需读取。默认子助手的可见与可读范围保持一致：

| 助手 | 允许的 Skill |
|---|---|
| db | ecommerce-analysis、data-analysis |
| search | ecommerce-analysis、research |
| knowledge | document-analysis |

Skill 不改变 Runtime 的工具、SQL、文件权限、预算及业务流程前置条件；internal_external 仍必须显式选择 ecommerce-analysis。自然语言选择 Skill 不自动开启服务端业务模式。

当前主助手的 read_file_content 支持现有文档读取能力，但 Word 仅返回正文段落，PDF 无 OCR，Excel 只返回首个工作表预览及统计摘要。子助手保留自身工具集，不因加载 Skill 获得主助手的文件解析工具或任意 Python 执行工具。三项 Skill 均要求按实际可读范围限制结论，不声称完成工具尚不支持的分析。

## 页面与流程

页面新增三个复选框，实际 POST /api/task 提交相应稳定 ID。未勾选时保留模型按需选择机制，不使用关键词硬编码路由。时间线将新 ID 映射为“信息调研”“文档分析”“数据分析”，继续显示“已选择、正文已读取、参考资源已读取”；展开详情保留 actor、resource、reason 与关联 ID。

本次没有新增“为什么选择”或“无需 Skill”的独立决策事件。正文读取成功表示已取得说明，不等于已经遵循流程或完成业务任务。

## 触发边界用例

以下是设计预期，用于后续真实模型选择评估，不冒充本次已经测得的准确率：

| 用户请求 | 预期 |
|---|---|
| 调研近期竞品策略，比较不同来源的证据 | research |
| 总结上传 SOP 并找出退货例外条款 | document-analysis |
| 按月统计数据并比较不同渠道转化率 | data-analysis |
| 把已有分析结果整理成运营周报 | report-writing；无需重做调研或数据查询 |
| 分析电商经营下滑原因并对照市场 | ecommerce-analysis；具体调研和计算环节按需组合新增 Skill |
| 查昨天订单数 | 可直接调用必要助手，不强制完整 Skill 流程 |
| 解释“报告”这个词 | 无需业务 Skill |

## 验证与证据

- 三项新增 Skill 的 quick_validate.py 格式校验，以及生产前端 app.test.cjs / run-events.test.cjs 的实际输出见 [15-validation.log](15-validation.log)。前端覆盖三个新选项的提交和三种读取状态的时间线渲染。
- Ruff 检查、格式检查、既有 ecommerce-analysis 描述更新后的校验及 JavaScript 语法检查见 [15-checks.log](15-checks.log)。
- 后端针对 Skill、委派、Hook 和工具权限的集成回归：206 passed in 152.71s (0:02:32)，退出码 0。命令及输出见 [15-tests.log](15-tests.log)。覆盖五项 Skill 同步/异步正文与资源读取、显式选择首轮注入且不预读资源、分页加载、HTTP 参数、全部声明资源只读，以及三种默认子助手的新 Skill 正文加载、资源事件和未授权读取拒绝。
- 上述后端测试使用可控模型和真实框架，外部网络与真实凭据由测试配置禁用。它们验证 Runtime 接线与事件，不证明模型语义触发准确率；新增三项未进行真实模型或外部服务联调。
- 全部本次交付文本的 UTF-8、CRLF、BOM 和末尾空白检查见 [15-text-audit.log](15-text-audit.log)。新文件无 BOM，已有文件保留 BOM 设置。

遵循本项目 Git 限制，仅执行 git status；未执行配置查询、ls-files、diff、暂存、提交或其他 Git 操作。字节审计替代 git diff --check。修改前已有用户工作区变更，保留其内容，仅对相关文件做本次必要修改。
