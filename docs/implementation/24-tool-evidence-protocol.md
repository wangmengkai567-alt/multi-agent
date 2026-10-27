# 24 · 工具证据协议与上传材料接入

实施日期：2026-10-24 至 2026-10-25（Asia/Shanghai）。

## 范围与接口

本次完成实施顺序第②项：工具证据适配协议及现有工具接入。第①项的技能目录接口继续保留。任务意图路由、调用者认证和资源归属不并入本次修复。

新增 agent/runtime/evidence_protocol.py。每个生产 BaseTool 通过公开 metadata 声明版本化 evidence_adapter；没有声明、未知版本、重复声明或专家网络权限矛盾时明确拒绝。框架工具通过 middleware.tools 绑定预先审核的声明，新增未知框架工具要求显式接入，不根据名称猜测来源类型。专家契约保存工具 schema 和适配器 ID。

| 适配器 | 用途 | 事实来源 |
| --- | --- | --- |
| sql.v1 | 已执行的只读数据库结果 | sql |
| web-search.v1 / web-page.v1 | 提供方返回的搜索结果 / 实际可读正文 | web |
| document.v1 | 原始上传文件提取，及其确定性程序统计 | document / calculation |
| calculation.v1 | 原始上传输入上的程序计算 artifact | calculation |
| record.v1 | 开发者明确声明的一般工具文本结果 | record |
| control.v1 / network-control.v1 | 技能、文件查看、写文件、计划、委派、任务管理等控制操作 | 无 |
| research.v1 | 现有研究动作入口 | 由真实 provider attempt 单独采集，入口回显不重复作为证据 |

record.v1 是显式工具接入协议，不能自动证明官方身份、文件原文或计算过程。新增工具需要选择适配器并遵守其返回结构，不能仅把成功状态当成证据。

声明位于 @tool 外侧，例如：

~~~python
@evidence_tool("document.v1")
@tool(response_format="content_and_artifact")
def read_document(filename: str) -> tuple[str, dict]:
    ...
~~~

document.v1 的 artifact 必填 document（path、sha256、parser、selection）、sections 和 calculations；section 必填 key、title、locator、content；计算项另需 method、parameters。calculation.v1 必填 inputs 和 calculations。结构由严格 Pydantic schema 检查；SQL 和 record 适配器只接受实际文本，不接收未声明的 artifact。所有摘要是内容和输入的审计字段，不用于文件名。

## 证据、引文与交付

- 运行时签发记录的 logical scope、执行轮次、thread、所属任务、call_id 和 attempt，工具返回内容不能覆盖这些归属。工具成功返回必须恰有一个匹配 call_id 的 ToolMessage。
- 捕获点位于工具执行内层，先保存真实 content / artifact，再由框架进行大结果落盘替换；没有从替换提示或后续生成文件中恢复事实的替代通路。
- 记录保留适配器版本、清理敏感信息后的实际返回、请求参数、缓存原调用及 attempt、规范化 sources、文本摘要和记录完整性摘要。摘要用于一致性校验，不是调用者认证或数字签名。
- SQL、网页、文档和计算统一经过来源枚举、受预算限制的摘录、任务所属引文登记、报告解析、逐事实语义审核和终稿事实池。quote_id 只来自模型实际可见的连续原文；不允许模型自填来源或拼接省略段落。
- 每次消费重新验证记录与来源的一致性；缓存消费仍须匹配 served 原结果与原调用。子专家转交原始工具记录，父专家仍登记自己的引文并独立审核。
- 不再把无 URL 的来源一律显示为 SQL。报告和终稿按照上传材料、程序计算、SQL 执行或工具结果显示，并保留来源快照 ID；来源数据保留文件定位、输入摘要、提取器和参数。
- 网页官方身份继续由服务端来源策略确认。文件和程序结果不会因为无 URL 被自动排除，也不能满足官方网页的来源层级要求。
- 单条网页结果的格式、URL、正文无效时记录排除原因，保留同次有效结果和原始 artifact；顶层 artifact 协议错误明确失败。错误页、登录页、空正文不成为实际页面证据。没有增加备用搜索提供方或额外重试。

## 上传材料专家

默认注册 document 专家，使用 read_document，并经过现有报告与语义审核链；没有开启联网、任意脚本执行或额外模型重试。

只有本轮 uploads/ 下的原始只读上传文件能进入文档和计算证据。工具读取一次文件字节进行解析并生成 SHA-256；采集时核对当前文件、大小上限与摘要。技能文件、生成文档、其他轮次文件不可通过该入口变成原始材料。

实际读取范围：

- PDF：逐页提取文本并记录页码；没有 OCR，扫描图片不被声明为已识别原文。
- DOCX：正文段落及顶层表格行，保留段落/表/行号；不含图片、页眉页脚、嵌入文件。
- XLSX / XLS：只读取第一个工作表；前五行预览为文档提取，行列数及 DataFrame.describe 为独立 calculation 来源。输入记录实际表名、读取范围和 pandas 版本；计算记录方法与参数，不能冒充工作簿原文或扩展至其他工作表。
- UTF-8 文本：保留文本和行范围；不执行文件内容。

主助手 read_file_content 与框架 read_file 保持查看/技能用途，均明确为 control，不绕过事实审核。报告生成只看到预算内摘录和已登记引文，不把读取成功解释为所有内容都已被审核。

## 恢复与检查边界

checkpoint_schema_version 升至 16，review 契约升至 review-v13-tool-evidence-protocol。旧快照保留并中断，不自动迁移或按新证据语义续跑；数据库 schema 未改变。

恢复检查显式使用待恢复的 RunContext，不依赖尚未绑定的 ContextVar。保留的工具记录、来源快照、引文归属、正文摘要及原始文件输入均被检查；失败转为 interrupted。没有重新生成缺失证据或放宽旧版本校验的兼容通路。

按用户此前要求，本次没有新增、修改或运行测试；没有运行问答验收、真实模型/搜索请求、服务启动或数据库操作。仅执行 Ruff lint / format、Python AST、YAML、JavaScript 语法，以及文件 UTF-8/BOM/CRLF/尾随空白和测试文件摘要检查，结果见 24-static.log。静态检查不能证明运行时或业务验收通过。

测试中的旧工具声明、证据结构和 checkpoint 夹具需后续获准处理测试时迁移，本次不为旧夹具保留生产兼容分支。

修改留在工作区，未暂存或提交。项目只允许 git status，因此使用直接文件字节检查替代 git diff --check，未执行其他 Git 操作。
