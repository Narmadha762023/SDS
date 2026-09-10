"""Renders a single PDF page as a clean image, optionally with a real
highlight drawn over a matched phrase.

Used instead of embedding the browser's native PDF viewer (via an iframe)
for showing a search match -- that approach pulls in the whole viewer's
own toolbar/controls and, with several results open, gets visually
cluttered fast. A single rendered page image has none of that chrome, and
when a bounding box is available (see pdf_word_index.py) it's highlighted
exactly like a real PDF annotation, not just "here's the right page."
"""

import io

import fitz

RENDER_ZOOM = 2.0  # ~144 DPI -- sharp enough to read, not excessive


def render_page(file_bytes: bytes, page_number: int, bbox: dict = None) -> bytes:
    """Returns PNG bytes for `page_number` (1-indexed) of the PDF. If
    `bbox` ({"x0", "top", "x1", "bottom"}, in the page's own PDF-point
    coordinates -- see pdf_word_index.find_match) is given, a highlight
    annotation is added over that exact rectangle before rendering, the
    same coordinate system pdfplumber and PyMuPDF both use for an
    unrotated page, so no conversion is needed between them.
    """
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    page = doc[page_number - 1]

    if bbox:
        rect = fitz.Rect(bbox["x0"], bbox["top"], bbox["x1"], bbox["bottom"])
        annot = page.add_highlight_annot(rect)
        annot.set_colors(stroke=(1, 0.85, 0.2))
        annot.update()

    pix = page.get_pixmap(matrix=fitz.Matrix(RENDER_ZOOM, RENDER_ZOOM))
    buf = io.BytesIO(pix.tobytes("png"))
    doc.close()
    return buf.getvalue()
