"""Read user files or read-only Skill resources inside the allowed directories."""

from pathlib import Path
from typing import Annotated

from langchain_core.tools import tool

from agent.runtime.policies import MissingConfiguration
from agent.runtime.skills import record_read
from api.context import get_session_context
from api.monitor import monitor
from utils.path_utils import resolve_path


@tool
def read_file_content(
    filename: Annotated[str, "本轮文件路径，如 uploads/data.xlsx"],
    instruction: Annotated[str, "内容读取要求"] = "提取全部内容",
) -> str:
    """Read Markdown, plain text, Word, PDF or Excel from an allowed directory."""
    file = Path(resolve_path(filename, get_session_context()))
    if not file.is_file():
        raise FileNotFoundError("Requested file does not exist")
    monitor.report_tool("文件内容读取工具", {"filename": filename, "instruction": instruction})
    try:
        if file.suffix.lower() == ".docx":
            import docx

            return "\n".join(para.text for para in docx.Document(str(file)).paragraphs)
        if file.suffix.lower() == ".pdf":
            import pypdf

            return "\n".join(page.extract_text() or "" for page in pypdf.PdfReader(str(file)).pages)
        if file.suffix.lower() in (".xlsx", ".xls"):
            import pandas as pd

            data = pd.read_excel(str(file))
            return "\n".join(
                [
                    f"文件: {filename}",
                    f"行数: {len(data)}, 列数: {len(data.columns)}",
                    f"列名: {', '.join(data.columns.astype(str))}",
                    "[前5行数据预览]:",
                    data.head().to_string(index=False),
                    "[统计描述]:",
                    data.describe().to_string(),
                ]
            )
        content = file.read_text(encoding="utf-8")
        record_read(file, content)
        return content
    except ImportError as exc:
        raise MissingConfiguration("Required file reader dependency is missing") from exc
    except UnicodeDecodeError as exc:
        raise ValueError("File format cannot be read as UTF-8 text") from exc
