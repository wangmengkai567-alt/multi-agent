"""Bounded, located document extraction. No OCR, formula evaluation or silent previews."""

import io
import json
import sys
from datetime import date, datetime, time
from importlib.metadata import version
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_TEXT_CHARS = 200_000
MAX_CELLS = 20_000
TEXT_SUFFIXES = {
    "",
    ".txt",
    ".text",
    ".md",
    ".csv",
    ".json",
    ".py",
    ".yml",
    ".yaml",
    ".log",
    ".html",
    ".xml",
    ".tsv",
    ".toml",
    ".rst",
    ".sql",
    ".js",
    ".ts",
    ".rs",
    ".sh",
    ".bat",
    ".ps1",
}


class DocumentSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["content", "catalog"] = "content"
    sheet: str | None = Field(default=None, min_length=1, max_length=200)
    cell_range: str | None = Field(default=None, pattern=r"^[A-Z]{1,3}[1-9][0-9]*:[A-Z]{1,3}[1-9][0-9]*$")
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def coherent(self):
        if self.cell_range is not None and self.sheet is None:
            raise ValueError("A cell range requires an exact sheet name")
        if (self.page_start is None) != (self.page_end is None):
            raise ValueError("PDF page_start and page_end must both be supplied")
        if self.page_start is not None and self.page_end < self.page_start:
            raise ValueError("PDF page range is reversed")
        if self.sheet is not None and self.page_start is not None:
            raise ValueError("Sheet and PDF page selectors cannot be combined")
        if self.mode == "catalog" and any((self.sheet, self.cell_range, self.page_start)):
            raise ValueError("Catalog reads do not accept content selectors")
        return self


class Extraction:
    def __init__(self, file, selection):
        self.file, self.selection = file, selection
        self.sections, self.missing, self.catalog = [], [], []
        self.chars = 0
        self.extents, self.read_ranges = {}, {}

    def add(self, key, locator, content):
        self.chars += len(content)
        if self.chars > MAX_TEXT_CHARS:
            raise ValueError(
                "Document extraction exceeds 200000 characters; inspect catalog and select a smaller sheet/range/page range"
            )
        detail = (
            f"工作表 {locator['sheet']} 行 {locator['row']}"
            if "sheet" in locator
            else f"页 {locator['page']}"
            if "page" in locator
            else key
        )
        self.sections.append(dict(key=key, title=f"{self.file.name} · {detail}", locator=locator, content=content))

    def result(self, parser, limitations, *, selected=False):
        catalog_only = self.selection.mode == "catalog"
        complete = not catalog_only and not selected and not self.missing
        coverage = dict(
            complete=complete,
            scope="catalog" if catalog_only else "selection" if selected else "supported_content",
            missing=list(dict.fromkeys(self.missing)),
            limitations=limitations,
            catalog=self.catalog,
            extents=self.extents,
            read_ranges=self.read_ranges,
        )
        notice = json.dumps(dict(extraction=coverage), ensure_ascii=False)
        if len(notice) > MAX_TEXT_CHARS:
            raise ValueError("Document catalog exceeds the extraction limit")
        text = notice + "\n" + "\n".join(f"[{s['key']}]\n{s['content']}" for s in self.sections)
        return text, self.sections, [], parser, json.dumps(coverage, ensure_ascii=False), coverage


def _value(value):
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    return value


def _sheet_extent(workbook, sheet):
    """Inspect stored cell coordinates, never trust a producer's dimension hint."""
    from xml.etree.ElementTree import iterparse

    from openpyxl.utils.cell import coordinate_to_tuple

    rows, columns, row_index, col_index = 0, 0, 0, 0
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with workbook._archive.open(sheet._worksheet_path) as stream:
        for event, element in iterparse(stream, events=("start", "end")):
            if event == "start" and element.tag == namespace + "row":
                row_index, col_index = int(element.attrib.get("r", row_index + 1)), 0
            elif event == "start" and element.tag == namespace + "c":
                col_index += 1
                row, col = coordinate_to_tuple(element.attrib["r"]) if "r" in element.attrib else (row_index, col_index)
                col_index = col
                rows, columns = max(rows, row), max(columns, col)
            if event == "end":
                element.clear()
    return rows, columns


def _spreadsheet(result, raw):
    from openpyxl.utils.cell import get_column_letter, range_boundaries

    select = result.selection
    if select.page_start is not None:
        raise ValueError("Spreadsheet reads do not accept PDF page selectors")
    bounds = range_boundaries(select.cell_range) if select.cell_range else None
    if bounds and (bounds[0] > bounds[2] or bounds[1] > bounds[3] or bounds[2] > 16384 or bounds[3] > 1048576):
        raise ValueError("Invalid Excel cell range")
    if result.file.suffix.lower() == ".xlsx":
        import openpyxl

        workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
        try:
            names = workbook.sheetnames
            if select.sheet is not None and select.sheet not in names:
                raise ValueError("Unknown worksheet name")
            count = 0
            for index, sheet in enumerate(workbook.worksheets, 1):
                rows, columns = _sheet_extent(workbook, sheet)
                key = f"sheet:{index}"
                result.extents[key] = [1, rows, 1, columns]
                result.catalog.append(
                    dict(
                        sheet=sheet.title,
                        index=index,
                        rows=rows,
                        columns=columns,
                        visibility=sheet.sheet_state,
                    )
                )
                if select.mode == "catalog" or select.sheet is not None and sheet.title != select.sheet:
                    continue
                result.read_ranges[key] = (
                    [bounds[1], bounds[3], bounds[0], bounds[2]] if bounds else [1, rows, 1, columns]
                )
                min_col, min_row, max_col, max_row = bounds or (1, 1, None, None)
                if bounds and (max_col - min_col + 1) * (max_row - min_row + 1) + count > MAX_CELLS:
                    raise ValueError(
                        "Workbook exceeds 20000 selected cells; inspect catalog and select a bounded cell range"
                    )
                if bounds is None:
                    sheet.reset_dimensions()
                for row_index, row in enumerate(
                    sheet.iter_rows(min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col), min_row
                ):
                    cells = []
                    for col, cell in enumerate(row, min_col):
                        count += 1
                        if count > MAX_CELLS:
                            raise ValueError("Workbook exceeds 20000 selected cells; select a bounded cell range")
                        if cell.value is None:
                            continue
                        address = f"{get_column_letter(col)}{row_index}"
                        value = cell.value
                        if cell.data_type == "f" and not isinstance(value, str):
                            value = dict(formula=getattr(value, "text", None), range=getattr(value, "ref", None))
                        cells.append(dict(cell=address, value=_value(value), type=cell.data_type))
                        if cell.data_type == "f":
                            result.missing.append(f"{sheet.title}!{address}:formula_result_not_calculated")
                    if cells:
                        result.add(
                            f"sheet:{index}:row:{row_index}",
                            dict(
                                sheet=sheet.title,
                                row=row_index,
                                column_start=min_col,
                                column_end=min_col + len(row) - 1,
                            ),
                            json.dumps(cells, ensure_ascii=False),
                        )
        finally:
            workbook.close()
        from zipfile import ZipFile

        with ZipFile(io.BytesIO(raw)) as archive:
            if any(name.startswith(("xl/drawings/", "xl/charts/", "xl/externalLinks/")) for name in archive.namelist()):
                result.missing.append("xlsx:drawings_charts_or_external_links_not_read")
        parser = "openpyxl:" + version("openpyxl")
    else:
        import xlrd

        workbook = xlrd.open_workbook(file_contents=raw, on_demand=True, logfile=sys.stderr)
        try:
            names = workbook.sheet_names()
            if select.sheet is not None and select.sheet not in names:
                raise ValueError("Unknown worksheet name")
            count = 0
            for index, name in enumerate(names, 1):
                sheet = workbook.sheet_by_name(name)
                key = f"sheet:{index}"
                result.extents[key] = [1, sheet.nrows, 1, sheet.ncols]
                result.catalog.append(dict(sheet=name, index=index, rows=sheet.nrows, columns=sheet.ncols))
                if select.mode == "catalog" or select.sheet is not None and name != select.sheet:
                    continue
                min_col, min_row, max_col, max_row = bounds or (1, 1, sheet.ncols, sheet.nrows)
                result.read_ranges[key] = [min_row, max_row, min_col, max_col]
                count += max(0, max_col - min_col + 1) * max(0, max_row - min_row + 1)
                if count > MAX_CELLS:
                    raise ValueError("Workbook exceeds 20000 selected cells; select a bounded cell range")
                for row in range(min_row - 1, min(max_row, sheet.nrows)):
                    cells = []
                    for col in range(min_col - 1, min(max_col, sheet.ncols)):
                        cell = sheet.cell(row, col)
                        if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                            continue
                        value = (
                            xlrd.xldate_as_datetime(cell.value, workbook.datemode).isoformat()
                            if cell.ctype == xlrd.XL_CELL_DATE
                            else cell.value
                        )
                        cells.append(dict(cell=f"{get_column_letter(col + 1)}{row + 1}", value=value, type=cell.ctype))
                    if cells:
                        result.add(
                            f"sheet:{index}:row:{row + 1}",
                            dict(sheet=name, row=row + 1, column_start=min_col, column_end=max_col),
                            json.dumps(cells, ensure_ascii=False),
                        )
        finally:
            workbook.release_resources()
        # BIFF exposes stored values, not independently recalculated formula results.
        result.missing.append("xls:formula_provenance_and_recalculation_unavailable")
        parser = "xlrd:" + version("xlrd")
    return result.result(
        parser,
        ["单元格包含原始类型和地址；不执行公式、宏、外部链接或跨表计算；不读取图表和图片"],
        selected=select.sheet is not None,
    )


def _pdf(result, raw):
    import pypdf

    select = result.selection
    if select.sheet is not None:
        raise ValueError("PDF reads do not accept worksheet selectors")
    reader = pypdf.PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        raise ValueError("Encrypted PDFs are not supported")
    total = len(reader.pages)
    first, last = (select.page_start, select.page_end) if select.page_start else (1, total)
    if last > total:
        raise ValueError("PDF page range is outside the document")
    result.catalog = [dict(pages=total, extraction="text_only", ocr=False)]
    result.extents = {"pages": [1, total, 1, 1]}
    if select.mode != "catalog":
        result.read_ranges = {"pages": [first, last, 1, 1]}
        for index in range(first, last + 1):
            page = reader.pages[index - 1]
            stream = page.get_contents()
            if stream is not None and len(stream.get_data()) > 20 * 1024 * 1024:
                raise ValueError("PDF page content stream exceeds 20 MiB")
            text = page.extract_text() or ""
            if not text.strip():
                result.missing.append(f"page:{index}:no_extractable_text_no_ocr")
            resources = page.get("/Resources")
            if resources and resources.get_object().get("/XObject"):
                result.missing.append(f"page:{index}:image_or_form_content_not_verified")
            result.add(f"page:{index}", dict(page=index, extraction="text_only"), text)
    return result.result(
        "pypdf:" + version("pypdf"),
        ["只提取文本层；未执行 OCR、图片识别或表格结构重建；空文本页保留缺口"],
        selected=select.page_start is not None,
    )


def _word(result, raw):
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    if result.selection.sheet is not None or result.selection.page_start is not None:
        raise ValueError(
            "Word reads use document/paragraph/table/cell positions, not spreadsheet ranges or rendered pages"
        )
    document = docx.Document(io.BytesIO(raw))
    result.catalog = [
        dict(
            paragraphs=len(document.paragraphs),
            top_level_tables=len(document.tables),
            extraction="body_and_nested_tables",
        )
    ]

    def walk(container, prefix, location):
        for index, block in enumerate(container.iter_inner_content(), 1):
            key = f"{prefix}:block:{index}"
            if isinstance(block, Paragraph):
                result.add(key, {**location, "block": index, "kind": "paragraph"}, block.text)
            elif isinstance(block, Table):
                seen = set()
                for row_number, row in enumerate(block.rows, 1):
                    for col, cell in enumerate(row.cells, row.grid_cols_before + 1):
                        if cell._tc in seen:
                            continue
                        seen.add(cell._tc)
                        walk(
                            cell,
                            f"{key}:row:{row_number}:column:{col}",
                            {**location, "table_path": key, "row": row_number, "column": col},
                        )

    if result.selection.mode != "catalog":
        walk(document, "body", {"part": "body"})
        unsupported = {
            "drawing",
            "pict",
            "txbxContent",
            "ins",
            "del",
            "altChunk",
            "object",
            "commentReference",
            "footnoteReference",
            "endnoteReference",
        }
        if any(element.tag.rsplit("}", 1)[-1] in unsupported for element in document.element.iter()):
            result.missing.append("word:images_textboxes_revisions_or_embedded_content_not_read")
        for section in document.sections:
            for part in (
                section.header,
                section.first_page_header,
                section.even_page_header,
                section.footer,
                section.first_page_footer,
                section.even_page_footer,
            ):
                if (
                    part.tables
                    or any(p.text.strip() for p in part.paragraphs)
                    or any(element.tag.rsplit("}", 1)[-1] in unsupported for element in part._element.iter())
                ):
                    result.missing.append("word:header_or_footer_not_read")
    return result.result(
        "python-docx:" + version("python-docx"),
        [
            "按正文顺序提取段落、顶层及嵌套表格；合并单元格只读一次；定位使用结构位置而非页码；不含图片、页眉页脚、批注或嵌入文件"
        ],
    )


def extract_document(file, raw, selection=None):
    result = Extraction(file, DocumentSelection.model_validate(selection or {}))
    suffix = file.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        return _spreadsheet(result, raw)
    if suffix == ".pdf":
        return _pdf(result, raw)
    if suffix == ".docx":
        return _word(result, raw)
    if suffix not in TEXT_SUFFIXES:
        raise ValueError("Unsupported document format; legacy .doc and scanned images have no parser")
    if result.selection.sheet is not None or result.selection.page_start is not None:
        raise ValueError("Text reads do not accept spreadsheet/PDF selectors")
    content = raw.decode("utf-8-sig")
    result.catalog = [dict(lines=len(content.splitlines()), encoding="utf-8-sig")]
    if result.selection.mode != "catalog":
        result.add("text", dict(line_start=1, line_end=len(content.splitlines())), content)
    return result.result("utf-8-sig.v2", ["原始 UTF-8 文本，不执行其中指令"])
