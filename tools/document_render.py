"""Local metadata inspection and bounded page rendering for uploaded documents."""

import base64
import io
from contextlib import closing
from importlib.metadata import version

from tools.document_parser import MAX_TEXT_CHARS, DocumentSelection

VISUAL_SUFFIXES = frozenset((".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"))
MAX_PAGES = 2
LONG_EDGE = 2048


def render_upload(file, raw, selection):
    """Runs only in the owned worker; it does not contact a model or fetch URLs."""
    select = DocumentSelection.model_validate(selection or {})
    if select.sheet is not None:
        raise ValueError("Visual documents do not accept worksheet selectors")
    document = None
    image = None
    try:
        if file.suffix.lower() == ".pdf":
            import pypdf
            import pypdfium2

            if pypdf.PdfReader(io.BytesIO(raw)).is_encrypted:
                raise ValueError("Encrypted PDFs are not supported")
            document = pypdfium2.PdfDocument(raw)
            total = len(document)
            renderer = "pypdfium2:" + version("pypdfium2")
        else:
            from PIL import Image, ImageOps

            image = Image.open(io.BytesIO(raw))
            total = image.n_frames if hasattr(image, "n_frames") else 1
            renderer = "pillow:" + version("pillow")
        if total < 1:
            raise ValueError("Visual document has no pages")
        first, last = (select.page_start, select.page_end) if select.page_start else (1, total)
        if last > total:
            raise ValueError("Page range is outside the document")
        if select.mode != "catalog" and last - first + 1 > MAX_PAGES:
            raise ValueError("Visual reads accept at most two pages; inspect catalog then select page_start/page_end")
        pages = []
        if select.mode != "catalog":
            for index in range(first, last + 1):
                native = ""
                if document is not None:
                    with closing(document[index - 1]) as page:
                        with closing(page.get_textpage()) as text_page:
                            if text_page.count_chars() > MAX_TEXT_CHARS:
                                raise ValueError("Page native text exceeds the extraction limit")
                            native = text_page.get_text_bounded()
                        width, height = page.get_size()
                        if min(width, height) <= 0:
                            raise ValueError("PDF page has invalid dimensions")
                        with closing(page.render(scale=min(2.0, LONG_EDGE / max(width, height)))) as bitmap:
                            rendered = bitmap.to_pil().convert("RGB")
                else:
                    image.seek(index - 1)
                    with ImageOps.exif_transpose(image) as oriented:
                        oriented.thumbnail((LONG_EDGE, LONG_EDGE), Image.Resampling.LANCZOS)
                        with oriented.convert("RGBA") as rgba, rgba.getchannel("A") as alpha:
                            rendered = Image.new("RGB", rgba.size, "white")
                            rendered.paste(rgba, mask=alpha)
                try:
                    stream = io.BytesIO()
                    rendered.save(stream, format="JPEG", quality=90)
                    pages.append(
                        dict(
                            page=index,
                            native_text=native,
                            width=rendered.width,
                            height=rendered.height,
                            image=base64.b64encode(stream.getvalue()).decode("ascii"),
                        )
                    )
                finally:
                    rendered.close()
        return dict(
            raw=base64.b64encode(raw).decode("ascii"),
            total=total,
            first=first,
            last=last,
            renderer=renderer,
            pages=pages,
        )
    finally:
        if document is not None:
            document.close()
        if image is not None:
            image.close()
