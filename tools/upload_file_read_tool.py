# 文件读取入口：普通查看与文档证据分开处理，文档证据必须对应本轮契约中的原始上传。
"""Read allowed files; original uploads also expose owned document evidence artifacts."""

import base64
from hashlib import sha256
from pathlib import Path
from typing import Annotated

from langchain_core.tools import tool

from agent.runtime.context import get_run_context
from agent.runtime.errors import DependencyUnavailable
from agent.runtime.evidence_protocol import evidence_tool
from agent.runtime.skills import stage_read
from api.context import get_session_context
from api.monitor import monitor
from tools.document_parser import DocumentSelection, extract_document
from utils.path_utils import resolve_path
from utils.secure_paths import PathDenied, resolve_run_path


def _read_local(file, maximum, selection=None):
    try:
        with file.open("rb") as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError("Original document exceeds the configured upload size limit")
        return raw, extract_document(file, raw, selection)
    except ImportError as exc:
        raise DependencyUnavailable("Required file reader dependency is missing") from exc
    except UnicodeDecodeError as exc:
        raise ValueError("File format cannot be read as UTF-8 text") from exc


def _read(file, maximum=None, selection=None):
    from agent.runtime.policies import policy_for_run, request_timeout
    from agent.runtime.workers import run_operation

    policy = policy_for_run()
    result = run_operation(
        "document_read",
        dict(path=str(file), maximum=maximum or policy.files.max_upload_bytes, selection=selection),
        seconds=request_timeout(policy.timeouts.tool_seconds),
    )
    return base64.b64decode(result["raw"], validate=True), result["result"]


@evidence_tool("control.v1")
@tool
def read_file_content(
    filename: Annotated[str, "本轮文件路径，如 uploads/data.xlsx"],
    instruction: Annotated[str, "内容读取要求"] = "提取全部内容",
) -> str:
    """Inspect allowed files or Skill instructions; delegate factual upload analysis to the document expert."""
    file = Path(resolve_path(filename, get_session_context()))
    if not file.is_file():
        raise FileNotFoundError("Requested file does not exist")
    monitor.report_tool("文件内容读取工具", {"filename": filename, "instruction": instruction})
    raw, (content, _, _, _, _, _) = _read(file)
    if file.suffix.lower() not in (".docx", ".pdf", ".xlsx", ".xls"):
        original = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        stage_read(file, original, numbered=False)
    return content


@evidence_tool("document.v1")
@tool(response_format="content_and_artifact")
def read_document(
    filename: Annotated[str, "本轮原始上传文件路径，必须位于 uploads/ 内"],
    selection: Annotated[
        DocumentSelection | None,
        "空值读取有界完整内容；catalog查看工作表/页目录；content按sheet/cell_range或page_start/page_end定位读取",
    ] = None,
) -> tuple[str, dict]:
    """Read bound uploads with exact page/table/cell positions and explicit completeness. All Excel sheets are read unless selected; large files require catalog then bounded ranges. No OCR or formula evaluation."""
    context = get_run_context()
    if context is None:
        raise PathDenied("Document evidence requires a run context")
    file = resolve_run_path(filename)
    from agent.runtime.contracts import assert_contract

    contract = assert_contract(context)
    if not file.is_relative_to((context.work_dir / "uploads").resolve()) or not file.is_file():
        raise PathDenied("Document evidence accepts original uploads only")
    # 仅目录在 uploads 内还不够，还需确认文件已经登记为当前任务的输入。
    relative = file.relative_to(context.work_dir.resolve()).as_posix()
    if not any(m.kind == "upload" and m.reference == relative for m in contract.materials):
        raise PathDenied("Document is not a bound input of the current task")
    if file.stat().st_size > context.policy.files.max_upload_bytes:
        raise ValueError("Original document exceeds the configured upload size limit")
    raw, (content, sections, calculations, parser, description, coverage) = _read(
        file,
        context.policy.files.max_upload_bytes,
        selection.model_dump() if isinstance(selection, DocumentSelection) else selection,
    )
    context.check_active()
    # 模型看到正文；证据载荷另保留文件摘要、解析器、位置和读取覆盖范围。
    return content, dict(
        document=dict(
            path=file.relative_to(context.work_dir.resolve()).as_posix(),
            sha256=sha256(raw).hexdigest(),
            parser=parser,
            selection=description,
        ),
        sections=sections,
        calculations=calculations,
        coverage=coverage,
    )


@evidence_tool("record.v1")
@tool
def read_bound_material(reference: Annotated[str, "当前契约已登记的文本材料 reference"]) -> str:
    """Read one bound text; proves the material's wording, not independent external truth."""
    from agent.runtime.contracts import assert_contract

    contract = assert_contract(get_run_context())
    matches = [
        material for material in contract.materials if material.reference == reference and material.text is not None
    ]
    if len(matches) != 1:
        raise ValueError("Unknown or ambiguous bound text reference")
    return matches[0].text
