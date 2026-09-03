"""Shared image-rendering helpers used by both the OCR and pictogram-icon
detection pipelines -- rasterizing PDF pages and encoding images for the
LLM vision endpoint.
"""

import base64

import pymupdf as fitz


def image_to_data_url(image_bytes: bytes, mime: str = "image/png") -> str:
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def pdf_to_page_images(pdf_bytes: bytes, dpi: int = 200) -> list[bytes]:
    """Rasterize every page of a PDF to PNG bytes using PyMuPDF."""
    images = []
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            pix = page.get_pixmap(matrix=matrix)
            images.append(pix.tobytes("png"))
    return images
