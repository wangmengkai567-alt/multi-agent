"""Ground search reports in local evidence and resolve explicitly dated facts."""

import re
from collections import defaultdict
from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from agent.runtime.retrieval import payload, source_key, usable_page

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]


class ReportError(ValueError):
    """A report failed structural or source checks; the draft needs correction."""


class ReportItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EvidenceReference(ReportItem):
    source_id: Text
    quote: Text


class Fact(ReportItem):
    id: Text
    subject: Text
    aspect: Text
    value: Text
    scope: Text
    evidence: list[EvidenceReference] = Field(min_length=1, max_length=3)
    as_of: date | None = None
    time_evidence: EvidenceReference | None = None


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
            for ref in [*fact.evidence, *([fact.time_evidence] if fact.time_evidence else [])]
        ]


REPORT_PROMPT = """
最后只返回 JSON 对象，facts、analysis、gaps 三个字段都必须存在且都是数组，不能用字符串、对象或 null 代替。无内容时填 []。
顶层形状：{"facts":[],"analysis":[],"gaps":[]}。不使用 Markdown 围栏或额外报告。
只提取回答原始需求必不可少的事实；单点核实通常只需 1 至 3 项，不为填写结构补充背景、历史或例子。不需分析时 analysis=[]，没有必要缺口时 gaps=[]。
facts 每项严格包含 id、subject、aspect、value、scope、evidence、as_of、time_evidence。
id 在当前报告内唯一。subject 为精确对象，aspect 为统一事实维度，value 为有依据的值或简明事实，scope 保留地区、版本、条件及口径；同一维度使用相同标签。
相同事实的等价改写合并为一项并列出多个 evidence；value 统一数字格式和同一单位的表达，不把语言或排版差别当成值冲突，不自行换算汇率。
evidence 为 1 至 3 项 {source_id, quote}；source_id 使用真实工具结果的 ID，quote 逐字摘录该来源正文或搜索片段中支持此事实的短句，最多600字符。只取连续原文，不改写、拼接或自行加入省略号。
as_of 只填写能被证实的信息生效日期 YYYY-MM-DD；不得用检索时间、文章日期或产品发布日期推定当前价格/参数的更新时间。
有 as_of 时 time_evidence 必须是 {source_id, quote}，逐字摘录说明该事实生效日期的原文；无法确定时两项均为 null。
来源冲突时保留各自事实和证据；相同 subject、aspect、scope 下可靠日期更晚的事实优先，未注明日期或日期相同而值不同时明确冲突，不猜测哪条更新。
analysis 每项为 {statement, fact_ids}，只依据本报告可采纳事实推导；不得引入新价格、参数、日期，不引用已过时或未解决冲突的事实作确定结论。
完整字段示意（占位符必须替换为真实依据，不可照抄）：{"facts":[{"id":"f1","subject":"对象","aspect":"维度","value":"已核实事实","scope":"版本与条件","evidence":[{"source_id":"实际ID","quote":"逐字原文"}],"as_of":null,"time_evidence":null}],"analysis":[{"statement":"有依据的推导","fact_ids":["f1"]}],"gaps":[]}。
不需要生效日期的版本文档核实，两项时间字段均为 null；不能给 time_evidence 填无关引文。直接回答事实即可时 analysis=[]。
gaps 只说明原始需求尚未核实的必要信息或冲突；事实为空时必须说明缺口，不能声称网上不存在。
引用摘录与报告总长遵守系统提供的上限，选最少且足以支持结论的原文。
"""


def report_response_format():
    schema = SearchReport.model_json_schema()
    for node in [schema, *schema.get("$defs", {}).values()]:
        if "properties" in node:
            node["required"] = list(node["properties"])
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


def local_sources(evidence):
    sources = defaultdict(list)
    for item in evidence:
        if item["name"] not in ("internet_search", "read_web_page") or not item.get("executed"):
            continue
        data = payload(item)
        if item["name"] == "read_web_page" and data.get("read_status") == "unavailable":
            continue
        for row in data.get("results", []):
            if item["name"] == "read_web_page" and not usable_page(row):
                continue
            if row.get("url"):
                sources[source_key(row["url"])].append(row)
    return sources


def parse_report(text, evidence, quote_chars):
    if len(text) > quote_chars * 2:
        raise ReportError("事实报告过长，请只保留原始需求的必要事实和短摘录")
    try:
        report = SearchReport.model_validate_json(text)
    except ValidationError as exc:
        fields = ", ".join(
            f"{'.'.join(map(str, error['loc'])) or 'report'}: {error['msg']}"
            for error in exc.errors(include_input=False)[:5]
        )
        raise ReportError("事实报告格式不符合约定：" + fields) from exc
    ids = [fact.id for fact in report.facts]
    if len(ids) != len(set(ids)):
        raise ReportError("事实 id 必须在当前报告中唯一")
    if not report.facts and not report.gaps:
        raise ReportError("没有已核实事实时必须说明具体缺口")
    sources = local_sources(evidence)
    for index, fact in enumerate(report.facts):
        references = [(f"facts.{index}.evidence.{number}", ref) for number, ref in enumerate(fact.evidence)]
        if fact.time_evidence:
            references.append((f"facts.{index}.time_evidence", fact.time_evidence))
        for path, ref in references:
            rows = sources.get(ref.source_id, [])
            if not any(ref.quote in str(row.get("raw_content") or row.get("content") or "") for row in rows):
                raise ReportError(
                    f"引用摘录不属于当前任务的真实来源：{ref.source_id}；字段 {path}，只能逐字复制连续原文，不能改写或加入省略号"
                )
    quotes = {(ref.source_id, ref.quote) for ref in report.references()}
    if sum(len(quote) + 16 for _, quote in quotes) > quote_chars:
        raise ReportError("引用摘录过长，请仅保留支持必要事实的短句")
    for fact in report.facts:
        if bool(fact.as_of) != bool(fact.time_evidence):
            raise ReportError("事实生效日期与时间依据必须同时提供：" + fact.id)
        if fact.as_of and not dated_quote(fact.time_evidence.quote, fact.as_of):
            raise ReportError("时间依据没有包含所声明的生效日期：" + fact.id)
    resolved = resolve_report(report)
    selected = {fact["id"] for fact in resolved["facts"]}
    if any(not set(item.fact_ids).issubset(selected) for item in report.analysis):
        raise ReportError("分析引用了未知、过时或存在未解决冲突的事实；修正推导或披露缺口")
    resolved["sources"] = [
        {"source_id": key, "url": sources[key][0]["url"]}
        for key in dict.fromkeys(ref.source_id for ref in report.references())
    ]
    return resolved


def resolve_report(report):
    data = report.model_dump(mode="json")
    return {**resolve_facts(data["facts"]), "analysis": data["analysis"], "gaps": data["gaps"]}


def resolve_facts(facts):
    groups = defaultdict(list)
    for fact in facts:
        groups[(fact["subject"].casefold(), fact["aspect"].casefold(), fact["scope"].casefold())].append(fact)
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
            if fact["time_evidence"]:
                references.append(fact["time_evidence"])
    return references


def approved_reports(context):
    tasks = [
        (key, task.report)
        for key, task in context.search_tasks.items()
        if task.check_status == "passed" and task.report is not None
    ]
    if not tasks:
        return []
    facts, analyses, gaps, sources, origins = [], [], [], {}, []
    for index, (task_id, report) in enumerate(tasks):
        local = [
            *report["facts"],
            *report["superseded"],
            *(fact for conflict in report["conflicts"] for fact in conflict["facts"]),
        ]
        ids = {fact["id"]: f"{index}:{fact['id']}" for fact in local}
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
    lines = []
    for fact in report["facts"]:
        sources = " ".join(f"[来源]({urls[ref['source_id']]})" for ref in fact["evidence"])
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
