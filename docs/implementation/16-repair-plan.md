# 16 · 多 Agent 协同与工具调用修复方案 v7

> 本版用于后续实施评审。当前仅修改方案，不修改代码、数据库、配置或测试。
>
> 保留参数重放与历史证据结论；补齐运行时状态、需求完整性、并发去重、逐 attempt 记账、恢复和终稿断言映射。实施阶段统一以 §5 的出口标准为准。
>
> 单一路径实施：不新增旧／新入口切换开关，不保留旧行为兼容分支。所有 Git 操作仅允许 `git status`；不新增或修改测试，回放与检查在获准实施时使用内存或 `output/replay/`，不能借目录名称绕过测试限制。

## 0. 目标、边界与顺序

目标是：原始必需需求不丢失；逐事实审核通过的有效事实可交付；失败与未知明确区分；拒绝与缓存命中不产生外发费用；每条终稿事实可追溯。

- **事实与覆盖分开**：事实正确不等于需求齐全；执行 `completed` 不等于覆盖 `complete`。
- **先有状态，再替换判定**：需求注册、逐事实审核、状态传递和两轴读写同批上线；工具入口使用这些已存在的状态。
- **证据级别用 F0–F3，验收层级用 L1–L4**，避免“L1 通过”同时表示语义审核和确定性回放。
- **历史观测与新设计分开**：§1–3 是原方案记录的冻结基线，不代表本次重新运行了真实模型或检索。迁移前先导出 run/result/event 清单，核实实际输入。
- **保留原始数据，不继承历史信任**：旧整份审核不能伪造成逐条审核；原始结果与原审核元数据完整保留，历史行不经兼容分支进入新的已核实交付池。
- **验证范围**：不运行真实检索、模型和端到端任务来完成本次文档优化；后续 S5 才执行一次真实验收。

| 阶段 | 本阶段完成的能力 | 依赖 |
|---|---|---|
| S0 | 冻结基线；统一状态、映射与迁移设计；在副本上验证迁移 | 无 |
| S1 | 需求注册 + 逐事实审核 + 两轴交付；一次切换存储、运行时与读取 | S0 |
| S2 | `research_step` 单入口 + 请求缓存 + 原子预留 + 唯一重试执行方 | S1 |
| S3 | 草稿指纹与审核复用 + 修订预算与时间预留 | S2 |
| S4 | 终稿断言映射 + 覆盖核对 + API／前端／恢复闭环 | S3 |
| S5 | 离线验收汇总 + 一次真实端到端 | S4 |

S0 不在生产读写链路提前调用新谓词，也不迁移正在服务的数据库。S1 同批启用新状态及其消费者；不留下“读方已升级、写方尚无字段”的中间运行版本。

---

## 1. 参数契约与单点修复基线

### 1.1 生产 schema 重测

脚本直接 import 生产 `internet_search.tool_call_schema`，不再自建类型：

```bash
uv run python output/p0c-replay/p0c_check.py
```

| # | 对象 | 生产基线 | 只修 `domain_array` | 再加字符串解码 |
|---|---|---|---|---|
| 1 | 华为 | REJECT `follow_up.include_domains`、`follow_up.include_raw_content` | 同左 | 同左 |
| 2 | 华为 | REJECT `include_domains` | **PASS** | PASS |
| 3 | 华为 | PASS | PASS | PASS |
| 4 | 小米 | REJECT `follow_up.sources` | 同左 | 同左 |
| 5 | 小米 | REJECT `follow_up`、`include_domains` | REJECT `follow_up` | **PASS** |
| 6 | 小米 | REJECT `follow_up`、`include_domains` | REJECT `follow_up`、`include_domains` | REJECT `include_domains` |
| 7 | Apple | REJECT `follow_up`、`topic` | 同左 | REJECT `topic` |
| 8 | Apple | REJECT `follow_up` | REJECT `follow_up` | **PASS** |
| — | **拒绝数** | **7/8** | **6/8** | **4/8** |

### 1.2 四类归因

| 类别 | 条目 | 性质 | 处理 |
|---|---|---|---|
| **A 纯程序缺陷** | #2（`include_domains: "null"` → `None` → 撞 `list[str]`） | `domain_array` 返回值与注解不符 | **修** |
| **B 字符串表达结构化参数** | #5、#8、#7 前半 | 接口复杂度的症状 | **不修**，S2 移除该类 |
| **C 模型真错** | #1 字段错层、#4 缺 `sources`、#6 域名带路径、#7 `topic="shopping"` | 未遵守契约 | 继续拒绝 |
| **D 协议拒绝** | #3 `search_prerequisite` | 非参数契约 | 由 `available_actions` 取代 |

### 1.3 结论

合法修复范围（A 类）只消掉 1 条：**7/8 → 6/8**，12.5%。不改变任何排期。B 类 3 条是"5 层嵌套必填"的直接代价，加兼容层等于承认该形态长期存在，与单入口方向相反。该样本仍有 4/8 条真实参数错误；这个样本比例不能解释为模型的一般错误概率。

### 1.4 `domain_array` 的修复边界

`tools/tavily_tool.py:63` `domain_array` 把"空"与"非法"混为一谈：

```python
if isinstance(value, list) and (not 1 <= len(value) <= 20 or any(...)):
    raise ValueError("include_domains must contain domain names without URL paths")
```

`[]` 撞 `not 1 <= 0 <= 20` → 抛"域名不能带路径"，错误信息与原因无关。**定死语义**：

| 输入 | 语义 | 处理 |
|---|---|---|
| 缺省 / `None` | 不过滤 | 保持 |
| `"null"` / `null` / `""` / `"[]"` / `[]` | 不过滤 | **新增**：统一归一为 `None` |
| `["a.com"]` | 过滤 | 保持 |
| `["https://a.com/x"]` | 模型真错 | 拒绝，错误信息改为"必须是不含协议与路径的裸域名" |
| > 20 项 | 越界 | 拒绝，错误信息改为"最多 20 个域名" |

类型必须写为 `Annotated[list[str] | None, BeforeValidator(domain_array)]`，让校验器作用于整个可空类型；不能让返回 `None` 的校验器仍被内层 `list[str]` 拒绝。空字符串先按上表处理，再解码其余 JSON 字符串；其他非法类型继续拒绝。

同时**删除函数体内重复的第二份校验**（`tavily_tool.py:99-108` 与 `domain_array` 完全重复），语义只在一处定义。`[]` 归 `None` 与"单入口、单行为"一致：空集合与缺省表达同一个意图。

---

## 2. 证据基线

### 2.1 四级事实状态

| 级别 | 含义 | 检查者 | 本轮 |
|---|---|---|---|
| F0 引文可追溯 | `quote` 逐字出现在该 `source_id` 正文 | `parse_report` | 三份全过 |
| F1 逐条语义通过 | 引文**支持**该 `value` | 审核模型 | **无一条经过** |
| F2 整份语义通过 | 无阻塞项 | 审核模型 | 仅华为 |
| F3 进入交付池 | 父代理可见 | `finalization` | 仅华为 8 条 |

可主张的只有：华为 8 条达 F3，其中"Pro 版 200 元券"不满足 F1（两条引文均不含该金额）；小米 4 条、Apple 4 条仅达 F0。

### 2.2 三个问题并列

双11 到手价三款全缺；渠道与优惠华为有（Pro 券不成立）、Apple 有券额无到手价、小米缺；使用差异华为 4 条、小米缺量化参数、Apple 整份被拒。只修交付层会把不成立的"200 元券"送进终稿。

---

## 3. 回放基线（含 events 可用性实测）

### 3.1 回放主源是落盘结果

`output/session_*/run_*/results/*.json` 的 `tools[]` 含 `name / call_id / executed / status / error_category / request_parameters / content`，**被拒绝的调用也在其中**，`request_parameters` 完整。这是历史请求与工具证据的回放主输入；逐事实语义预期另存人工核对的判定表，不能把旧整份 `passed` 当成逐条通过。

### 3.2 events 能读什么、不能读什么

`sanitize()`（`events.py:30-69`）按**键名**脱敏：键名含 `instruction`/`query`/`question`/`content`/`description`/`password`/`token` 等 → 值整个变 `[REDACTED]`；`args` 子树下所有字符串 → `[REDACTED]`。其余字符串截 512 字符保留。

实测 13 条 run / 988 条事件：

| 项 | 实测 |
|---|---|
| `answer_validation` 覆盖 | **3 条 run**（4 条记录），其余 10 条无此事件 |
| `data` 里可读字段 | `verdict`、`answer_kind`、`issues`、`missing_evidence` ✅ |
| `data` 里不可读字段 | `revision_instruction` ❌（键名含 `instruction`） |
| 工具参数 | ❌（`args` 子树全红） |
| `data.verdict` 与事件行 `status` 是否一致 | **不一致**：`424a486e` 行 `status='partial'`、`data.verdict='passed'`、`data.answer_kind='partial'` |
| 有 `answer_kind` 的记录 | **1 条**，值 `partial` |

**拆开两个命题：**

- "events 不能重建工具参数" —— **成立**，`args` 全红。
- "events 不能提供覆盖结论" —— **不成立**，`verdict` / `answer_kind` 可读。

**覆盖回填必须读最后一次有效终审事件的 `data.answer_kind`，不能读事件行 `status`，也不能以较早的成功覆盖后来的失败。** `424a486e` 的 `partial` 来源是 `data.answer_kind`；`08456423` 的审核未通过不能单独证明覆盖不足。

### 3.3 真实回填结果（先算清楚，再写迁移）

| 来源 | 条数 | 回填值 |
|---|---|---|
| `data.answer_kind == "complete"` | **0** | — |
| `data.answer_kind == "partial"` | 1 | `partial` |
| 有事件但无 `answer_kind` | 2 | `unknown` |
| 无 `answer_validation` 事件 | 10 | `unknown` |
| 合计 | 13 | `complete` 0 / `partial` 1 / `unknown` 12 |

**历史回填为 1 条 `partial`、12 条 `unknown`，没有一条可证的 `complete`。** 执行时按冻结基线清单核对，不把 13 条硬编码成适用于所有数据库的迁移规则。

L1 断言（精确版）：

- **`complete` 恰为 0 条**；
- **`partial` 恰为 1 条，且必须是 `424a486e`**（它本身是 `completed`）；
- **其余 12 条为 `unknown`**。

**`completed` 只描述执行结束，不描述覆盖。** 基线中 9 条 `completed` 包含 `424a486e`，不能断言这 9 条全部为 `unknown`。

### 3.4 已知盲区

子任务在 `results.save` 之前崩溃（超时）时，被拒调用只存在于 events 且 `args` 已红，无法回放。此类场景靠 L2 覆盖，不靠 L1。

---

## 4. 最终契约

### 4.1 需求注册与覆盖完整性

在 `task_plan.py` 创建不可变需求注册表。模型可提出候选，程序负责校验和登记，不接受模型自行登记 ID、增加必需条件或删除缺失条件。

```python
@dataclass(frozen=True)
class Requirement:
    rid: str
    parent_rid: str
    owner: Literal["task", "main"]
    task_id: str | None
    object_id: str | None
    anchor: str                    # 原始 user_query 的逐字片段
    query_version: int
    label: str
    model: str | None = None
    variant: str | None = None
    region: str | None = None
    channel: str | None = None
    window: str | None = None
    eligibility: str | None = None
    price_kind: str | None = None
    required_tier: tuple[str, ...] = ()
```

登记规则：

1. 原需求中的 `each` 按已登记对象展开为独立行，分别绑定稳定 `task_id/object_id`；综合对比、建议等归 `main`。不能用多个对象共用一格来掩盖漏查。
2. `anchor` 必须出现在当前版本原始问题中；结合上下文识别可选项，不得只截取“优先／如有”后面的名词来创建必需条件。未指定的地区、容量、渠道等保持未知，不由规划器补成硬要求。
3. 逐条记录原始需求子句与 rid 的对应关系；无法明确归属的子句登记为待判定需求，coverage 为 `unknown`，不能静默丢弃。主助手终审核对原始问题与完整注册表；逐字 anchor 只能证明来源，不能单独证明语义无遗漏。
4. 查询或验收要求仅在用户明确修改后升级 `query_version`；保留旧版本供审计，新版本重算 coverage，并使旧审核缓存失效。子助手不得改写版本。
5. 价格事实匹配 `model/variant/region/channel/window/eligibility/price_kind`；仅对需求明确指定的维度要求相等，缺少必要证据不能匹配。需求未指定的维度仍须保留事实实际适用范围，不推成通用结论。
6. `required_tier=()` 表示原需求未限制来源级别；有值时才应用分级条件。不把偏好自行升级为硬性门槛。

**覆盖响应必须完整且不可越权：**

```text
set(review.coverage) == set(本次 task/main 负责的全部 rid)
所有 coverage 引用的 fact_id 都属于本次允许访问的有效判定集
covered 的支持边只引用通过、未过时、无未解决冲突的事实／已核实分析
```

缺键、额外键、重复 rid、错误任务归属均为审核契约错误，不能聚合成通过。缺失格保留 `unknown`；没有需求行时不得利用 `all([])` 判 complete。注册表异常则本轮覆盖为 `unknown`，并记录原因。covered 表示该格全部必需子项得到充分支持；partial 表示只满足一部分；missing 表示审核确认尚无充分覆盖；无法判定时为 unknown。main 可访问各任务最新有效事实的并集，但不得恢复被拒或过时的报告版本。

### 4.2 Gap、请求缓存与并发

```python
@dataclass
class Gap:
    gid: str
    rid: str
    task_id: str
    reason: Literal["plan", "review_missing", "fact_rejected", "claim"]
    source_fact_id: str | None
    status: Literal["open", "searching", "covered", "exhausted", "blocked", "dropped"]
    searches: int
    reads: int
    no_evidence_streak: int
    consecutive_technical_failures: int
    covered_by: list[str]
```

- 需求登记后，程序为各任务未覆盖格初始化 gap。S1 已能审核和重算 covered，S2 使用该能力，不依赖未来阶段。
- `open → searching` 在取得请求执行所有权时发生；完成后按有效事实重算，仍无覆盖则回 `open`，达到停止条件才进入 `exhausted/blocked`。
- `covered` 必须同时具备 F1 通过事实、适用范围匹配和来源条件满足；新网页或非空搜索结果不等于 covered。
- 剔除某条事实后，重新计算该格全部剩余支持边；还有充分支持则保持 covered，不能一律降 missing。失去全部充分支持时重开对应 gap。
- `dropped` 必须有 `requirement_changed/claim_resolved/superseded` 的可追溯原因；`claim` 必须绑定 `source_fact_id`，支持变化后重算，不能静默失效。
- `searches/reads` 按该 gap 发起的逻辑请求计；共享请求的消费者另记消费引用，不伪造额外外发。技术失败连续计数在一次成功响应后清零。

**请求 key 从适配层构造的实际请求生成：**

```text
request_key = (run_id, provider, kind, canonical_request_json)
```

`canonical_request_json` 仅排序对象键；请求值与实际发送值完全一致。搜索包含 query、topic、search_depth、include_domains、max_results、include_raw_content 及其他实际影响结果的参数；读取包含 URL、query、extract_depth、chunks_per_source、format。API 密钥和传输超时不进入结果缓存 key；供应方或语义参数变化必须改变 key。

不对 query 做 `casefold/NFKC/去标点` 后再合并；区分 `1 mW` 与 `1 MW`、版本号和原始标点。只有接口明确声明无序的集合可排序去重，并将同一规范化值实际发出。`gid/task_id` 不进入请求 key，缓存严格限定同一 run 的公开检索证据；不跨 run 复用动态价格。预留失败时在同一锁域撤销新登记的执行所有权，等待者收到本地失败，不各自启动重试。

处理顺序：任务与来源归属、字段和动作合法性 → 缓存状态 → 原子预留 → 外发。covered/dropped 等关闭 gap 不得通过缓存绕过动作限制。

| 状态 | 行为 | 外发与预算 |
|---|---|---|
| `served` | 将原始证据关联到消费任务，返回缓存结果 | 0 外发、0 工具／搜索／读取／重试扣额 |
| `in_flight` | 等待同 key 的唯一执行者；不持预算锁等待 | 等待者不重复预留、不重复外发 |
| `empty` | 返回 `duplicate_empty`，要求改用其他合法动作 | 0 外发、0 扣额 |
| `failed` | 只有唯一重试执行方按策略安排下一 attempt | 禁止多个等待者分别自动重试 |
| 不存在 | 锁内登记唯一执行所有权并原子预留 | 仅执行者承担预算 |

每条共享证据保留 `origin_call_id/attempt` 和各消费任务的 `consumer_call_id/gap_id/rid/cache_hit`。同一来源加入新任务的本地证据目录后才可引用；不得借用其他任务的结论、提示或审核。重复消费不重复累加 `evidence_version`。

缓存消费记录如实保存 `executed=False/cache_hit=True`，通过 origin 引用解析到真实 completed 响应；不能伪造一次执行。统一 `evidence_available` 判据供 reporter、SearchHistory、local_sources、ReviewContract 和 finalization 使用：真实完成的执行，或本地已登记且能解析到真实完成响应的缓存消费，才可作为证据。仅保留旧 `executed` 过滤会让缓存来源整体消失，必须同批替换。

`no_evidence_streak` 按 gap 本地已知的 `(source_id, content_version)` 计算：外发空结果或全为已知版本则 +1；新来源、内容变化或读取取得新正文则归零。缓存证据首次进入该 gap 可归零，重复命中不增加；非法请求和技术失败不增加。连续 2 次无新增停止该 gap 本轮扩展，标 `exhausted`；连续 2 次技术失败标 `blocked`。**exhausted 只表示本轮无进展，不能据此声称网页上不存在资料。**

等待者取消不取消其他消费者仍需的请求。执行者取消／超时后按 §4.6 确认是否已经发送；迟到回调使用请求所有权版本校验，不能覆盖新 attempt 的状态。

### 4.3 来源分级与证据版本

来源分级至少保存 `publisher/channel_type/authorization/applicability/tier/source_id/content_version`。`channel_type` 可含 ugc；无法确认的最终 tier 为 `unknown`。

程序按受控域名规则和本次页面证据分级；页面自称“授权／官方”不足以单独证明授权。授权与适用条件均保存可追溯原文，不能用模型自行填写的标签代替。规则没有充分依据时保留 unknown。

分开计算：

- `fact_establishes`：逐条引文是否支持该断言及其口径。
- `source_satisfies(rid)`：来源级别与适用范围是否满足该格要求。

第三方标价字面成立、但不满足官方到手价要求时，可作为注明口径的参考事实，不覆盖该格。判定绑定具体正文版本；来源更新后，使用旧正文的审核不能自动套到新正文。

### 4.4 统一状态与存储

#### 4.4.1 运行时只接受同一种状态对象

```python
@dataclass(frozen=True)
class ReviewState:
    judged: bool
    facts_status: Literal["all_passed", "some_passed", "none_passed"] | None
    coverage_status: Literal["covered", "missing", "unknown"] | None
    check_status: str


def deliverable(state: ReviewState) -> bool:
    return state.judged and state.facts_status in ("all_passed", "some_passed")


def publishable(state: ReviewState, summary: str | None) -> bool:
    return (deliverable(state)
            and state.coverage_status in ("covered", "missing")
            and summary is not None and len(summary.strip(" ")) > 0)
```

所有外部数据先显式解码并校验为 `ReviewState`；谓词不接收混合 dict／任意对象，不用 `getattr` 默认值掩盖缺字段。summary 先从通过事实生成并 `.strip()`，空结果存 NULL；上述内容条件与 SQLite `length(trim(summary)) > 0` 一致。

| 载体／路径 | 最终职责 | 上线阶段 |
|---|---|---|
| `ReviewDecision/ReviewResult` | 每条事实结论、完整 coverage、契约版本、错误／终态信息；程序派生 ReviewState | S1 |
| `HookCall`、delegation 内存 record | 显式携带 state、当前 result_id、report_attempt | S1 |
| `SearchTaskBudget` | 携带 state、有效结构化报告、evidence_version、当前 result_id | S1 |
| `RunContext` | 需求／gap 注册表、query_version、run coverage；移除 delivery_status 双源 | S1 |
| `results` | 两轴、判定标记、派生标签与审核快照的权威持久化记录 | S1 |
| `subtasks` | 增加 `current_result_id`；check_status 仅展示镜像，不再作为结果资格闸门 | S1 |
| `ResultService.save → _checked → _finish` | 接收同一份已经过滤／判定的快照，禁止先发布原草稿再清理 | S1 |
| checkpoint 写入／恢复 | 保存版本与引用；恢复后按 result_id 对齐数据库状态，不继承过时镜像 | 基础 S1；缓存 S2；交付映射 S4 |
| API／事件／前端 | 传递 state 与 coverage；前端只显示派生状态，不另算审核结论 | 字段 S1；终稿闭环 S4 |

`current_result_id` 指向本子任务最新一次已保存结果；写结果、更新该指针和记录 result_saved 事件在同一事务内完成。新失败 attempt 不能继续复用旧成功 attempt 的资格。`current_result_id` 的更新校验该结果确属同一 subtask/run/thread，读取时也校验；指针不能只按 UUID 存在性放行。跨报告事实 ID 用 `(task_id, report_attempt, local_fact_id)` 定位，并保留原始 local id；不得按聚合列表的临时序号生成身份。

#### 4.4.2 两轴映射

| facts_status | coverage_status | check_status | 可交付 | 内容满足发布资格 |
|---|---|---|---|---|
| all_passed | covered | passed | 是 | 是 |
| all_passed | missing | passed_with_gap | 是 | 是 |
| all_passed | unknown | passed_coverage_unknown | 是，附未知标注 | 否 |
| some_passed | covered | partial_passed | 是 | 是 |
| some_passed | missing | partial_passed_with_gap | 是 | 是 |
| some_passed | unknown | partial_coverage_unknown | 是，附未知标注 | 否 |
| none_passed | 任意 | insufficient | 否 | 否 |
| NULL | NULL | not_checked／tool_failed／dependency_unavailable／timed_out／check_incomplete／budget_exhausted | 否 | 否 |

前七行要求 judged=1，最后一行要求 judged=0。`label` 是确定性展示投影，不要求单射：none_passed 的三种覆盖可以同名；权威信息是两轴。Python 映射与 SQL CASE 从同一份映射定义生成。

**判定完整性：** 每个当前草稿 fact 必须恰有一次判定，未知／重复 fact_id 也拒绝；覆盖 key 集必须满足 §4.1。契约不完整时记 `check_incomplete`、judged=0、两轴 NULL，不得把漏审事实当成通过，也不缓存为终态审核。有效判定中零通过（含空事实数组）为 none_passed；全部通过为 all_passed；其余为 some_passed。

数据库助手同样纳入 state 传递，不能把旧整份 passed 直接转换成 all_passed：将可交付内部结论显式绑定已执行 SQL／查询结果并逐条审核，再生成同一状态。保留现有只读 SQL 和权限校验；内部证据采用其真实 SQL 引用，不伪造网页 source_id。这一适配属于 S1，不能遗漏 internal_external 的准入和 handoff。

`review.verdict` 只表示本次原草稿的整体审核结论：判定完整且全部事实通过、负责需求全部 covered 时为 passed；有效审核仍有拒绝事实或缺口时为 insufficient。它不决定部分事实能否交付；终态只表示这次审核已完成。修订器按下列 finding 和 gap 决定是否补查／重写，不再机械地把 insufficient 当成整份丢弃。

finding.kind 统一为 `missing_requirement/unsupported_claim/contradicted_claim/bundled_assertion/expression_issue`；每条绑定 rid 或 fact_id，只有原始必需需求仍缺证据时才创建研究 gap。旧 `missing_evidence` 文本仅作诊断，不继续作为前缀匹配或另一个触发枚举。

逐条过滤后同时重算 `facts/analysis/superseded/conflicts/sources/coverage/origins`。分析依赖被删事实时删除对应分析；被拒事实不能藏在冲突展示或旧摘要中进入终稿。`summary` 和交付 report 都从同一份有效事实快照生成，原始完整草稿仅保留作审计。

#### 4.4.3 数据库迁移契约

schema 版本为 6；支持现有 0–5 顺序迁移，也支持已是 6 时无变更重开。S0 在隔离副本上验证，S1 才与新版读写链路一次启用。

`runs` 增加 coverage（unknown 默认值与三值 CHECK）；`subtasks` 增加可空 `current_result_id`；`results` 重建。以下为 **results 新约束片段**，不是完整可直接运行的建表脚本；未变列、PK、FK、UNIQUE、原文二选一约束必须完整保留。

```sql
judged INTEGER NOT NULL DEFAULT 0 CHECK(judged IN (0, 1)),
facts_status TEXT CHECK(facts_status IN ('all_passed','some_passed','none_passed')),
coverage_status TEXT CHECK(coverage_status IN ('covered','missing','unknown')),
CHECK(
    (judged = 0 AND facts_status IS NULL AND coverage_status IS NULL)
    OR (judged = 1 AND facts_status IS NOT NULL AND coverage_status IS NOT NULL)
),
CHECK(
    (judged = 0 AND check_status IN (
        'not_checked','tool_failed','dependency_unavailable',
        'timed_out','check_incomplete','budget_exhausted'))
    OR (judged = 1 AND check_status = CASE
        WHEN facts_status = 'none_passed' THEN 'insufficient'
        WHEN coverage_status = 'covered' THEN CASE facts_status
            WHEN 'all_passed' THEN 'passed' ELSE 'partial_passed' END
        WHEN coverage_status = 'missing' THEN CASE facts_status
            WHEN 'all_passed' THEN 'passed_with_gap' ELSE 'partial_passed_with_gap' END
        ELSE CASE facts_status
            WHEN 'all_passed' THEN 'passed_coverage_unknown' ELSE 'partial_coverage_unknown' END
    END)
),
CHECK(published = 0 OR (
    judged = 1 AND facts_status IN ('all_passed','some_passed')
    AND coverage_status IN ('covered','missing')
    AND summary IS NOT NULL AND length(trim(summary)) > 0
))
```

- 迁移在 `BEGIN IMMEDIATE` 事务内完成：导出结构清单 → 建新表 → 显式列名复制 → 重建索引 → 完整性／外键检查 → user_version=6 → COMMIT；失败全部回滚。不要用 `SELECT *` 猜新列顺序。
- 对 `results_by_run` 等显式索引保存并恢复 `sqlite_schema.sql`；UNIQUE/PK 的自动索引由约束生成，不能逐条执行 CREATE。对比索引列、顺序、唯一性和 partial 条件，不依赖 PRAGMA 返回行顺序；同时核对 FK、行数、主键集合、raw 路径与摘要哈希。
- 旧行全部 judged=0、两轴 NULL。旧 check_status／summary／published 原值保存进 `review.migration_origin`，不丢弃历史证据；当前标签使用允许的前置态，旧 passed／insufficient 归 not_checked，published=0、当前 summary=NULL。历史界面显示“历史整份审核，未完成逐事实判定”，不伪装成新审核失败。
- 原始 text/path/hash、result_id、run_id、call_id、attempt 不变；不自动重新请求模型，不删除或归档用户原始数据。历史 run coverage 按 §3 事件规则回填，冻结样本为 0 complete／1 partial／12 unknown；此为覆盖记录，不赋予历史事实新资格。
- 各 subtask 的 current_result_id 由该 call 的最大 attempt 确定；有歧义则迁移失败并报告，不能任取旧通过行。无结果时为 NULL。
- 写 published 前，事务内额外检查 run/task 仍处于允许发布的执行阶段；迟到结果可落原文但 published=0、summary=NULL。SQL 的行内资格与运行生命周期检查分别验收。
- 上线前停止旧 worker 对目标库写入，并盘点未完成后台任务。旧 checkpoint 不自动按新 schema 继续执行：显式标 interrupted 并保留文件；只有新 checkpoint_schema_version 可恢复，不新增双解析兼容路径。

没有 `judged=0 → check_status=='passed'` 的兼容资格分支，也没有等待“历史行退休”才能删的旧运行路径。

#### 4.4.4 读取与 coverage 聚合

区分两种读取用途，移除语义不清的 `usable_only`：

- **handoff**：只读当前任务 latest result 中 deliverable 的有效结构化报告；允许 coverage unknown，必须带未知标记。不能要求 published=1 才交给父代理，也不能回传原草稿来代替过滤报告。
- **published**：只读 published=1 的结果摘要，且记录仍满足 publishable；保留正确的任务完成／归属限制。

两者都校验 run/thread/task 归属，使用 `subtasks.current_result_id = results.result_id` 选择当前结果；去掉 JOIN 中 `s.check_status='passed'` 等展示标签资格判断。历史原文读取单独保留审计权限，不进入上述资格池。

需求级：covered／partial／missing／unknown；结果级：covered／missing／unknown；run 级：complete／partial／unknown。

| 聚合 | 规则 |
|---|---|
| 需求 → result | 只遍历该 result 应负责的非空 rid 集：缺键／unknown → unknown；全部 covered → covered；其余 → missing |
| 需求 → run | 遍历当前版本完整注册表（含 main）：存在未知／未判定 → unknown；全部 covered → complete；其余 → partial |
| 事实删除／来源变化 | 基于剩余有效支持边重新计算；不能直接按删除数量降级 |
| 终稿省略必需内容 | 对应交付 coverage 重新核对；研究资料齐全但终稿没回答，不能仍标 complete |

审核模型提供逐格结论和支持边，程序验证并聚合；run.coverage 是对外唯一来源。删除 `context.delivery_status` 的赋值、checkpoint 字段、API 别名和前端独立推断；审核未完成时只可 unknown。没有定义额外审核次数与比较标准前，不使用“审核一致率不足”这种无法执行的条件。

### 4.5 修改清单与扫描闸门

原快照中 Python `check_status` 有 41 行、`verdict ==/!= "passed"` 有 10 行。这是定位基线，**不是新方案修改总数或验收阈值**；新增字段传播、恢复和 JS 点位也必须登记。

| 范围 | 当前关键位置（行号仅供原快照定位） | 修改责任 | 阶段 |
|---|---|---|---|
| 状态定义 | `search.py:16`、`hooks.py:30`、`reflection.py`、`review_contract.py` | 统一 State、逐事实／coverage 完整性 | S1 |
| 写入与返回 | `delegation.py:180/223/225/381`、`results.py:132–144` | 先构造有效报告，再同源落库与回传 | S1 |
| 汇总与终审 | `facts.py:205`、`finalization.py:26/42/44/131/145/151/154`、`answer_review.py:33/69/188/195` | 用 deliverable 收集有效事实，删除失败草稿泄漏 | S1；交付映射 S4 |
| 存储 | `run_store.py:229/231/259` 及迁移 | 两轴重算、内容资格、latest 指针、分用途读取 | S1 |
| 需求与来源 | `task_plan.py`、`review_contract.py:129–145`、`facts.py`、`retrieval.py` | 注册表、支持边、版本与来源适用条件 | S1 |
| 工具与预算 | `tavily_tool.py`、`policies.py:489–511`、`hooks.py`、`recovery.py`、`search.py:143/160` | 入口替换、缓存、预留、重试与发送账本 | S2 |
| 报告证据 | `reporting.py:56`、`evidence.py`、`facts.py:100`、`retrieval.py`、`review_contract.py` | 统一 evidence_available，真实执行与可解析缓存消费均可用，不伪造 executed | S2 |
| 修订与治理 | `delegation.py:301`、`governance.py`、`models.py`、`policies.py` | 审核复用、分类预留、时间约束 | S3 |
| run／后台／恢复 | `context.py`、`checkpoints.py`、`server.py:107/437/463` | 单一 coverage、state／缓存／预算快照及版本校验 | S1/S2；整体验收 S4 |
| API 与前端 | `run_store.py` 查询／解码、`api/static/app.js:14/95/109/218` | 新字段与标签、unknown 显示、重连一致 | 字段与唯一状态来源 S1；交付核对 S4 |

扫描范围包括 `agent api tools utils` 的 Python、JS、SQL、配置及提示词，不只按变量名匹配。使用 Windows 可执行的 rg 命令收集候选：

```powershell
rg -n 'check_status|facts_status|coverage_status|delivery_status|answer_kind|current_result_id' agent api tools utils
rg -n 'passed|usable_only|reserve_search|reserve_page|research_step|internet_search|read_web_page' agent api tools utils config prompt
```

逐项登记到回放清单：文件、函数／SQL／JS 位置、用途、负责阶段、最终保留原因。分类为 `predicate/display/transport/migration/pre_review/obsolete`；任一未分类出现处使闸门失败。行号变化不等于完成，出现次数变少也不等于安全。

S1 出口：状态读写、资格 SQL、前端字段和历史迁移无旧资格判断；S2 出口：无旧入口可达、无重复预留／重试方；S1 同时清除 delivery_status 双源和独立前端审核推断；S4 验证其在最终交付与重连恢复中仍一致。纯显示映射与原始审计字段可保留 passed 字符串，不要求全仓机械清零。

### 4.6 工具单入口、重试与预算

S2 后模型只见 research_step，internet_search/read_web_page 仅作为内部 provider 实现和证据名称。适配层调用不经过第二套工具 hooks／预留／重试；不新增 `bill=False` 绕过通用框架的平行调用路径。

| action | 必填 | 禁止 | 适配层行为 |
|---|---|---|---|
| search | gap_id、query | source_id | 校验当前任务 open gap，构造完整搜索请求 |
| read | gap_id、source_id | query | 校验当前任务来源；按该 rid 的原始 anchor、条件和缺口构造读取 query |
| report | 无 | gap_id、query、source_id | 结束采集，调用现有唯一报告生成流程，不暗中研究 |

未知参数拒绝；reason 是有界诊断字符串，不作为事实证据。返回统一字段 `allowed_actions/open_gaps/readable_sources/reason`，非法请求不扣额。连续 2 次非法请求明确结束采集，只保留 report；这是采集状态转换，不调用旧入口补救。报告动作触发的模型调用按 sub_model 记账，不算搜索／读取外发。

`available_actions` 从同一状态机计算；search/read 需满足任务归属、可用 gap/source、预算和时间。结果仅为当前快照，执行时仍在锁内重新校验，不能用先前返回的动作绕过并发预算变化。

**单一执行责任：** research_step 适配层拥有预留、传输重试和证据记录。现有 hooks/recovery 对该工具不再执行外层重试；复用 recovery 的纯错误分类／退避计算，不复用其执行循环。旧工具不注册为模型工具，相关白名单与提示同步移除。SDK 自动重试必须关闭；若不能确认一 attempt 只触发一次发送，S2 不得通过。

**三本账，禁止混用：**

| 名称 | 定义 |
|---|---|
| logical_calls | 执行者取得的逻辑操作数；同一 call_id 的重试仍是一个逻辑操作（传输 attempt 与报告 report_attempt 分别编号） |
| search_attempts/read_attempts | 实际进入 provider 发送边界的 attempt 数，每次发送各 +1 |
| budget_ledger | reserved／spent／released，按 `(run_id, kind, call_id, attempt)` 唯一定位 |

每次 attempt 原子预留 `tool + search/read`；attempt>0 另加 retry。缓存和等待者不创建预留。现有 SearchAllowance 依 call_id 集合去重的搜索／读取扣额必须替换为 attempt 账本，不能只增加计数器。每轮工具上限统计获准的模型工具调用，传输重试不重复占模型回合槽位，但仍消耗 tool/retry/total 与搜索或读取额度。

| 时点 | 搜索／读取及 tool/retry 预算 | 请求状态 |
|---|---|---|
| 预留前本地拒绝 | 不变 | rejected |
| 已预留，尚未发送即取消／失败 | 一次性释放该 attempt；仅逻辑调用从未发送时归还其回合槽位 | released |
| 进入发送边界 | reserved → spent，一次性结算 | sent |
| 成功响应 | 保留 spent | completed／empty |
| 已发送后超时／取消 | 保留 spent，不宣称未计费 | uncertain |
| 可重试的已发送失败 | 保留本次 spent，下一 attempt 重新预留 | failed → retry |

“发送边界”定义为调用受控 HTTP transport 的 send 前；不代表服务端实际收到，也不代表供应商账单收费。到达边界的连接失败按保守消耗记账。预留与结算使用现有 budget_lock 保证原子性，等待、网络与退避均不持锁。

证据 tools[] 保留内层 `name`、共享 call_id、attempt、via=research_step、gap_id/rid、程序构造的 request_parameters 和模型意图。每次失败 attempt 都记录；共享消费引用可追溯到唯一原始响应。原始响应不为适配器重新截断；原有模型摘录预算保持。

恢复时，未确认已发出的预留不能直接释放并重新发送。执行中请求记 uncertain，保留消耗；后续显式重试使用新 attempt。保存证据目录、请求状态／所有权版本与账本，避免重启后重复外发和重复退款。

### 4.7 发送审计

在实际 HTTP transport 的 send 边界统一挂载审计，不使用“无 session 就静默跳过”的计数器。启动时确认 search/read 都经过该边界；审计不可用则依赖不可用，不放行无记录发送。

- 每次发送记录 `run_id/kind/call_id/attempt/request_key/send_seq`；响应钩子只补结果，不负责计数。
- 计数器存放在 RunContext 的共享、加锁账本中；ContextVar 仅传递该共享引用，不能依赖工作线程修改 ContextVar 标量回传父协程。
- 每种 kind 的 `outbound_attempts == spent_attempts`；不存在重复 send 的相同 attempt，也不存在没有预留的发送。累计值从账本导出，不能让计数器与扣额代码读取同一个自增变量后自证一致。
- 本地拒绝、缓存、in_flight 等待的 outbound/spent 增量均为 0；回放禁用网络，outbound 必须为 0。
- 晚到响应只结算既有 attempt，不重复扣额／退款；审计记录随 checkpoint 与执行事件保留，最终检查在 worker 已结束或明确记 uncertain 后进行。

### 4.8 终稿断言映射

S4 增加内部 DeliveryDraft，不在用户界面展示实现字段。包含确定顺序的文本块、逐块 claim、支持 fact_ids、rid、分析依赖和未覆盖需求；程序从该结构渲染最终正文，禁止另生成一份未映射的事实段落。

每条具体外部事实断言绑定至少一个当前有效的通过事实；每条分析绑定已审核的 analysis 及其有效事实依赖。claim 包含精确 scope、时间和金额口径，终审核对语义蕴含，**绑定了合法 fact_id 本身不等于断言成立**。事实 ID 不因换写法重新生成，删改文本同步更新映射并重新终审。

```text
终稿支持 fact_id 集 ⊆ 当前可交付、未过时、未冲突的通过事实集
终稿覆盖的 rid 集及各格结论，按实际回答重新核对
```

不要求每条通过事实都原样进入正文：重复、背景、过时及未解决冲突事实不能被“全进入”条件强制发布。每条通过事实必须进入可追溯有效池，未采用时记录 `duplicate/background/superseded/conflict/not_needed` 等具体原因；若省略造成必需内容缺失，则交付 coverage 降级。

同一 source_id/quote 可支持多条不同断言，通过与被拒断言也可能共用一段引文。因此不按 value 子串或 source/quote 黑名单判泄漏；按终稿 claim 的支持事实与语义判断。历史原稿、被拒 findings 和 conflicts 不能作为额外事实输入。内部数据库结论使用 S1 已核实的内部断言 ID 与现有 SQL 证据校验；DeliveryDraft 显式区分事实来源类型，不能将内部自由文本虚标成外部事实 ID。

---

## 5. 实施步骤与唯一出口标准

### S0 · 冻结基线与契约准备

1. 导出 §1–3 对应的 run/result/event 清单和三份原始结果，放入 output/replay；记录文件来源和 SHA-256 内容摘要，不在文件名追加哈希。
2. 固定八条参数案例、13 条历史 coverage 的预期，以及人工核对的事实语义表；模型响应快照与人工预期明确区分。
3. 按 §4.4 定义 State、字段传递图、映射表、完整迁移 SQL 和 checkpoint 版本；当前生产读写方尚不替换。
4. 用隔离数据库副本验证迁移；原库不改写。`domain_array` 修复作为明确独立项，按 §1.4 验收，不把它当成工具架构有效性的证明。

**出口：** 基线来源可定位、八条归因一致；历史覆盖 0/1/12；旧审核信息和原始结果不丢失；非法 judged、半空轴、错标签、非法发布均被约束拒绝；显式索引／FK／行数／raw 完整性保留；同版本重开无重复迁移；状态消费者与阶段责任清单完整。S0 不要求尚未启用的新状态在生产数据中出现。

### S1 · 需求、逐事实审核与两轴交付

1. 实现 §4.1 的注册表、each 展开、main 行、完整性校验和 query_version；初始化 gap，提供真实的 covered 重算能力。
2. ReviewDecision 改为逐事实判定 + findings（绑定事实／需求）+ 完整 coverage + 支持边；删除旧“passed 当且仅当 findings 为空”的整体互斥规则。合法的“部分通过且有 findings”可被表达。
3. 审核以当前对象任务为边界；生成阶段拆分 bundled_assertion，发现打包断言交回修订，不按正则猜分句。漏审或非法 ID 返回 check_incomplete，不制造通过。
4. 按 §4.4 过滤完整报告并生成摘要；ResultService 与 delegation 接收同一个已判定快照。修改所有读写和状态载体，数据库迁移、新写路径、handoff／published SQL、checkpoint 和基础 API／JS 字段同批启用，同时删除 delivery_status 双源和前端根据 passed 日志自行推断完整性的代码。
5. 历史行只显示原审核记录，不进入当前资格池；后台旧 checkpoint 显式中断，不能默认继续。

**出口：** 字段创建→审核→保存→任务→恢复→读取全链路一致；所有映射组合可表达，未知覆盖的已通过事实可交付但不可发布；最新失败不复用旧成功；剔除错误事实后其他有效事实保留；替代事实仍可维持 coverage；缺失／额外 rid 不会 complete；Apple 打包断言拆分、华为 Pro 200 元券不通过、小米／Apple 不越界承担综合推荐。对应预期来自冻结语义表，禁止把预期写死进生产逻辑。

### S2 · 单入口、缓存与原子预算

1. 实现 §4.6 research_step 及其动作参数；read 同时绑定 gap/source，适配层生成读取问题。
2. 实现 §4.2 精确请求 key、in_flight 单执行者、同 run 跨 gap 公开证据复用与本地消费登记。
3. 将预算按 attempt 原子预留／结算，覆盖搜索、读取、tool、retry、total 和回合上限；本地流程与发送边界分离。
4. 适配层成为该入口唯一重试执行方，移除外层重试、SDK 自动重试和旧 follow_up 协议；接入独立发送审计。
5. 在同一替换阶段移除旧模型入口、SearchFollowUp、SourceAssessment、validate_follow_up、仅供旧协议使用的 guidance/blocked_read_sources/stalled 及提示／配置引用；保留仍承担来源目录职责的代码。历史证据工具名保留。

**出口：** 无旧入口／开关可达；缓存消费可经统一证据判据进入 reporter／parser／审核器；本地拒绝、缓存和等待均零外发零扣额；并发相同 key 只有一个执行者；basic/advanced、读取不同 query 不错误合并；429／连接失败／超时重试每 attempt 独立扣额且无两层重试；预发送取消释放、发送后取消保留；搜索与读取都无预扣泄漏；原始工具证据可被 reporter 与 parser 读取；恢复不重置预算或遗失消费来源。

### S3 · 无变化审核复用与修订预留

**草稿指纹：** 结构化报告通过 schema 校验后，用 `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))` 的 UTF-8 字节计算 SHA-256。只消除 JSON 对象键序与传输排版差异，保留字符串内容、数字／单位、事实 ID、数组顺序；不得调用搜索查询 normalize。非结构化报告使用原始 UTF-8 字节，不推断“语义相同”。指纹在附加全局 report_attempt 身份之前计算，保留模型提交的 local_fact_id；复用结论时按 local_fact_id 映射到当前报告身份，不能把旧 attempt 的引用直接复制过去。

```text
review_key = (task_id, report_fingerprint, evidence_version,
              query_version, reviewer_contract_version,
              source_policy_version, reviewer_model_config_version)
```

evidence_version 仅在该任务实际可见的来源集合／正文版本变化时递增；无关任务或重复缓存消费不递增。reviewer_model_config_version 包含影响审核的模型／提示／配置版本。以上版本由程序维护，检查前取快照。

复用要求同时满足：完整 key 相同；上次审核为成功解析的终态 passed/insufficient；error=None；事实与 coverage 完整性均通过。check_incomplete/tool_failed/timed_out/budget_exhausted 均不复用；相同 key 的已完成 insufficient 不安排 R3 反复抽取模型结论。复用旧 insufficient 只表示不重复审核，仍交回原缺口或结束受限修订，不能变成通过。指纹相同事件保存真实审核引用及零新增审核调用。

| 类型 | 触发 | 预留下限 |
|---|---|---|
| R1 重写 | bundled_assertion／表述问题 | sub_model 1 + review 1 |
| R2 补查后重写 | missing_requirement，或必要事实缺证据且存在合法 gap | sub_model 1 + review 1 + search attempt 1 + read attempt 1 + tool 2 |
| R3 只重判 | 上次审核未完成，或证据／审核契约已变且需重判 | review 1；确需重写时显式另申请 sub_model 1 |

admission 按任务保留 `sub_model 1 + review 1` 的修订下限；类型确定后原子申领增量，剩余部分不可被其他并发任务抢走。预留不等于已经消耗，只有实际调用结算；失败或结束释放未用部分，任何 retry 也需显式余量。R2 若必须额外模型研究决策，执行前申领所需 sub_model，不能假定一次报告调用覆盖整个工具研究循环。

同时预留实际剩余时长；预计必需请求无法在总截止时间内启动／完成时不启动该修订，记录 `reserve_exhausted` 或 `time_exhausted`。near_budget 不拦已获准预留，但不能越过总时间、取消和实际 API 硬上限。申领失败明确结束，不偷偷退成缺证据的只改写。

**出口：** 同 JSON 不同排版复用；1 mW/1 MW、数字、引用、scope、证据与契约版本变化强制重审；上次检查失败仍重审；冻结样本 d08f4ba6 的未变报告少一次审核（原预期 3→2 需由快照核实）；小米 81637d08 修订达到明确终态或带原因的资源终止；预算预留不被并发占用、未用预留释放、时间不足不启动半次修订。

### S4 · 终稿映射、展示与恢复

1. 按 §4.8 生成内部 DeliveryDraft、语义核对与程序渲染；记录事实池、采用／省略理由、claim 支持边和实际交付 coverage。
2. AnswerReview／finalization 只接受当前有效事实和必要工具证据；去掉失败草稿／审核提示作为事实的输入通路。
3. API/history/SSE/checkpoint 统一以 run.coverage 和 state 输出；验证 S1 已补全的标签、unknown 显示与单一状态来源，不能重新引入按日志 passed 自行推断完整性的代码。刷新、重连与恢复不得改变结论。
4. 对新 checkpoint 的 requirements/gaps/source versions/cache/ledger/state/delivery map 做恢复核对。数据库当前结果优先，无法对齐时明确 interrupted，不猜测旧资格。

**出口：** 终稿事实与分析均有合法支持且语义成立；不以共用引文误杀合法事实；研究已齐但终稿漏答会降低 coverage；无越权／被拒事实泄漏；同一 run 的实时、history、后台结果与页面显示一致；旧协议和 delivery_status 引用只允许存在于明确的历史迁移元数据中。

### S5 · 最终验收

先核对 S0–S4 所有出口与 §6 离线检查，再按冻结的同一原始问题执行一次真实端到端。任何真实失败保留请求／证据／账本定位到所属契约；不通过临时兼容、放宽 schema、关闭检查或切回旧入口获得通过。

**出口：** §6.3 五条全部满足；真实场景未触发的取消／重试／复用分支必须已有离线证明，不能用“未发生”冒充覆盖。

---

## 6. 验收方法

### 6.1 分层与确定性边界

| 层 | 输入与范围 | 模型／检索 |
|---|---|---|
| L1 回放 | 冻结请求、原始响应、审核响应快照、人工预期；检查路由、事实过滤、coverage、状态映射 | 不调用真实服务 |
| L2 边界 | 内存／隔离数据库和可控执行器；迁移、并发、取消、超时、重启、预算与 SQL/API 资格 | 不调用真实服务 |
| L3 稳定性 | 同一冻结输入重复 3 次，比较规范化后的业务状态与关联图 | 不调用真实服务 |
| L4 端到端 | 同一问题完整实跑；读取独立发送记录、账本、交付映射 | 真实模型与检索 |

L1 的冻结审核响应只能证明代码正确消费判定，不能证明新审核模型语义可靠；后者由人工预期对照和 L4 核对。历史没有逐事实结论的条目须标“新增人工预期”，不能声称从旧事件还原。

L3 比较：Fact 的 `subject/aspect/value/scope/evidence/as_of/time_evidence`、Analysis 的 `statement/fact_ids`、事实与需求支持边、coverage、两轴、summary、采用／省略理由、预算账本的业务字段。使用现有真实 schema，不以不存在的 `analysis.text` 或平铺 quote 字段验收。

UUID 归一为稳定的任务／attempt／local_fact_id 引用；随机运行标识、时间、耗时、token、文件路径与 raw_sha256 排除。**关联关系不能排除**：分析必须仍指向相同事实，coverage 与 claim 同理。并发事件按因果关系和业务投影比较，不要求自然调度的全局 event_seq 相同；确实要求顺序的场景使用固定调度器。

### 6.2 必须覆盖的离线场景

下表是后续实施的验收要求，不授权创建或修改测试文件。可在获准的回放脚本／一次性内存检查中执行；不把现有 tests 作为绕过或扩写目标。

| 组 | 必须覆盖 |
|---|---|
| 状态／SQL | 六种有通过事实的组合、none_passed×三种覆盖、空事实；judged=0/1 合法；judged=2、半空轴、错标签、无摘要发布、unknown 发布均拒绝 |
| 前置态 | not_checked、tool_failed、dependency_unavailable、timed_out、check_incomplete、budget_exhausted 不可交付、不复用审核 |
| 需求／覆盖 | each 多对象展开、main 尚未判定、缺键／额外键／空矩阵、可选项不升格、query_version 变化、替代事实仍支持、删除最后支持后重开 |
| gap | open/searching/covered/exhausted/blocked；三种 dropped 原因；成功清技术失败连续数；无进展不能宣称不存在 |
| 缓存 | served/empty/failed/in_flight；并发相同请求、不同深度／读取 query、首次与重复消费、越权 gap/source 拒绝、等待者取消不终止共享执行 |
| 预算／传输 | 搜索与读取分别覆盖回合超限、总额超限、预发送失败／取消、已发送超时／取消、429 重试、双重回调幂等、无重复 SDK 重试 |
| 审核／修订 | 未变报告复用；单位大小写、数字、scope、证据与规则变化不复用；reserve_exhausted/time_exhausted；预留隔离和未用释放 |
| 交付 | 被拒／通过共用引文、同金额不同产品／口径、重述错误、冲突与过时、重复省略、必需内容漏答、分析悬空引用 |
| 持久化／UI | 版本0–5迁移与6重开、迁移失败回滚、索引/FK保留、最新失败遮蔽旧成功、history/SSE/刷新一致、新 checkpoint 恢复、旧 checkpoint 显式中断 |

覆盖以每条契约有正反例及执行记录为准，不用“总共 ≥20 个”代替边界完整性。

### 6.3 L4 五条硬条件

1. **合法动作契约成立**：真实 run 的 invalid_argument 为 0；本地策略拒绝单独计数，不能与 schema 错误混淆，也不能把零错误解释成可放宽参数。
2. **外发与预算逐 attempt 对齐**：search/read 分别满足 `outbound_attempts == spent_attempts`，每条发送都有唯一预留与结算；本地拒绝、缓存、等待均零外发零扣额。不是与旧 call_id 集合大小比较，也不声称等于供应商账单。
3. **事实可用且必需内容交付**：每条语义通过事实都在审核台账中保留采用／省略原因；当前有效通过事实全部在可追溯事实池中；终稿支持 ID 是其子集；未采用有原因；每个 rid 根据实际终稿有 covered/partial/missing/unknown，只有完整注册表全部 covered 才是 complete。
4. **零无依据断言**：终稿 claim 与分析的支持 ID、来源、版本、scope 和语义均成立；无被拒事实或无支持断言被采纳。共用引文与重复金额不构成自动违规，不能仅凭绑定合法 ID 自动放行。
5. **无变化审核正确复用**：如该 run 产生 eligible 的 revision_ineffective，关联的新增审核调用为 0；未触发则记“不适用”，由 L1/L2 已触发场景证明，不能为达指标人为制造空修订。

观察指标：有效事实数、缺口分布、采集／汇总／审核耗时、审核缓存命中、每种 attempt／重试／失败数、预留等待与释放。原快照 6 次审核、希望降至 ≤5 仅作同流程比较目标；S1/S4 新审核步骤可能改变基数，不能为次数目标跳过必要审核。

---

## 7. 修改与交付约束

- 本次只优化本文件；后续实施按各阶段出口执行，不自动创建分支、暂存、提交或执行其他 Git 命令。
- 不保留旧研究入口、回滚开关、旧判定资格或双重 coverage 写源；原始工具名称与历史审核元数据仅供证据读取／审计。
- 项目文本为 UTF-8，保留已有 BOM 设置，写入 CRLF 并检查真实字节。不得以当前 Windows 默认设置代替校验。
- 只允许 git status 的项目约定优先于通用 git diff --check；使用文件字节、尾随空白及内存差异检查完成文档校验，不执行被禁止的 Git 命令。
- 只读核对现有测试可辅助理解；不新增或修改测试。任何回放脚本也只验证本方案相关契约，不建立伪装在 output 下的另一套测试工程。

## 8. 剩余风险与明确限制

1. 公开资料可能没有所需到手价；exhausted、blocked、unknown 都不能写成“网上不存在”。
2. anchor 和结构完整性不能完全证明需求提取语义无遗漏；主助手需对照原问题核对，无法确定则保留 unknown。
3. 逐事实与终稿语义审核仍有模型误判风险；F0 引文存在、合法 fact_id、非空字段均不能替代语义核实。
4. 历史结果失去自动交付资格是有意的信任边界变更；历史内容与旧审核完整保留，实际回填先对副本验收，禁止伪造逐事实结论。
5. 进程崩溃后的外部执行结果可能不确定；保守保留已发消耗。发送账本衡量应用侧尝试，不是服务端接收或收费证明。
6. 协调与汇总自身耗时、checkpoint 体积治理不在本轮修复范围；只记录真实指标，不承诺固定总耗时。

## 附录 A · 本版修订对应表

| 原问题 | v7 落点 |
|---|---|
| 只给 results 加字段，运行时对象无法调用新谓词 | §4.4.1 状态载体与同源快照；S1 一次切换 |
| SQL 允许半空轴和非法 judged | §4.4.3 显式枚举与完整布尔约束 |
| 草稿套用查询 normalize，单位变化被当成未变 | S3 保留事实值的结构化指纹 |
| 重试复用 call_id 额度，外发与扣额不等 | §4.6 三本账与逐 attempt 预留；§4.7 独立发送记录 |
| Gap 使用早于 Requirement／逐事实审核 | S1 先建设，S2 才消费 |
| coverage 漏格、空集 complete、删一事实就降级 | §4.1 完整 key 集；§4.4.4 支持边重算 |
| 全部事实强入终稿、共用引文误杀、缺 fact_id 映射 | §4.8 DeliveryDraft；§6.3 事实池与实际交付核对 |
| 并发重复外发、key 漏 search_depth、来源没交给消费任务 | §4.2 in_flight、实际请求指纹与本地证据登记 |
| 只扫 Python，前端和恢复继续算旧状态 | §4.5 扩围；S4 API／UI／checkpoint 闭环 |
| 双轨与历史资格兼容违背项目约定 | 删除切换开关和历史资格分支；保留审计原文 |
| 修订历史挤占执行正文、多个阶段出口互相矛盾 | 正文只保留最终契约与唯一出口，旧轮次讨论不再作为实施指令 |
