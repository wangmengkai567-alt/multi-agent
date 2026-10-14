"""Convert bounded report files using a private, hidden Word instance."""

import logging
import tempfile
from html.parser import HTMLParser
from pathlib import Path

from agent.runtime.policies import MissingConfiguration
from utils.path_utils import PathDenied, resolve_path

logger = logging.getLogger(__name__)


class ReportHTMLGuard(HTMLParser):
    """Reject resources which Word could load outside the run directory."""

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"img", "script", "iframe", "object", "embed", "link", "base", "style", "svg"}:
            raise PathDenied("Embedded resources and active HTML are disabled in PDF reports")
        for name, value in attrs:
            if name.lower().startswith("on") or name.lower() in {"src", "srcset", "background", "style"}:
                raise PathDenied("Active or resource-loading HTML attributes are disabled")
            if name.lower() == "href" and value and not value.startswith(("https://", "http://", "#")):
                raise PathDenied("Report links must be web citations or document anchors")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


def convert_md_to_pdf_via_word(md_abs_path: Path, pdf_abs_path: Path) -> str:
    md_abs_path = Path(resolve_path(str(md_abs_path)))
    pdf_abs_path = Path(resolve_path(str(pdf_abs_path), write=True))
    try:
        import markdown
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise MissingConfiguration("PDF conversion requires pywin32, Markdown and Microsoft Word") from exc
    html_body = markdown.markdown(md_abs_path.read_text(encoding="utf-8"), extensions=["tables", "fenced_code"])
    ReportHTMLGuard().feed(html_body)
    html = '<html><head><meta charset="UTF-8"></head><body>' + html_body + "</body></html>"
    temp_path = None
    word = None
    document = None
    initialized = False
    try:
        pdf_abs_path.parent.mkdir(parents=True, exist_ok=True)
        # Never put temporary HTML beside a read-only Skill or uploaded source.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", dir=pdf_abs_path.parent, encoding="utf-8", delete=False
        ) as stream:
            temp_path = Path(stream.name)
            stream.write(html)
        pythoncom.CoInitialize()
        initialized = True
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = False
        document = word.Documents.Open(str(temp_path))
        document.SaveAs(str(pdf_abs_path), FileFormat=17)
        if not pdf_abs_path.is_file():
            raise RuntimeError("Word completed without creating a PDF file")
        return f"成功转换: {pdf_abs_path} (Word引擎)"
    finally:
        if document is not None:
            try:
                document.Close(SaveChanges=0)
            except Exception:
                logger.exception("Could not close the private Word document")
        if word is not None:
            try:
                word.Quit()
            except Exception:
                logger.exception("Could not close the private Word instance")
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        if initialized:
            pythoncom.CoUninitialize()
