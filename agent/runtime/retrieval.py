"""Ground follow-up searches in this child's existing tool evidence."""

import json
import re
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agent.runtime.policies import SearchPrerequisiteRequired

Explanation = Annotated[str, StringConstraints(strip_whitespace=True, min_length=4, max_length=1000)]


class SourceAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_id: str = Field(min_length=1, max_length=200)
    relevant: bool
    reason: Explanation


class SearchFollowUp(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    known_information: Explanation = Field(description="上次结果及必要读取已经确认的信息，不能填未核实猜测")
    missing_information: Explanation = Field(description="原始任务仍缺少的具体必要事实，不能仅写资料不足")
    insufficiency: Explanation = Field(description="已有片段及读取为什么不能解决这个缺口")
    search_target: Explanation = Field(description="下一查询将如何针对该缺口取证")
    sources: list[SourceAssessment] = Field(
        max_length=20, description="评估上次搜索的关键来源；relevant 指与本次缺口相关。若全部无关，须逐项说明全部来源"
    )


def payload(item):
    if item.get("status") != "completed":
        return {}
    content = item.get("content") or {}
    return json.loads(content) if isinstance(content, str) else content


def source_key(url):
    from agent.reflection import source_id

    return source_id(url)


def usable_page(row):
    """A successful HTTP/extract request can still contain an error or login page."""
    title = str(row.get("title") or "").strip()
    blocked = re.match(
        r"(?i)^(?:404\b|page not found\b|not found\b|access denied\b|just a moment\b|"
        r"verify you are human\b|sign in\b|log in\b|页面不存在|页面未找到|访问验证|安全验证|登录[ -])",
        title,
    )
    text = str(row.get("raw_content") or row.get("content") or "").strip()
    return bool(text) and not blocked


@dataclass(frozen=True)
class SearchHistory:
    evidence: list[dict]

    @property
    def searches(self):
        return [item for item in self.evidence if item["name"] == "internet_search"]

    @property
    def sources(self):
        return {
            source_key(row["url"]): row
            for item in self.evidence
            if item["name"] in ("internet_search", "read_web_page")
            and item.get("executed")
            and item.get("status") == "completed"
            for row in payload(item).get("results", [])
            if row.get("url")
        }

    def resolve_url(self, value):
        key = value.removeprefix("[source:").removesuffix("]")
        source = self.sources.get(key)
        return source["url"] if source else value

    def page_attempted(self, url):
        return any(
            item["name"] == "read_web_page"
            and item.get("executed")
            and item.get("request_parameters", {}).get("url") == url
            for item in self.evidence
        )

    def latest_search(self, reserved):
        return next((item for item in reversed(self.searches) if item["call_id"] in reserved), None)

    def previous_sources(self, reserved):
        return {
            source_key(row["url"]): row
            for row in payload(self.latest_search(reserved) or {}).get("results", [])
            if row.get("url")
        }

    def guidance(self, reserved):
        return {
            "follow_up_required": bool(reserved),
            "follow_up_fields": list(SearchFollowUp.model_fields),
            "previous_sources": [
                {
                    "source_id": key,
                    "title": row.get("title", "")[:160],
                    "read_attempted": self.page_attempted(row["url"]),
                }
                for key, row in self.previous_sources(reserved).items()
            ],
        }

    def validate_follow_up(self, call, reserved):
        completed = {item["call_id"] for item in self.searches}
        if reserved - completed:
            raise SearchPrerequisiteRequired("当前子任务的上次搜索尚未返回；先等待结果，再决定是否需要下一次搜索。")
        if not reserved:
            if call.args.get("follow_up") is not None:
                raise SearchPrerequisiteRequired("首次搜索没有上次结果，不应提交 follow_up。")
            return
        explanation = call.args.get("follow_up")
        if explanation is None:
            raise SearchPrerequisiteRequired(
                "追加搜索必须提交 follow_up：known_information、missing_information、"
                "insufficiency、search_target 和 sources；先读取相关页面，再针对必要缺口检索。"
            )
        follow_up = SearchFollowUp.model_validate(explanation)
        previous = self.latest_search(reserved)

        def signature(args):
            return (
                " ".join(str(args.get("query") or "").casefold().split()),
                args.get("topic", "general"),
                tuple(sorted(domain.casefold() for domain in args.get("include_domains") or [])),
            )

        if signature(previous.get("request_parameters", payload(previous))) == signature(call.args):
            raise SearchPrerequisiteRequired("查询与上次相同；复用已有结果，只针对明确缺口调整查询。")
        sources = self.previous_sources(reserved)
        assessed = {item.source_id: item for item in follow_up.sources}
        if (sources and not assessed) or set(assessed) - set(sources) or len(assessed) != len(follow_up.sources):
            raise SearchPrerequisiteRequired(
                "sources 只能评估上次搜索的真实 source_id，不得为空、重复或借用其他任务来源。"
                + json.dumps(
                    {
                        "available_source_ids": sorted(sources),
                        "unknown_source_ids": sorted(set(assessed) - set(sources)),
                    },
                    ensure_ascii=False,
                )
            )
        relevant = [sources[key] for key, assessment in assessed.items() if assessment.relevant]
        if not relevant and set(assessed) != set(sources):
            raise SearchPrerequisiteRequired("声称上次结果全部无关时，须说明每个来源；否则先选择关键相关页面读取。")
        if relevant and not any(self.page_attempted(source["url"]) for source in relevant):
            raise SearchPrerequisiteRequired(
                "相关来源尚未读取；先选择能覆盖关键缺口的页面，使用 read_web_page(url=source_id, query=具体缺口)。"
                "不要求逐页读取；已有页面的读取可与另一独立缺口的搜索并行，尚未返回的读取不能作为补查依据。"
            )
        call.args["follow_up"] = follow_up.model_dump()
