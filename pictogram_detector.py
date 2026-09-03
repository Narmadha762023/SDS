"""GHS hazard pictogram icon detection via LLM vision.

SDS documents almost always render their hazard pictograms as small graphic
icons (a red-bordered diamond containing a black symbol), not as text. That
means neither pdfplumber's text extraction nor OCR-as-transcription can see
them -- there is no text there to read. This module runs a dedicated vision
pass whose only job is to recognize which of the 9 standard GHS icon shapes
are visibly present on the page; it does not transcribe or interpret any
other content. Runs alongside both the PDF Extraction and OCR pipelines.

Capped to the first few pages, since GHS pictograms appear in Section 2
(Hazard Identification) near the start of a standard 16-section SDS.
"""

import json

from openai import OpenAI

from image_utils import image_to_data_url, pdf_to_page_images

DETECT_PROMPT = """You are looking at one page of a Safety Data Sheet. Your \
only task is to identify which GHS hazard pictogram ICONS are visibly \
present as graphics on this page -- not text, not hazard statement codes, \
just the actual icon shapes.

Every GHS pictogram is a white diamond with a red border containing a \
black symbol. The 9 possible icons are:
- GHS01: a black exploding bomb shape
- GHS02: a black flame
- GHS03: a black flame drawn over a black circle
- GHS04: a black gas cylinder
- GHS05: a black image of liquid dripping onto a hand and onto a surface \
(corrosion)
- GHS06: a black skull and crossbones
- GHS07: a black exclamation mark
- GHS08: a black human silhouette with a bursting/starburst shape on the \
chest (a "health hazard" silhouette)
- GHS09: a black dead tree and dead fish (environment)

Look carefully at the page image and list ONLY the codes for icons you can \
actually see drawn on the page. Do not include a code because the product \
sounds hazardous, because a hazard statement or word is mentioned, or for \
any reason other than seeing that specific icon shape. If you see no \
pictogram icons on this page, return an empty list.

Return ONLY a JSON object of the form {{"pictograms": ["GHS02", "GHS05"]}} \
(empty list if none)."""


def _detect_in_image(client: OpenAI, model: str, image_bytes: bytes) -> list:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": DETECT_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": image_to_data_url(image_bytes)},
                    },
                ],
            }
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or "{}"
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []
    codes = data.get("pictograms", [])
    return codes if isinstance(codes, list) else []


def detect_pictograms(
    file_bytes: bytes,
    filename: str,
    api_key: str,
    model: str = "gpt-4o-mini",
    max_pages: int = 3,
) -> list:
    """Return the GHS pictogram codes (e.g. ["GHS02", "GHS05"]) visibly
    present as icons anywhere in the first `max_pages` pages."""
    client = OpenAI(api_key=api_key)
    is_pdf = filename.lower().endswith(".pdf")

    if is_pdf:
        page_images = pdf_to_page_images(file_bytes)[:max_pages]
    else:
        page_images = [file_bytes]

    found = set()
    for image_bytes in page_images:
        found.update(_detect_in_image(client, model, image_bytes))

    return sorted(found)
