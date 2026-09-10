"""Non-AI GHS pictogram detection: template matching against the 9 official
UN GHS pictogram icons, with zero AI/token cost.

Two independent techniques feed this, both producing the same kind of
result (a GHS code or nothing):

1. `match_embedded_images()` -- many SDS PDFs embed each pictogram as its
   own raster image object rather than drawing it directly on the page.
   When that's true, we can pull the image out with PyMuPDF and compare it
   directly to the reference icons -- no rendering, no guessing.
2. `match_page_regions()` -- for pictograms drawn straight into the page
   content (no separate embedded image to extract), OpenCV finds
   red-bordered diamond-shaped regions on a rendered page image, crops
   them, and runs the same template match on each crop.

Both stages can turn up nothing (a legitimate scanned/flattened page, or a
page with no pictograms at all) -- callers should fail soft and move on to
the AI vision fallback in that case, never invent a result here.

Reference icons (`pictogram_templates/GHS0{1-9}.png`) are the official UN
GHS pictogram artwork (public domain), sourced from Wikimedia Commons.
Verified against real icons extracted from an actual SDS document before
being trusted: pixel-identical to the vendor-embedded GHS02 and GHS05
icons found in that document.
"""

import io
from pathlib import Path

import cv2
import imagehash
import numpy as np
from PIL import Image

import fitz

TEMPLATES_DIR = Path(__file__).parent / "pictogram_templates"
GHS_CODES = ["GHS01", "GHS02", "GHS03", "GHS04", "GHS05", "GHS06", "GHS07", "GHS08", "GHS09"]

# Calibrated against real examples extracted from an actual SDS document:
# GHS pictograms measured white=0.77-0.80, red=0.14; a PPE icon (different
# icon standard) measured white=0.33, red=0.00; a transport/DOT hazard
# class label (visually similar diamond, different meaning) measured
# white=0.50, red=0.42. These bounds cleanly separate all three.
WHITE_MIN = 0.60
RED_MIN = 0.05
RED_MAX = 0.30

# Perceptual-hash distance threshold for "close enough" to a template.
# phash is 64 bits; 0 is a pixel-identical match after normalization.
# Real embedded icons compared exactly equal to their reference (distance
# 0-2) in testing -- this threshold leaves headroom for compression
# artifacts while still being strict enough to reject a different icon.
HASH_MAX_DISTANCE = 10

_template_hashes = None


def _load_templates() -> dict:
    global _template_hashes
    if _template_hashes is not None:
        return _template_hashes
    hashes = {}
    for code in GHS_CODES:
        path = TEMPLATES_DIR / f"{code}.png"
        if path.exists():
            hashes[code] = imagehash.phash(Image.open(path).convert("RGB"))
    _template_hashes = hashes
    return hashes


def _color_ratios(pil_image: Image.Image) -> tuple:
    arr = np.array(pil_image.convert("RGB"))
    r, g, b = arr[:, :, 0].astype(int), arr[:, :, 1].astype(int), arr[:, :, 2].astype(int)
    total = arr.shape[0] * arr.shape[1]
    if total == 0:
        return 0.0, 0.0
    white = np.sum((r > 200) & (g > 200) & (b > 200)) / total
    red = np.sum((r > 150) & (g < 90) & (b < 90)) / total
    return float(white), float(red)


def _looks_like_ghs_pictogram(pil_image: Image.Image) -> bool:
    """Style filter: is this a white-background, red-diamond-bordered icon
    (the real GHS pictogram style), not some other red/colored graphic?
    """
    width, height = pil_image.size
    if width < 20 or height < 20:
        return False
    aspect = width / height
    if aspect < 0.5 or aspect > 2.0:
        return False
    white, red = _color_ratios(pil_image)
    return white >= WHITE_MIN and RED_MIN <= red <= RED_MAX


def match_icon(image_bytes: bytes) -> str | None:
    """Compare one candidate icon image against the 9 GHS templates.
    Returns a code (e.g. "GHS05") if it's a confident match, else None --
    never a guess.
    """
    try:
        pil_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        return None

    if not _looks_like_ghs_pictogram(pil_image):
        return None

    templates = _load_templates()
    if not templates:
        return None

    candidate_hash = imagehash.phash(pil_image)
    best_code, best_distance = None, None
    for code, template_hash in templates.items():
        distance = candidate_hash - template_hash
        if best_distance is None or distance < best_distance:
            best_code, best_distance = code, distance

    if best_distance is not None and best_distance <= HASH_MAX_DISTANCE:
        return best_code
    return None


def match_embedded_images(file_bytes: bytes, filename: str, max_pages: int = 3) -> list:
    """Tier 1: extract embedded raster images from the first `max_pages`
    pages of a PDF and match each against the GHS templates. Returns a
    sorted list of matched codes (possibly empty). Non-PDF files and any
    extraction error return an empty list -- this tier fails soft so the
    caller can move on to the next tier.
    """
    if not filename.lower().endswith(".pdf"):
        return []

    found = set()
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        for page_index in range(min(max_pages, doc.page_count)):
            page = doc[page_index]
            for img in page.get_images(full=True):
                xref = img[0]
                try:
                    extracted = doc.extract_image(xref)
                except Exception:
                    continue
                code = match_icon(extracted["image"])
                if code:
                    found.add(code)
        doc.close()
    except Exception:
        return []

    return sorted(found)


def match_page_regions(page_image_bytes: bytes) -> list:
    """Tier 2: find red-bordered diamond-shaped regions on a rendered page
    image (OpenCV contour detection), crop each, and match against the GHS
    templates. Returns a sorted list of matched codes (possibly empty).
    Used when a page has no matching embedded images -- e.g. the icon was
    drawn directly into the page content instead of embedded separately.
    """
    try:
        arr = np.frombuffer(page_image_bytes, dtype=np.uint8)
        image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if image is None:
            return []
    except Exception:
        return []

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    # Red wraps around the hue circle -- two ranges needed.
    lower_red_1 = np.array([0, 100, 80])
    upper_red_1 = np.array([10, 255, 255])
    lower_red_2 = np.array([170, 100, 80])
    upper_red_2 = np.array([180, 255, 255])
    mask = cv2.inRange(hsv, lower_red_1, upper_red_1) | cv2.inRange(hsv, lower_red_2, upper_red_2)

    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    found = set()
    h, w = image.shape[:2]
    page_area = h * w
    for contour in contours:
        area = cv2.contourArea(contour)
        # A pictogram should be a meaningfully sized shape, not a stray
        # pixel or a full-page border -- reject anything too small or
        # implausibly large.
        if area < page_area * 0.0005 or area > page_area * 0.25:
            continue
        x, y, cw, ch = cv2.boundingRect(contour)
        aspect = cw / ch if ch else 0
        if aspect < 0.7 or aspect > 1.4:
            continue
        pad = int(max(cw, ch) * 0.05)
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(w, x + cw + pad), min(h, y + ch + pad)
        crop = image[y0:y1, x0:x1]
        ok, buf = cv2.imencode(".png", crop)
        if not ok:
            continue
        code = match_icon(buf.tobytes())
        if code:
            found.add(code)

    return sorted(found)
