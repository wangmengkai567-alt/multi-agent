"""Ground search reports in local evidence and resolve explicitly dated facts."""

import json
import re
from collections import defaultdict
from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]
QuoteId = Annotated[str, StringConstraints(min_length=2, max_length=32, pattern=r"^q[1-9][0-9]*$")]


class ReportError(ValueError):
    """A report failed structural or source checks; the draft needs correction."""


class ReportItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class QuoteReference(ReportItem):
    quote_id: QuoteId


class EvidenceReference(ReportItem):
    quote_id: QuoteId
    source_id: Text
    quote: Text


class FactDimensions(ReportItem):
    model: str | None = None
    variant: str | None = None
    region: str | None = None
    channel: str | None = None
    window: str | None = None
    eligibility: str | None = None
    price_kind: str | None = None


class Fact(ReportItem):
    id: Text
    dimensions: FactDimensions = Field(default_factory=FactDimensions)
    subject: Text
    aspect: Text
    value: Text
    scope: Text
    evidence: list[EvidenceReference] = Field(min_length=1, max_length=3)
    identity_evidence: list[EvidenceReference] = Field(max_length=3)
    as_of: date | None = None
    time_evidence: EvidenceReference | None = None


class FactDraft(Fact):
    evidence: list[QuoteReference] = Field(min_length=1, max_length=3)
    identity_evidence: list[QuoteReference] = Field(max_length=3)
    time_evidence: QuoteReference | None = None


class Analysis(ReportItem):
    statement: Text
    fact_ids: list[Text] = Field(min_length=1, max_length=24)


class SearchReport(ReportItem):
    facts: list[Fact] = Field(max_length=24)
    analysis: list[Analysis] = Field(max_length=12)
    gaps: list[Text] = Field(max_length=12)

    def references(self):
        return [
            ref
            for fact in self.facts
            for ref in [*fact.evidence, *fact.identity_evidence, *([fact.time_evidence] if fact.time_evidence else [])]
        ]


class ReportDraft(SearchReport):
    facts: list[FactDraft] = Field(max_length=24)


REPORT_PROMPT = """
最后只返回 JSON 对象，facts、analysis、gaps 三个字段都必须存在且都是数组，不能用字符串、对象或 null 代替。无内容时填 []。
顶层形状：{"facts":[],"analysis":[],"gaps":[]}。不使用 Markdown 围栏或额外报告。
只提取回答原始需求必不可少的事实；单点核实通常只需 1 至 3 项，不为填写结构补充背景、历史或例子。不需分析时 analysis=[]，没有必要缺口时 gaps=[]。
facts 每项严格包含 id、dimensions、subject、aspect、value、scope、evidence、identity_evidence、as_of、time_evidence。dimensions 含 model、variant、region、channel、window、eligibility、price_kind，不能确认的各项为 null；每项非空条件必须有引文依据，金额口径尤其区分发布价、标价和优惠后到手价。
identity_evidence 使用 {quote_id} 选择服务端登记的连续原文，说明该事实实际对应的产品/对象、版本或系列适用范围；不能确认时填[]。它只用于核实对象身份，不可用来补足value证据。原始名称按来源保留，不能为匹配需求把Pro、系列或不同地区版改成标准版。
id 在当前报告内唯一。subject 为精确对象，aspect 为统一事实维度，value 为有依据的值或简明事实，scope 保留地区、版本、条件及口径；同一维度使用相同标签。
相同事实的等价改写合并为一项并列出多个 evidence；value 统一数字格式和同一单位的表达，不把语言或排版差别当成值冲突，不自行换算汇率。
evidence 为 1 至 3 项 {quote_id}；只选择 citation_options 中支持此事实的真实短 ID。原文由程序展开，禁止返回 source_id、quote 或自行拼接引用。
as_of 只填写能被证实的信息生效日期 YYYY-MM-DD；不得用检索时间、文章日期或产品发布日期推定当前价格/参数的更新时间。
有 as_of 时 time_evidence 必须是 {quote_id}，选择说明该事实生效日期的已登记原文；无法确定时两项均为 null。
来源冲突时保留各自事实和证据；相同 subject、aspect、scope 下可靠日期更晚的事实优先，未注明日期或日期相同而值不同时明确冲突，不猜测哪条更新。
analysis 每项为 {statement, fact_ids}，只依据本报告可采纳事实推导；不得引入新价格、参数、日期，不引用已过时或未解决冲突的事实作确定结论。
完整字段示意（占位符必须替换为真实依据，不可照抄）：{"facts":[{"id":"f1","dimensions":{"model":null,"variant":null,"region":null,"channel":null,"window":null,"eligibility":null,"price_kind":null},"subject":"对象","aspect":"维度","value":"已核实事实","scope":"版本与条件","evidence":[{"quote_id":"实际短ID"}],"identity_evidence":[],"as_of":null,"time_evidence":null}],"analysis":[{"statement":"有依据的推导","fact_ids":["f1"]}],"gaps":[]}。
不需要生效日期的版本文档核实，两项时间字段均为 null；不能给 time_evidence 填无关引文。直接回答事实即可时 analysis=[]。
gaps 只说明原始需求尚未核实的必要信息或冲突；事实为空时必须说明缺口，不能声称网上不存在。
引用摘录与报告总长遵守系统提供的上限，选最少且足以支持结论的原文。
"""


def report_response_format():
    schema = ReportDraft.model_json_schema()

    def strict(node):
        if isinstance(node, dict):
            node.pop("default", None)
            if "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for child in node.values():
                strict(child)
        elif isinstance(node, list):
            for child in node:
                strict(child)

    strict(schema)
    return {"type": "json_schema", "json_schema": {"name": "search_report", "strict": True, "schema": schema}}


def dated_quote(quote, value):
    normalized = re.sub(r"[\s,，]+", "", quote).casefold()
    forms = (
        value.isoformat(),
        value.strftime("%Y/%m/%d"),
        value.strftime("%Y.%m.%d"),
        value.strftime("%Y年%m月%d日"),
        f"{value.year}年{value.month}月{value.day}日",
        value.strftime("%B") + str(value.day) + str(value.year),
        str(value.day) + value.strftime("%B") + str(value.year),
        value.strftime("%b") + str(value.day) + str(value.year),
        str(value.day) + value.strftime("%b") + str(value.year),
    )
    return any(form.casefold() in normalized for form in forms)


def parse_report(text, evidence, quote_chars):
    if len(text) > quote_chars * 2:
        raise ReportError("事实报告过长，请只保留原始需求的必要事实和短摘录")
    try:
        draft = ReportDraft.model_validate_json(text)
    except ValidationError as exc:
        fields = ", ".join(
            f"{'.'.join(map(str, error['loc'])) or 'report'}: {error['msg']}"
            for error in exc.errors(include_input=False)[:5]
        )
        raise ReportError("事实报告格式不符合约定：" + fields) from exc
    from agent.runtime.quotes import QuoteError, resolve_quote, task_owner

    owner = task_owner()
    value = draft.model_dump(mode="json")
    snapshots = {}
    try:
        for fact in value["facts"]:
            for field in ("evidence", "identity_evidence", "time_evidence"):
                refs = fact[field] if isinstance(fact[field], list) else [fact[field]] if fact[field] else []
                expanded = []
                for ref in refs:
                    resolved, row = resolve_quote(ref["quote_id"], evidence, owner=owner)
                    expanded.append(resolved)
                    snapshots[resolved["source_id"]] = row
                fact[field] = expanded if field != "time_evidence" else expanded[0] if expanded else None
        report = SearchReport.model_validate_json(json.dumps(value, ensure_ascii=False))
    except QuoteError as exc:
        raise ReportError(str(exc)) from exc
    ids = [fact.id for fact in report.facts]
    if len(ids) != len(set(ids)):
        raise ReportError("事实 id 必须在当前报告中唯一")
    if not report.facts and not report.gaps:
        raise ReportError("没有已核实事实时必须说明具体缺口")
    sources = {key: [row] for key, row in snapshots.items()}
    fact_errors, identity_errors = {}, {}
    for index, fact in enumerate(report.facts):
        errors = []
        references = [(f"facts.{index}.evidence.{number}", ref) for number, ref in enumerate(fact.evidence)]
        if fact.time_evidence:
            references.append((f"facts.{index}.time_evidence", fact.time_evidence))
        for path, ref in references:
            rows = sources.get(ref.source_id, [])
            if not any(ref.quote in str(row.get("raw_content") or row.get("content") or "") for row in rows):
                errors.append(f"引用不属于本任务真实连续原文：{ref.source_id}，字段 {path}")
        identity_failures = []
        for ref in fact.identity_evidence:
            if not any(
                ref.quote in str(row.get("raw_content") or row.get("content") or "")
                for row in sources.get(ref.source_id, [])
            ):
                identity_failures.append(f"对象身份引文不属于本任务真实原文：{ref.source_id}")
        if identity_failures:
            identity_errors[fact.id] = identity_failures
        if bool(fact.as_of) != bool(fact.time_evidence):
            errors.append("事实生效日期与时间依据必须同时提供")
        elif fact.as_of and not dated_quote(fact.time_evidence.quote, fact.as_of):
            errors.append("时间依据没有包含所声明的生效日期；无法证实时日期和时间依据均应为 null")
        if errors:
            fact_errors[fact.id] = errors
    quotes = {(ref.source_id, ref.quote) for ref in report.references()}
    if sum(len(quote) + 16 for _, quote in quotes) > quote_chars:
        raise ReportError("引用摘录过长，请仅保留支持必要事实的短句")
    from agent.runtime.source_policy import page_tier

    # Date/source failures belong to individual facts. Resolve conflicts only
    # after complete per-fact judgments, so a bad sibling cannot hide a good one.
    return {
        **report.model_dump(mode="json"),
        "fact_errors": fact_errors,
        "identity_errors": identity_errors,
        "sources": [
            {
                "source_id": key,
                "url": sources[key][0]["url"],
                "origin_source_id": sources[key][0]["origin_source_id"],
                **page_tier(sources[key][0]),
            }
            for key in dict.fromkeys(ref.source_id for ref in report.references())
            if key in sources
        ],
    }


def resolve_facts(facts):
    groups = defaultdict(list)
    for fact in facts:
        identity = (
            ("object", fact["object_id"], fact["dimensions"]["variant"])
            if fact.get("object_id")
            else ("unresolved", fact["subject"].casefold())
        )
        groups[(identity, fact["aspect"].casefold(), fact["scope"].casefold())].append(fact)
    selected, superseded, conflicts = [], [], []
    for group in groups.values():
        if len({fact["value"] for fact in group}) == 1:
            selected.extend(group)
            continue
        dated = all(fact["as_of"] is not None for fact in group)
        latest = [fact for fact in group if fact["as_of"] == max(item["as_of"] for item in group)] if dated else []
        if latest and len({fact["value"] for fact in latest}) == 1:
            selected.extend(latest)
            superseded.extend(fact for fact in group if fact not in latest)
            continue
        conflicts.append({"facts": group, "reason": "同一口径下的值冲突，缺少可靠时间顺序或同日仍有不同值"})
    return {"facts": selected, "superseded": superseded, "conflicts": conflicts}


def report_references(reports):
    references = []
    for report in reports:
        facts = [
            *report["facts"],
            *report["superseded"],
            *(fact for conflict in report["conflicts"] for fact in conflict["facts"]),
        ]
        for fact in facts:
            references.extend(fact["evidence"])
            references.extend(fact["identity_evidence"])
            if fact["time_evidence"]:
                references.append(fact["time_evidence"])
    return references


def approved_reports(context):
    from agent.runtime.review_state import deliverable

    tasks = [
        (key, task.report)
        for key, task in context.search_tasks.items()
        if deliverable(task.state) and task.report is not None
    ]
    if not tasks:
        return []
    facts, analyses, gaps, sources, origins = [], [], [], {}, []
    for task_id, report in tasks:
        local = [
            *report["facts"],
            *report["superseded"],
            *(fact for conflict in report["conflicts"] for fact in conflict["facts"]),
        ]
        ids = {fact["id"]: f"{task_id}:{context.search_tasks[task_id].report_attempt}:{fact['id']}" for fact in local}
        facts.extend({**fact, "id": ids[fact["id"]]} for fact in local)
        analyses.extend({**item, "fact_ids": [ids[key] for key in item["fact_ids"]]} for item in report["analysis"])
        gaps.extend(report["gaps"])
        sources.update((source["source_id"], source) for source in report["sources"])
        origins.extend({"fact_id": ids[key], "task_id": task_id, "original_id": key} for key in ids)
    resolved = resolve_facts(facts)
    selected = {fact["id"] for fact in resolved["facts"]}
    return [
        {
            **resolved,
            "analysis": [item for item in analyses if set(item["fact_ids"]).issubset(selected)],
            "gaps": list(dict.fromkeys(gaps)),
            "sources": list(sources.values()),
            "origins": origins,
        }
    ]


def render_report(report):
    urls = {source["source_id"]: source["url"] for source in report["sources"]}
    origins = {source["source_id"]: source["origin_source_id"] for source in report["sources"]}
    lines = []
    for fact in report["facts"]:
        sources = " ".join(
            f"[来源]({urls[ref['source_id']]})" if urls[ref["source_id"]] else f"[SQL执行：{origins[ref['source_id']]}]"
            for ref in fact["evidence"]
        )
        lines.append(
            f"- {fact['subject']} · {fact['aspect']}：{fact['value']}（{fact['scope']}；生效日期：{fact['as_of'] or '未核实'}）{sources}"
        )
    lines.extend("分析：" + item["statement"] for item in report["analysis"])
    lines.extend("未核实：" + gap for gap in report["gaps"])
    for conflict in report["conflicts"]:
        alternatives = "；".join(
            f"{fact['subject']} {fact['aspect']}={fact['value']}（{fact['scope']}，{fact['as_of'] or '时间未核实'}）"
            for fact in conflict["facts"]
        )
        lines.append("来源冲突：" + alternatives + "。" + conflict["reason"])
    return "\n".join(lines)
