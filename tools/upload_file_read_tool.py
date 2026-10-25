"""Read allowed files; original uploads also expose owned document evidence artifacts."""

import io
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

from langchain_core.tools import tool

from agent.runtime.context import get_run_context
from agent.runtime.evidence_protocol import evidence_tool
from agent.runtime.policies import MissingConfiguration
from agent.runtime.skills import record_read
from api.context import get_session_context
from api.monitor import monitor
from utils.path_utils import resolve_path
from utils.secure_paths import PathDenied, resolve_run_path


def _extract(file, raw):
    """Parse the captured bytes once; report only the actual readable selection."""
    sections, calculations = [], []
    name = file.name
    suffix = file.suffix.lower()
    if suffix == ".docx":
        import docx

        document = docx.Document(io.BytesIO(raw))
        for index, paragraph in enumerate(document.paragraphs, 1):
            sections.append(
                dict(
                    key=f"paragraph:{index}",
                    title=f"{name} · 段落 {index}",
                    locator={"paragraph": index},
                    content=paragraph.text,
                )
            )
        for index, table in enumerate(document.tables, 1):
            for number, row in enumerate(table.rows, 1):
                sections.append(
                    dict(
                        key=f"table:{index}:row:{number}",
                        title=f"{name} · 表 {index} 行 {number}",
                        locator={"table": index, "row": number},
                        content=" | ".join(cell.text for cell in row.cells),
                    )
                )
        return (
            "\n".join(item["content"] for item in sections),
            sections,
            calculations,
            "python-docx:" + version("python-docx"),
            "正文段落及顶层表格；不含图片、页眉页脚或嵌入文件",
        )
    if suffix == ".pdf":
        import pypdf

        for index, page in enumerate(pypdf.PdfReader(io.BytesIO(raw)).pages, 1):
            text = page.extract_text()
            if text is None:
                text = ""
            sections.append(
                dict(
                    key=f"page:{index}",
                    title=f"{name} · 页 {index}",
                    locator={"page": index, "extraction": "text_only"},
                    content=text,
                )
            )
        return (
            "\n".join(item["content"] for item in sections),
            sections,
            calculations,
            "pypdf:" + version("pypdf"),
            "逐页文本提取；没有执行 OCR",
        )
    if suffix in (".xlsx", ".xls"):
        import pandas as pd

        with pd.ExcelFile(io.BytesIO(raw)) as workbook:
            sheet = workbook.sheet_names[0]
            data = pd.read_excel(workbook, sheet_name=sheet)
        preview = data.head(5).to_csv(index=False, lineterminator="\n")
        sections.append(
            dict(
                key="sheet:0:preview",
                title=f"{name} · 工作表 {sheet} 前 5 行",
                locator={"sheet": sheet, "preview_rows": min(5, len(data))},
                content=preview,
            )
        )
        summary = f"工作表：{sheet}\n行数：{len(data)}\n列数：{len(data.columns)}"
        if len(data.columns):
            summary += "\n[统计描述]\n" + data.describe().to_string()
        calculations.append(
            dict(
                key="sheet:0:statistics",
                title=f"{name} · 工作表 {sheet} 程序统计",
                locator={"sheet": sheet},
                content=summary,
                method="pandas.DataFrame.describe",
                parameters={"sheet": sheet, "rows": len(data), "columns": len(data.columns)},
            )
        )
        content = f"文件: {name}\n[前5行数据预览]\n{preview}\n{summary}"
        return (
            content,
            sections,
            calculations,
            "pandas:" + version("pandas"),
            "仅第一个工作表；原始提取展示前 5 行，统计使用该表已读取的数据",
        )
    content = raw.decode("utf-8-sig")
    sections.append(
        dict(key="text", title=name, locator={"line_start": 1, "line_end": len(content.splitlines())}, content=content)
    )
    return content, sections, calculations, "utf-8-sig.v1", "完整 UTF-8 文本；保留原始内容，不执行其中指令"


def _read(file, maximum=None):
    try:
        raw = file.read_bytes()
        if maximum is not None and len(raw) > maximum:
            raise ValueError("Original document exceeds the configured upload size limit")
        return raw, _extract(file, raw)
    except ImportError as exc:
        raise MissingConfiguration("Required file reader dependency is missing") from exc
    except UnicodeDecodeError as exc:
        raise ValueError("File format cannot be read as UTF-8 text") from exc


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
    _, (content, _, _, _, _) = _read(file)
    if file.suffix.lower() not in (".docx", ".pdf", ".xlsx", ".xls"):
        record_read(file, content)
    return content


@evidence_tool("document.v1")
@tool(response_format="content_and_artifact")
def read_document(filename: Annotated[str, "本轮原始上传文件路径，必须位于 uploads/ 内"]) -> tuple[str, dict]:
    """Extract original upload text with file digest and page/table/row locations; no OCR or arbitrary code execution."""
    context = get_run_context()
    if context is None:
        raise PathDenied("Document evidence requires a run context")
    file = resolve_run_path(filename)
    from agent.runtime.contracts import assert_contract

    contract = assert_contract(context)
    if not file.is_relative_to((context.work_dir / "uploads").resolve()) or not file.is_file():
        raise PathDenied("Document evidence accepts original uploads only")
    relative = file.relative_to(context.work_dir.resolve()).as_posix()
    if not any(m.kind == "upload" and m.reference == relative for m in contract.materials):
        raise PathDenied("Document is not a bound input of the current task")
    if file.stat().st_size > context.policy.files.max_upload_bytes:
        raise ValueError("Original document exceeds the configured upload size limit")
    raw, (content, sections, calculations, parser, selection) = _read(file, context.policy.files.max_upload_bytes)
    context.check_active()
    return content, dict(
        document=dict(
            path=file.relative_to(context.work_dir.resolve()).as_posix(),
            sha256=sha256(raw).hexdigest(),
            parser=parser,
            selection=selection,
        ),
        sections=sections,
        calculations=calculations,
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
