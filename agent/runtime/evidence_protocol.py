"""Explicit tool evidence adapters and runtime-owned, versioned source records."""

import copy
import json
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_VERSION = 1
METADATA_KEY = "evidence_adapter"


class EvidenceError(ValueError):
    """An executed result did not satisfy its declared evidence contract."""


class ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DocumentInput(ArtifactModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser: str = Field(min_length=1)
    selection: str = Field(min_length=1)


class DocumentSection(ArtifactModel):
    key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    locator: dict[str, str | int]
    content: str


class CalculationSection(DocumentSection):
    method: str = Field(min_length=1)
    parameters: dict


class DocumentCoverage(ArtifactModel):
    complete: bool
    scope: Literal["catalog", "selection", "supported_content"]
    missing: list[str]
    limitations: list[str]
    catalog: list[dict]
    extents: dict[str, list[int]]
    read_ranges: dict[str, list[int]]

    @model_validator(mode="after")
    def coherent_ranges(self):
        if set(self.read_ranges) - set(self.extents):
            raise ValueError("Document reads must belong to declared coverage extents")
        for bounds in [*self.extents.values(), *self.read_ranges.values()]:
            if len(bounds) != 4 or bounds[0] < 1 or bounds[2] < 1 or bounds[1] < 0 or bounds[3] < 0:
                raise ValueError("Document coverage requires four valid row/column bounds")
        if self.scope == "catalog" and self.read_ranges:
            raise ValueError("A catalog cannot contribute content coverage")
        return self


class DocumentArtifact(ArtifactModel):
    document: DocumentInput
    sections: list[DocumentSection]
    calculations: list[CalculationSection]
    coverage: DocumentCoverage


class CalculationArtifact(ArtifactModel):
    inputs: list[DocumentInput] = Field(min_length=1)
    calculations: list[CalculationSection] = Field(min_length=1)


class EvidenceSource(ArtifactModel):
    key: str = Field(min_length=1)
    kind: Literal["web", "sql", "document", "calculation", "record"]
    title: str
    url: str
    content: str
    locator: dict[str, str | int]
    provenance: dict
    inputs: list[DocumentInput]
    retrieval: Literal["search", "page", "local"]
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    origin_source_id: str = Field(min_length=1)


@dataclass(frozen=True)
class EvidenceAdapter:
    adapter_id: str
    kind: str
    normalize: object
    network: bool = False
    capture: bool = True


def fingerprint(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _source(key, kind, title, content, *, url="", locator=None, provenance=None, inputs=(), retrieval="local"):
    from agent.results import clean

    if not isinstance(content, str):
        raise EvidenceError("Evidence text must be a string")
    body = clean(content)
    return EvidenceSource(
        key=key,
        kind=kind,
        title=title,
        url=url,
        content=body,
        locator={} if locator is None else locator,
        provenance={} if provenance is None else clean(provenance),
        inputs=list(inputs),
        retrieval=retrieval,
        digest=sha256(body.encode()).hexdigest(),
        origin_source_id=key,
    ).model_dump(mode="json")


def _control(content, artifact, call):
    return [], "Tool is explicitly declared as workflow/instruction control, not factual evidence"


def _text(kind):
    def normalize(content, artifact, call):
        if not isinstance(content, str):
            raise EvidenceError("Text evidence tool must return textual content")
        if artifact is not None:
            raise EvidenceError("Text evidence adapter does not accept an undeclared artifact")
        if not content.strip():
            return [], "Tool returned no factual text"
        return [
            _source(
                call.call_id,
                kind,
                call.actor,
                content,
                provenance={"tool": call.actor, "parameters": call.args},
            )
        ], ""

    return normalize


def web_results(artifact):
    if not isinstance(artifact, dict) or not isinstance(artifact.get("results"), list):
        raise EvidenceError("Web evidence requires the provider result artifact")
    return artifact["results"]


def web_result_error(row, retrieval):
    """Reject malformed provider rows independently; never coerce them into facts."""
    from agent.runtime.retrieval import usable_page

    if not isinstance(row, dict):
        return "Web result is not an object"
    url = row.get("url")
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        valid = (
            parsed
            and parsed.scheme in ("https", "http")
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 80, 443)
            and not any(char.isspace() for char in url)
        )
    except ValueError:
        valid = False
    if not valid:
        return "Web result has an invalid source URL"
    body = row.get("raw_content") or row.get("content") or ""
    if not isinstance(body, str):
        return "Web source body must be text"
    if not body.strip() or retrieval == "page" and not usable_page(row):
        return "Web result has no readable factual text"
    return None


def _web(retrieval):
    def normalize(content, artifact, call):
        from agent.runtime.retrieval import source_key

        results = web_results(artifact)
        rows = []
        exclusions = [f"result[{row['result_index']}]: {row['error']}" for row in artifact.get("invalid_results", [])]
        if retrieval == "page" and artifact.get("read_status") == "unavailable":
            return [], "Provider did not return readable page content"
        for index, row in enumerate(results):
            error = web_result_error(row, retrieval)
            if error:
                exclusions.append(f"result[{index}]: {error}")
                continue
            url = row["url"]
            body = row.get("raw_content") or row.get("content")
            rows.append(
                _source(
                    source_key(url) + ":" + str(index),
                    "web",
                    str(row.get("title") or ""),
                    body,
                    url=url,
                    provenance={"tool": call.actor, "retrieval": retrieval, "result_index": index},
                    retrieval=retrieval,
                )
            )
        note = "; ".join(exclusions)
        if not rows:
            note = "Provider returned no usable web source" + ("; " + note if note else "")
        return rows, note

    return normalize


def validate_input(value, context):
    from utils.secure_paths import confined_path

    if context is None:
        raise EvidenceError("Document evidence requires a run context")
    path = confined_path(value.path, context.work_dir)
    relative = path.relative_to(context.work_dir.resolve()).as_posix()
    if relative != value.path or not relative.startswith("uploads/"):
        raise EvidenceError("Only original run uploads may support document/calculation evidence")
    if context.task_contract is not None and not any(
        m.kind == "upload" and m.reference == relative and m.digest == value.sha256
        for m in context.task_contract.materials
    ):
        raise EvidenceError("Input is outside the current task's bound materials")
    if (
        not path.is_file()
        or path.stat().st_size > context.policy.files.max_upload_bytes
        or sha256(path.read_bytes()).hexdigest() != value.sha256
    ):
        raise EvidenceError("Input document no longer matches its execution snapshot")
    return value


def _calculation_rows(calculations, inputs):
    rows = []
    for item in calculations:
        if not item.content.strip():
            continue
        rows.append(
            _source(
                item.key,
                "calculation",
                item.title,
                item.content,
                locator=item.locator,
                inputs=inputs,
                provenance={"method": item.method, "parameters": item.parameters},
            )
        )
    return rows


def _document(content, artifact, call):
    from agent.runtime.context import get_run_context

    data = DocumentArtifact.model_validate(artifact)
    source = validate_input(data.document, get_run_context())
    rows = [
        _source(
            item.key,
            "document",
            item.title,
            item.content,
            locator=item.locator,
            provenance=source.model_dump(),
            inputs=[source],
        )
        for item in data.sections
        if item.content.strip()
    ]
    rows.extend(_calculation_rows(data.calculations, [source]))
    if data.coverage.complete and (data.coverage.scope != "supported_content" or data.coverage.missing):
        raise EvidenceError("Document extraction completeness contradicts its declared selection")
    note = json.dumps(data.coverage.model_dump(), ensure_ascii=False)
    return rows, note


def _calculation(content, artifact, call):
    from agent.runtime.context import get_run_context

    data = CalculationArtifact.model_validate(artifact)
    inputs = [validate_input(value, get_run_context()) for value in data.inputs]
    rows = _calculation_rows(data.calculations, inputs)
    return rows, "" if rows else "Calculation returned no usable textual result"


ADAPTERS = MappingProxyType(
    {
        "control.v1": EvidenceAdapter("control.v1", "control", _control),
        "research.v1": EvidenceAdapter("research.v1", "control", _control, network=True, capture=False),
        "network-control.v1": EvidenceAdapter("network-control.v1", "control", _control, network=True),
        "sql.v1": EvidenceAdapter("sql.v1", "sql", _text("sql")),
        "record.v1": EvidenceAdapter("record.v1", "record", _text("record")),
        "web-search.v1": EvidenceAdapter("web-search.v1", "web", _web("search"), network=True),
        "web-page.v1": EvidenceAdapter("web-page.v1", "web", _web("page"), network=True),
        "document.v1": EvidenceAdapter("document.v1", "document", _document),
        "calculation.v1": EvidenceAdapter("calculation.v1", "calculation", _calculation),
    }
)


def adapter_by_id(identity):
    if not isinstance(identity, str) or identity not in ADAPTERS:
        raise EvidenceError("A registered, explicitly versioned evidence adapter is required")
    return ADAPTERS[identity]


def adapter_for_tool(tool):
    if not isinstance(tool, BaseTool):
        raise EvidenceError("Evidence binding requires a registered BaseTool")
    return adapter_by_id((tool.metadata or {}).get(METADATA_KEY))


def evidence_tool(adapter_id):
    adapter_by_id(adapter_id)

    def bind(tool):
        if not isinstance(tool, BaseTool):
            raise EvidenceError("Apply evidence_tool outside the public tool decorator")
        if METADATA_KEY in (tool.metadata or {}):
            raise EvidenceError("Tool evidence adapter was already declared")
        return tool.model_copy(update={"metadata": {**(tool.metadata or {}), METADATA_KEY: adapter_id}})

    return bind


FRAMEWORK_ADAPTERS = MappingProxyType(
    {
        "ls": "control.v1",
        "read_file": "control.v1",
        "write_file": "control.v1",
        "edit_file": "control.v1",
        "glob": "control.v1",
        "grep": "control.v1",
        "execute": "control.v1",
        "task": "control.v1",
        "write_todos": "control.v1",
    }
)


def bind_framework_tools(middleware):
    tools = []
    for tool in middleware.tools:
        if tool.name not in FRAMEWORK_ADAPTERS:
            raise EvidenceError("Framework added a tool without a reviewed evidence binding")
        tools.append(evidence_tool(FRAMEWORK_ADAPTERS[tool.name])(tool))
    middleware.tools = tools
    return middleware


def make_record(call, content, artifact=None, *, cache=None, parameters=None):
    """Identity comes from the runtime; adapters may only normalize actual payloads."""
    from agent.results import clean
    from agent.runtime.context import get_run_context

    context = get_run_context()
    if context is None:
        raise EvidenceError("Evidence collection requires a run")
    owner = call.search_task_key
    adapter = adapter_by_id(call.evidence_adapter) if call.evidence_adapter is not None else None
    if adapter is None and (call.executed or call.status == "completed"):
        raise EvidenceError("Executed evidence has no declared adapter")
    if adapter is not None and adapter.kind != "control" and not owner:
        raise EvidenceError("Factual evidence requires an owned delegated task")
    record = clean(
        dict(
            protocol_version=PROTOCOL_VERSION,
            adapter=adapter.adapter_id if adapter else None,
            scope_id=context.research_scope_id,
            run_id=context.run_id,
            thread_id=context.thread_id,
            task_id=owner,
            name=call.actor.rsplit(":", 1)[-1],
            call_id=call.call_id,
            attempt=call.attempt,
            executed=call.executed,
            status=call.status,
            content=content,
            artifact=artifact,
            request_parameters=dict(call.args) if parameters is None else parameters,
            query_reference={key: value for key, value in call.args.items() if key in ("table_name", "query")}
            if adapter is not None and adapter.kind == "sql"
            else {},
            cache_hit=cache is not None,
            origin_call_id=cache["call_id"] if cache else None,
            origin_attempt=cache["attempt"] if cache else None,
            sources=[],
            evidence_note="",
        )
    )
    if call.status == "completed" and (call.executed or cache is not None):
        context.check_active()
        sources, note = adapter.normalize(record["content"], record["artifact"], call)
        keys = [source["key"] for source in sources]
        if len(keys) != len(set(keys)):
            raise EvidenceError("Adapter returned duplicate source keys")
        record.update(sources=sources, evidence_note=note)
    if call.status == "completed":
        context.check_active()
    record["integrity"] = fingerprint(record)
    return record


def validate_source(value, adapter):
    source = EvidenceSource.model_validate(value)
    permitted = (
        {"document", "calculation"} if adapter and adapter.kind == "document" else {adapter.kind} if adapter else set()
    )
    if source.kind not in permitted:
        raise EvidenceError("Source kind contradicts the declared adapter")
    if source.kind == "web":
        expected = "search" if adapter.adapter_id == "web-search.v1" else "page"
        if source.retrieval != expected or web_result_error(source.model_dump(), source.retrieval):
            raise EvidenceError("Invalid normalized web source")
    elif source.url or source.retrieval != "local":
        raise EvidenceError("Source location contradicts its evidence kind")
    if source.kind in ("document", "calculation") and not source.inputs:
        raise EvidenceError("Document/calculation evidence must retain input provenance")
    if sha256(source.content.encode()).hexdigest() != source.digest:
        raise EvidenceError("Normalized source text changed")
    if source.key != source.origin_source_id:
        raise EvidenceError("Normalized source identity changed")
    return source


def validate_record(record, context):
    if record.get("protocol_version") != PROTOCOL_VERSION:
        raise EvidenceError("Unsupported tool evidence protocol")

    if record["adapter"] is None:
        if record["executed"] or record["status"] == "completed" or record["sources"]:
            raise EvidenceError("An unbound rejected call cannot provide evidence")
        adapter = None
    else:
        adapter = adapter_by_id(record["adapter"])
    if context is None or record["scope_id"] != context.research_scope_id:
        raise EvidenceError("Evidence belongs to another logical run")
    if record["thread_id"] != context.thread_id:
        raise EvidenceError("Evidence belongs to another thread")
    if record["integrity"] != fingerprint(
        {
            key: value
            for key, value in record.items()
            if key
            not in (
                "integrity",
                "via",
                "gap_id",
                "rid",
                "source_assessments",
                "error",
                "error_category",
                "model_intent",
                "delegated_from",
            )
        }
    ):
        raise EvidenceError("Tool evidence record changed after collection")
    if record["sources"] and (not record["task_id"] or record["status"] != "completed"):
        raise EvidenceError("Only a completed owned result can provide sources")
    keys = []
    for value in record["sources"]:
        source = validate_source(value, adapter)
        keys.append(source.key)
    if len(keys) != len(set(keys)):
        raise EvidenceError("Duplicate source keys in an evidence record")
    return record


def source_rows(evidence):
    from agent.runtime.retrieval import evidence_available

    for item in evidence:
        if evidence_available(item):
            for row in item["sources"]:
                yield item, copy.deepcopy(row), row["content"]


def is_web_evidence(item):
    """Classify an attempted call without claiming that it returned usable evidence."""
    return item["adapter"] is not None and adapter_by_id(item["adapter"]).kind == "web"


def source_identity(row):
    if row["kind"] == "web":
        from agent.runtime.source_policy import page_tier

        return page_tier(row)
    return dict(
        publisher=row["title"],
        channel_type=row["kind"],
        identity_basis="Runtime-owned tool/file evidence",
        applicability=json.dumps(row["locator"], ensure_ascii=False, sort_keys=True),
        tier="unknown",
        identity_status="not_applicable",
        content_version=row["digest"],
    )


def source_citation(source):
    if source["kind"] == "web":
        return "[来源](" + source["url"] + ")"
    label = {"sql": "SQL执行", "document": "上传材料", "calculation": "程序计算", "record": "工具结果"}[source["kind"]]
    detail = source["title"].replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    return "[" + label + "：" + detail + "；" + source["source_id"] + "]"
