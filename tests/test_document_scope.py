"""Requested document ranges stay separate from whole-upload coverage."""

import io
import json
from copy import deepcopy
from hashlib import sha256

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from openpyxl import Workbook
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from test_basic_coordination import finished, models
from test_framework_contract import call

from agent.results import task_evidence
from agent.runtime.context import RunContext, reset_run_context, set_run_context
from agent.runtime.contracts import bind_contract
from agent.runtime.evidence_protocol import EvidenceError, make_record
from agent.runtime.hooks import HookCall
from agent.runtime.policies import load_runtime_policy
from agent.runtime.workflows import after_tool_workflow, finalize_answer
from tools.upload_file_read_tool import _read_local


def workbook_bytes(formula=False):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = "=20+22" if formula else 42
    sheet["A2"] = 99
    workbook.create_sheet("Other")["A1"] = "unrequested content"
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def pdf_bytes(blank_first=False):
    writer = PdfWriter()
    for index in range(2):
        page = writer.add_blank_page(width=300, height=200)
        if index == 0 and blank_first:
            continue
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 20 100 Td (Page {index + 1} value {42 + index}) Tj ET".encode())
        page[NameObject("/Contents")] = stream
    output = io.BytesIO()
    writer.write(output)
    writer.close()
    return output.getvalue()


@pytest.fixture
def document_context(sandbox_dir):
    context = RunContext(
        "document-scope", "thread", sandbox_dir, policy=load_runtime_policy(), user_query="Read selected content"
    )
    (sandbox_dir / "uploads").mkdir()
    (sandbox_dir / "uploads/data.xlsx").write_bytes(workbook_bytes())
    (sandbox_dir / "uploads/pages.pdf").write_bytes(pdf_bytes())
    context.expert_contract = {"document": {"execution": {"source_kind": "other"}}}
    context.task_results["task"] = {}
    token = set_run_context(context)
    try:
        yield context
    finally:
        reset_run_context(token)


def observation(context, filename="data.xlsx", selection=None, identity="read"):
    bind_contract(context)
    file = context.work_dir / "uploads" / filename
    raw, (text, sections, calculations, parser, description, coverage) = _read_local(file, 20_000_000, selection)
    artifact = dict(
        document=dict(path="uploads/" + filename, sha256=sha256(raw).hexdigest(), parser=parser, selection=description),
        sections=sections,
        calculations=calculations,
        coverage=coverage,
    )
    observed = HookCall(
        "document:read_document",
        identity,
        task_id="task",
        evidence_adapter="document.v1",
        executed=True,
        status="completed",
        args=dict(filename="uploads/" + filename, selection=selection),
        result=ToolMessage(content=text, artifact=artifact, tool_call_id=identity),
    )
    record = make_record(observed, text, artifact)
    after_tool_workflow(observed)
    return record


async def deliver(context, records):
    sources, unfinished, limitations = task_evidence(records, "task")
    context.task_results["task"] = dict(
        status="completed",
        target="document",
        display_name="文档助手",
        sources=sources,
        unfinished=unfinished,
        limitations=limitations,
    )
    return await finalize_answer(context, "选定内容为42。")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename,selection,expected_range",
    [
        ("data.xlsx", {"sheet": "Data", "cell_range": "A1:A1"}, {"sheet:1": [1, 1, 1, 1]}),
        ("data.xlsx", {"sheet": "Data"}, {"sheet:1": [1, 2, 1, 1]}),
        ("pages.pdf", {"page_start": 1, "page_end": 1}, {"pages": [1, 1, 1, 1]}),
    ],
)
async def test_selected_content_completes_without_claiming_full_read(
    document_context, filename, selection, expected_range
):
    context = document_context
    record = observation(context, filename, selection)
    coverage = record["artifact"]["coverage"]
    assert coverage["scope"] == "selection" and coverage["complete"] is False
    assert coverage["read_ranges"] == expected_range
    assert coverage["extents"] != expected_range
    assert not coverage["missing"] and record["sources"]
    assert not context.upload_reads
    answer = await deliver(context, [record])
    assert context.coverage == "complete"
    assert context.task_results["task"]["unfinished"] == []
    assert "未读取整个文件" in answer and "部分完成" not in answer
    assert (context.work_dir / "answer.md").read_bytes() == answer.encode()
    assert "unrequested content" not in str(record["sources"])
    if filename.endswith("pdf"):
        assert {source["locator"]["page"] for source in record["sources"]} == {1}
    else:
        assert {source["locator"]["sheet"] for source in record["sources"]} == {"Data"}
        if "cell_range" in selection:
            assert {source["locator"]["row"] for source in record["sources"]} == {1}
            assert "99" not in "".join(source["content"] for source in record["sources"])


@pytest.mark.asyncio
async def test_required_full_upload_still_rejects_selected_subset(document_context):
    context = document_context
    context.required_uploads = ("data.xlsx",)
    record = observation(context, selection={"sheet": "Data", "cell_range": "A1:A1"})
    answer = await deliver(context, [record])
    assert context.coverage == "partial" and not context.upload_reads
    assert "尚未完整读取的必需文件：data.xlsx" in answer
    assert context.task_results["task"]["unfinished"]


@pytest.mark.asyncio
@pytest.mark.parametrize("split", [False, True])
async def test_full_read_and_range_union_satisfy_required_upload(document_context, split):
    context = document_context
    context.required_uploads = ("data.xlsx",)
    if split:
        selections = [
            {"sheet": "Data", "cell_range": "A1:A1"},
            {"sheet": "Data", "cell_range": "A2:A2"},
            {"sheet": "Other"},
        ]
        records = [
            observation(context, selection=selection, identity=f"read-{index}")
            for index, selection in enumerate(selections)
        ]
        assert all(record["artifact"]["coverage"]["complete"] is False for record in records)
    else:
        records = [observation(context)]
        assert records[0]["artifact"]["coverage"]["complete"] is True
    answer = await deliver(context, records)
    assert context.coverage == "complete" and context.upload_reads
    assert "未读取整个文件" not in answer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename,selection",
    [
        ("data.xlsx", {"sheet": "Data", "cell_range": "A1:A1"}),
        ("pages.pdf", {"page_start": 1, "page_end": 1}),
    ],
)
async def test_real_formula_or_unreadable_page_gap_remains_partial(document_context, filename, selection):
    context = document_context
    (context.work_dir / "uploads" / filename).write_bytes(
        workbook_bytes(formula=True) if filename.endswith("xlsx") else pdf_bytes(blank_first=True)
    )
    record = observation(context, filename, selection)
    missing = record["artifact"]["coverage"]["missing"]
    assert missing and not context.upload_reads
    answer = await deliver(context, [record])
    assert context.coverage == "partial"
    assert all(reason in answer for reason in missing)


def test_catalog_does_not_supply_content_or_resolve_full_read(document_context):
    record = observation(document_context, selection={"mode": "catalog"})
    sources, unfinished, _ = task_evidence([record], "task")
    assert not sources and unfinished and not document_context.upload_reads


@pytest.mark.asyncio
async def test_catalog_followed_by_valid_selection_can_complete(document_context):
    context = document_context
    catalog = observation(context, selection={"mode": "catalog"}, identity="catalog")
    selected = observation(context, selection={"sheet": "Data", "cell_range": "A1:A1"})
    await deliver(context, [catalog, selected])
    assert context.coverage == "complete" and not context.upload_reads


def test_failed_read_and_evidence_integrity_are_not_relaxed(document_context):
    context = document_context
    record = observation(context, selection={"sheet": "Data", "cell_range": "A1:A1"})
    failed = make_record(
        HookCall(
            "document:read_document",
            "failed",
            task_id="task",
            evidence_adapter="document.v1",
            executed=True,
            status="failed",
            error=OSError("controlled read failure"),
        ),
        "",
    )
    _, unfinished, _ = task_evidence([record, failed], "task")
    assert any(item["reason"] == "controlled read failure" for item in unfinished)
    for key, value in [("task_id", "foreign-task"), ("run_id", "foreign-run"), ("evidence_note", "{}")]:
        forged = deepcopy(record)
        forged[key] = value
        with pytest.raises(EvidenceError):
            task_evidence([forged], "task")


@pytest.mark.parametrize(
    "selection",
    [
        {"sheet": "Unknown"},
        {"sheet": "Data", "cell_range": "B2:A1"},
        {"sheet": "Data", "cell_range": "A1:A20001"},
        {"page_start": 1, "page_end": 3},
    ],
)
def test_invalid_or_excessive_range_fails_without_silent_preview(document_context, selection):
    filename = "pages.pdf" if "page_start" in selection else "data.xlsx"
    with pytest.raises(ValueError):
        observation(document_context, filename, selection)
    assert not document_context.upload_reads


@pytest.mark.asyncio
@pytest.mark.parametrize("require_full", [False, True])
async def test_selected_cell_real_worker_http_history_and_download(migrated_environment, monkeypatch, require_full):
    parent, expert = models(
        monkeypatch,
        [
            call("task", {"subagent_type": "document", "description": "只读取 Data!A1"}),
            AIMessage(content="Data!A1为42。"),
        ],
        [
            call(
                "read_document",
                {"filename": "uploads/data.xlsx", "selection": {"sheet": "Data", "cell_range": "A1:A1"}},
            ),
            AIMessage(content="Data!A1为42。"),
        ],
    )
    async with migrated_environment.client() as client:
        thread = (await client.post("/api/threads")).json()["thread_id"]
        upload = await client.post(
            "/api/upload", data={"thread_id": thread}, files={"files": ("data.xlsx", workbook_bytes())}
        )
        assert upload.status_code == 200, upload.text
        submission = await client.post(
            "/api/task",
            json={
                "thread_id": thread,
                "query": "只读取 Data!A1",
                "required_uploads": ["data.xlsx"] if require_full else [],
            },
        )
        assert submission.status_code == 200, submission.text
        run = await finished(client, submission.json()["run_id"])
        assert run["status"] == "completed", run
        assert run["completion"]["status"] == ("partial" if require_full else "complete"), run
        history = (await client.get(f"/api/runs/{run['run_id']}/history")).json()
        (task,) = history["subtasks"]
        assert task["status"] == "completed" and task["target"] == "document"
        directory = migrated_environment.root / "output" / f"session_{thread}" / f"run_{run['run_id']}" / "results"
        saved = json.loads((directory / (task["call_id"] + ".json")).read_text(encoding="utf-8"))
        (observed,) = saved["evidence"]
        assert observed["task_id"] == task["call_id"] and observed["run_id"] == run["run_id"]
        assert observed["status"] == "completed" and observed["adapter"] == "document.v1"
        coverage = observed["artifact"]["coverage"]
        assert coverage["complete"] is False and coverage["missing"] == []
        assert coverage["scope"] == "selection" and coverage["read_ranges"] == {"sheet:1": [1, 1, 1, 1]}
        assert coverage["extents"] == {"sheet:1": [1, 2, 1, 1], "sheet:2": [1, 1, 1, 1]}
        (source,) = saved["sources"]
        assert source["locator"] == dict(sheet="Data", row=1, column_start=1, column_end=1)
        assert json.loads(source["content"]) == [dict(cell="A1", value=42, type="n")]
        assert json.loads(source["inputs"][0]["selection"]) == coverage
        assert bool(saved["unfinished"]) is require_full
        assert (
            "未读取整个文件" in run["answer"]
            if not require_full
            else "尚未完整读取的必需文件：data.xlsx" in run["answer"]
        )
        assert (
            "未读取整个文件" in str(parent.seen_messages[-1])
            if not require_full
            else "材料尚未完整读取" in str(parent.seen_messages[-1])
        )
        downloaded = await client.get(f"/api/runs/{run['run_id']}/files/download?path=answer.md")
        assert downloaded.status_code == 200 and downloaded.content == run["answer"].encode()
        assert sha256(downloaded.content).hexdigest() == run["completion"]["artifact"]["file_digest"]
        assert b"\n" not in downloaded.content.replace(b"\r\n", b"")
        assert not parent.responses and not expert.responses
