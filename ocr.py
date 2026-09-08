"""OCR extraction (Option 2).

Used only when the user explicitly selects the OCR method. Scanned PDFs and
images have no reliable text layer, so instead of a local OCR engine this
module rasterizes each page (or uses the image as-is) and asks the LLM's
vision endpoint to transcribe the visible text verbatim. No text is
invented -- the model is instructed to transcribe only, not interpret.

`extract_text_and_pictograms()` tries non-AI pictogram detection first
(template_matcher -- embedded-image match, then OpenCV region match on the
rendered pages) since text transcription needs the page images rendered
anyway, so trying those costs nothing extra. Only when neither finds
anything does it fall back to asking the vision model to also look for
pictogram icons, in the SAME call as the transcription (rather than a
second call sending the same image again) -- combining them halves the
vision calls for the pages where the AI fallback is actually needed. PDF
Extraction mode never renders page images at all, so it goes through
`extraction_pipeline._detect_pictogram_icons()`'s own three-tier logic
instead.
"""

import json

from openai import OpenAI

import template_matcher
from image_utils import image_to_data_url, pdf_to_page_images

TRANSCRIBE_PROMPT = (
    "You are an OCR engine. Transcribe ALL visible text in this image "
    "exactly as it appears, preserving structure and section order as best "
    "you can. Do not summarize, translate, explain, or add any text that is "
    "not visibly present in the image. If the image contains no readable "
    "text, return an empty string."
)

# Same icon descriptions as pictogram_detector.py's DETECT_PROMPT, folded
# into a combined "transcribe + detect" instruction for OCR mode.
COMBINED_PROMPT = """You are looking at one page of a document. Do TWO \
things and return ONE JSON object with both results.

1. Transcribe ALL visible text on this page exactly as it appears, \
preserving structure and section order as best you can. Do not summarize, \
translate, explain, or add any text that is not visibly present. Use "" \
if there is no readable text.

2. Identify which GHS hazard pictogram ICONS are visibly present as \
graphics on this page -- not text, not hazard statement codes, just the \
actual icon shapes. Every GHS pictogram is a white diamond with a red \
border containing a black symbol. The 9 possible icons are:
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
List ONLY codes for icons you can actually see drawn on the page. Do not \
include a code because the text sounds hazardous or mentions a hazard \
statement -- only because you see that specific icon shape. Use an empty \
list if none are visible.

Return ONLY a JSON object of the form:
{"text": "<transcribed text, or \\"\\" if none>", "pictograms": ["GHS02", "GHS05"]}
(empty list for pictograms if none are visible)."""


def _transcribe_image(client: OpenAI, model: str, image_bytes: bytes, tracker=None) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": TRANSCRIBE_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": image_to_data_url(image_bytes)},
                    },
                ],
            }
        ],
        temperature=0,
    )
    if tracker is not None:
        tracker.record(response, model)
    return (response.choices[0].message.content or "").strip()


def _transcribe_and_detect(client: OpenAI, model: str, image_bytes: bytes, tracker=None):
    """One combined vision call: transcribe the page AND look for pictogram
    icons. Returns (text, pictogram_codes)."""
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": COMBINED_PROMPT},
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
    if tracker is not None:
        tracker.record(response, model)
    content = response.choices[0].message.content or "{}"
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return "", []

    text = data.get("text")
    text = text.strip() if isinstance(text, str) else ""

    codes = data.get("pictograms", [])
    codes = codes if isinstance(codes, list) else []

    return text, codes


def extract_text_and_pictograms(
    file_bytes: bytes,
    filename: str,
    api_key: str,
    model: str = "gpt-4o-mini",
    max_pictogram_pages: int = 3,
    tracker=None,
):
    """Run OCR and pictogram-icon detection together.

    Pictogram detection is tried non-AI first (template_matcher), since
    transcription already needs the page images rendered -- if that finds
    a result, every page (including the first `max_pictogram_pages`) is
    transcribed with the plain prompt, and the AI is never asked about
    icons at all. Only if the non-AI tiers find nothing does the first
    `max_pictogram_pages` fall back to the combined "transcribe + detect"
    call, so the AI is only asked once per page either way -- never twice.
    `tracker`, if given a usage_tracker.UsageTracker, records every call's
    token usage onto it.

    Returns (transcribed_text, pictogram_codes).
    """
    client = OpenAI(api_key=api_key)
    is_pdf = filename.lower().endswith(".pdf")
    page_images = pdf_to_page_images(file_bytes) if is_pdf else [file_bytes]

    pictogram_codes = set()
    try:
        pictogram_codes.update(
            template_matcher.match_embedded_images(file_bytes, filename, max_pages=max_pictogram_pages)
        )
    except Exception:
        pass
    if not pictogram_codes:
        try:
            for image_bytes in page_images[:max_pictogram_pages]:
                pictogram_codes.update(template_matcher.match_page_regions(image_bytes))
        except Exception:
            pass

    need_ai_pictogram_detection = not pictogram_codes

    transcripts = []
    for i, image_bytes in enumerate(page_images, start=1):
        if need_ai_pictogram_detection and i <= max_pictogram_pages:
            text, codes = _transcribe_and_detect(client, model, image_bytes, tracker)
            pictogram_codes.update(codes)
        else:
            text = _transcribe_image(client, model, image_bytes, tracker)
        if text:
            transcripts.append(f"--- Page {i} ---\n{text}")

    return "\n\n".join(transcripts).strip(), sorted(pictogram_codes)
