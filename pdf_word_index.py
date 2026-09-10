"""Position-aware search index: per-page word positions straight from the
PDF's own text layer, not a flattened text dump.

This exists because a flat "does this string appear in the extracted
text" search (the original raw_text approach) can tell you *which
document and roughly which page* matched, but not *where on the page* --
there's no way back from a character offset in joined text to a location
in the actual PDF. `pdfplumber`'s `page.extract_words()` gives each word's
bounding box (x0, top, x1, bottom, in PDF page-point coordinates), so a
match can be pinned to an exact rectangle, which `pdf_render.py` then uses
to draw a real highlight when showing that page.

Only meaningful for documents with a real PDF text layer -- i.e. "PDF
Extraction" method. A scanned/OCR document has no such layer to query
positions from (the only text that exists is the AI's own transcription,
which was never tied to on-page coordinates), so this index isn't built
for OCR-processed documents; search for those falls back to the
raw_text.py page-level tier instead (page jump, no highlight box).
"""

import io
import json

import pdfplumber

from storage_paths import WORD_INDEX_DIR


def build_word_index(file_bytes: bytes) -> list:
    """Returns a list of {"page": N, "text": <joined words with single
    spaces>, "words": [{"text", "x0", "top", "x1", "bottom"}, ...]} for
    each page. `text` plus `words` together let a caller search for a
    substring and map the match straight back to the word(s) -- and their
    bounding box -- it came from.
    """
    pages = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            words = page.extract_words()
            joined = " ".join(w["text"] for w in words)
            pages.append({"page": page_number, "text": joined, "words": words})
    return pages


def save_word_index(record_id: str, file_bytes: bytes) -> None:
    index = build_word_index(file_bytes)
    (WORD_INDEX_DIR / f"{record_id}.json").write_text(
        json.dumps(index), encoding="utf-8"
    )


def load_word_index(record_id: str):
    path = WORD_INDEX_DIR / f"{record_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def find_match(record_id: str, needle: str):
    """Search a record's word index for `needle` (case-insensitive).
    Returns (page_number, bbox, snippet) for the first match, or None if
    not found / no index exists for this record. `bbox` is
    {"x0", "top", "x1", "bottom"} in the page's own PDF point coordinates
    -- exactly what pdf_render.py needs to draw a highlight.
    """
    index = load_word_index(record_id)
    if not index:
        return None

    needle_lower = needle.lower()
    for page_entry in index:
        text = page_entry["text"]
        idx = text.lower().find(needle_lower)
        if idx == -1:
            continue

        words = page_entry["words"]
        # Map the character range [idx, idx+len(needle)) back to the word(s)
        # it spans, by walking the same " ".join() layout used to build
        # `text` -- each word's slice is [pos, pos+len(word)), followed by
        # a single space before the next word.
        pos = 0
        matched_words = []
        for w in words:
            w_start, w_end = pos, pos + len(w["text"])
            if w_end > idx and w_start < idx + len(needle):
                matched_words.append(w)
            pos = w_end + 1  # +1 for the joining space
            if pos > idx + len(needle):
                break

        if not matched_words:
            continue

        bbox = {
            "x0": min(w["x0"] for w in matched_words),
            "top": min(w["top"] for w in matched_words),
            "x1": max(w["x1"] for w in matched_words),
            "bottom": max(w["bottom"] for w in matched_words),
        }

        radius = 40
        start = max(0, idx - radius)
        end = min(len(text), idx + len(needle) + radius)
        before = text[start:idx]
        match = text[idx:idx + len(needle)]
        after = text[idx + len(needle):end]
        snippet = f"{'...' if start > 0 else ''}{before}**{match}**{after}{'...' if end < len(text) else ''}"

        return page_entry["page"], bbox, snippet

    return None
