"""OCR extraction (Option 2).

Used only when the user explicitly selects the OCR method. Scanned PDFs and
images have no reliable text layer, so instead of a local OCR engine this
module rasterizes each page (or uses the image as-is) and asks the LLM's
vision endpoint to transcribe the visible text verbatim. No text is
invented -- the model is instructed to transcribe only, not interpret.

`extract_text_and_pictograms()` also folds in pictogram-icon detection for
the first few pages, in the SAME vision call as the transcription --
transcription and icon detection would otherwise send the exact same page
image to the AI twice (once per job), so combining them halves the vision
calls (and image-token cost) for those overlapping pages. This only applies
here, in OCR mode: PDF Extraction mode never sends page images for
transcription in the first place, so there's nothing to combine there --
it still calls pictogram_detector.detect_pictograms() as its own step.
"""

import json

from openai import OpenAI

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


def _transcribe_image(client: OpenAI, model: str, image_bytes: bytes) -> str:
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
    return (response.choices[0].message.content or "").strip()


def _transcribe_and_detect(client: OpenAI, model: str, image_bytes: bytes):
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


def extract_text(
    file_bytes: bytes,
    filename: str,
    api_key: str,
    model: str = "gpt-4o-mini",
) -> str:
    """Run OCR (via LLM vision) on an uploaded PDF or image file.

    Returns the transcribed text, page by page for PDFs.
    """
    client = OpenAI(api_key=api_key)
    is_pdf = filename.lower().endswith(".pdf")

    if is_pdf:
        page_images = pdf_to_page_images(file_bytes)
    else:
        page_images = [file_bytes]

    transcripts = []
    for i, image_bytes in enumerate(page_images, start=1):
        text = _transcribe_image(client, model, image_bytes)
        if text:
            transcripts.append(f"--- Page {i} ---\n{text}")

    return "\n\n".join(transcripts).strip()


def extract_text_and_pictograms(
    file_bytes: bytes,
    filename: str,
    api_key: str,
    model: str = "gpt-4o-mini",
    max_pictogram_pages: int = 3,
):
    """Run OCR and pictogram-icon detection together.

    For the first `max_pictogram_pages` pages, transcription and icon
    detection happen in a single combined vision call per page (instead of
    two separate calls sending the same image twice). Remaining pages are
    transcribed only, since GHS pictograms are essentially always found
    early in a standard SDS, so there's no reason to keep asking about
    icons past that point.

    Returns (transcribed_text, pictogram_codes).
    """
    client = OpenAI(api_key=api_key)
    is_pdf = filename.lower().endswith(".pdf")
    page_images = pdf_to_page_images(file_bytes) if is_pdf else [file_bytes]

    transcripts = []
    pictogram_codes = set()

    for i, image_bytes in enumerate(page_images, start=1):
        if i <= max_pictogram_pages:
            text, codes = _transcribe_and_detect(client, model, image_bytes)
            pictogram_codes.update(codes)
        else:
            text = _transcribe_image(client, model, image_bytes)
        if text:
            transcripts.append(f"--- Page {i} ---\n{text}")

    return "\n\n".join(transcripts).strip(), sorted(pictogram_codes)
