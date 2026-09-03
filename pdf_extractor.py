"""PDF text extraction (Option 1).

Reads the text layer of a machine-readable PDF. No OCR happens here.
"""

import io

import pdfplumber


def extract_text(pdf_bytes: bytes) -> str:
    """Return the text layer of a PDF, page by page."""
    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if text.strip():
                pages.append(f"--- Page {i} ---\n{text.strip()}")
    return "\n\n".join(pages).strip()


def page_count(pdf_bytes: bytes) -> int:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return len(pdf.pages)
