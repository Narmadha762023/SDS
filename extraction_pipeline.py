"""Shared SDS extraction pipeline.

Used by bulk_upload.py (and any future caller) to turn a document's raw
bytes into structured, saved data. No Streamlit dependency -- every
function here is plain Python, safe to call from a worker thread.

Two independent processing methods feed the same AI extraction step:

  PDF Extraction:  PDF -> pdf_extractor.extract_text -> ai_extractor
  OCR:             PDF/Image -> ocr.extract_text_and_pictograms -> ai_extractor

The caller picks one -- they never run automatically or fall back into
one another. Either way, pictogram-icon detection also runs -- SDS hazard
pictograms are almost always graphic icons, not text, so plain text
extraction can't see them. For PDF Extraction, that's a separate vision
pass (pictogram_detector.py), since that pipeline never renders page
images at all. For OCR, it's folded into the same vision call that reads
the page's text (ocr.py), since OCR already renders and sends those
images -- asking two separate questions about the same image in two
separate calls would upload it twice for no reason.

AI-extracted fields are returned as plain text (never a forced choice,
since the value must reflect exactly what's in the document). Review
Frequency and Next Review Due are pre-filled only when the document
itself states a review cadence (e.g. "review: Every 3 Years") -- the due
date is then computed deterministically from that stated interval plus
the revision/issue date, never left to the model to invent.
"""

import hashlib
import json
import os
import re
from datetime import date

from dateutil import parser as dateparser
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv

import ai_extractor
import ocr
import pictogram_detector
import pdf_extractor
import regex_extractor
import template_matcher
from image_utils import pdf_to_page_images
from storage_paths import RECORDS_FILE, RAW_TEXT_DIR, load_existing_records
from usage_tracker import UsageTracker

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
AI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OCR_MODEL = os.getenv("OPENAI_OCR_MODEL", "gpt-4o-mini")

# GHS pictograms, in a fixed display order.
PICTOGRAM_DEFS = [
    {"code": "GHS02", "label": "Flammable", "icon": "🔥",
     "synonyms": ["flame", "flammable"]},
    {"code": "GHS03", "label": "Oxidizer", "icon": "🟠",
     "synonyms": ["oxidiz", "flame over circle"]},
    {"code": "GHS01", "label": "Explosive", "icon": "💥",
     "synonyms": ["explod", "exploding bomb"]},
    {"code": "GHS04", "label": "Gas Under Pressure", "icon": "💨",
     "synonyms": ["gas cylinder", "gas under pressure", "compressed gas"]},
    {"code": "GHS05", "label": "Corrosive", "icon": "🧪",
     "synonyms": ["corrosion", "corrosive"]},
    {"code": "GHS06", "label": "Toxic", "icon": "☠️",
     "synonyms": ["skull", "toxic", "poison"]},
    {"code": "GHS08", "label": "Health Hazard", "icon": "🫁",
     "synonyms": ["health hazard", "silhouette"]},
    {"code": "GHS07", "label": "Irritant", "icon": "❗",
     "synonyms": ["exclamation", "irritant"]},
    {"code": "GHS09", "label": "Environmental", "icon": "🌳",
     "synonyms": ["environment", "aquatic", "fish"]},
]

# Official GHS Hazard Statement -> pictogram assignments (UN GHS Annex 1),
# limited to codes commonly seen in industrial/chemical SDS documents. This
# is a fixed regulatory lookup, applied deterministically in Python -- the
# AI only has to extract which H-codes literally appear in the text, never
# to apply the mapping itself.
H_CODE_TO_PICTOGRAMS = {
    "H200": ["GHS01"], "H201": ["GHS01"], "H202": ["GHS01"], "H203": ["GHS01"],
    "H204": ["GHS01"], "H205": ["GHS01"],
    "H220": ["GHS02"], "H221": ["GHS02"], "H222": ["GHS02"], "H223": ["GHS02"],
    "H224": ["GHS02"], "H225": ["GHS02"], "H226": ["GHS02"], "H228": ["GHS02"],
    "H240": ["GHS01"], "H241": ["GHS01", "GHS02"], "H242": ["GHS02"],
    "H250": ["GHS02"], "H251": ["GHS02"], "H252": ["GHS02"],
    "H260": ["GHS02"], "H261": ["GHS02"],
    "H270": ["GHS03"], "H271": ["GHS03"], "H272": ["GHS03"],
    "H280": ["GHS04"], "H281": ["GHS04"],
    "H290": ["GHS05"],
    "H300": ["GHS06"], "H301": ["GHS06"],
    "H302": ["GHS07"],
    "H304": ["GHS08"],
    "H310": ["GHS06"], "H311": ["GHS06"],
    "H312": ["GHS07"],
    "H314": ["GHS05"],
    "H315": ["GHS07"], "H317": ["GHS07"],
    "H318": ["GHS05"],
    "H319": ["GHS07"],
    "H330": ["GHS06"], "H331": ["GHS06"],
    "H332": ["GHS07"],
    "H334": ["GHS08"],
    "H335": ["GHS07"], "H336": ["GHS07"],
    "H340": ["GHS08"], "H341": ["GHS08"],
    "H350": ["GHS08"], "H351": ["GHS08"],
    "H360": ["GHS08"], "H361": ["GHS08"], "H362": ["GHS08"],
    "H370": ["GHS08"], "H371": ["GHS08"], "H372": ["GHS08"], "H373": ["GHS08"],
    "H400": ["GHS09"], "H410": ["GHS09"], "H411": ["GHS09"],
    "H412": ["GHS09"], "H413": ["GHS09"],
}

REVIEW_FREQUENCIES = ["Annually", "Every 2 Years", "Every 3 Years", "Custom"]
SITE_OPTIONS = ["All Sites", "Site A", "Site B", "Site C"]


def parse_review_interval_years(text: str):
    """Pull a whole-number year interval out of a stated review cadence.

    Only returns a value when the document's own wording clearly states an
    interval (e.g. "Every 3 Years", "Annually"); returns None otherwise so
    the caller never has to guess.
    """
    if not text:
        return None
    lowered = text.lower()
    if re.search(r"\bannual", lowered) or re.search(r"\b1\s*year", lowered):
        return 1
    match = re.search(r"(\d+)\s*year", lowered)
    if match:
        return int(match.group(1))
    return None


def parse_version_from_filename(filename: str):
    """Pull a version number out of the uploaded filename as a fallback.

    Only used when the document text itself has no version -- many SDS
    files are named with their version (e.g. "... - v3.0 (2023-02-15).pdf"),
    so this reads something that's actually present, not invented.
    """
    if not filename:
        return None
    match = re.search(r"\bv(\d+(?:\.\d+)*)\b", filename, re.IGNORECASE)
    return match.group(1) if match else None


def match_pictograms(extracted: list, hazard_codes: list = None) -> set:
    """Map AI-returned pictogram strings and hazard statement (H-)codes onto
    the fixed GHS01-09 codes. H-codes are resolved via the deterministic
    H_CODE_TO_PICTOGRAMS lookup, not left to the model to interpret."""
    selected = set()
    for item in extracted or []:
        text = str(item).strip().lower()
        for pic in PICTOGRAM_DEFS:
            terms = [pic["code"].lower(), pic["label"].lower(), *pic["synonyms"]]
            if any(term in text for term in terms):
                selected.add(pic["code"])
                break
    for code in hazard_codes or []:
        normalized = re.sub(r"\s+", "", str(code)).upper()
        selected.update(H_CODE_TO_PICTOGRAMS.get(normalized, []))
    return selected


def parse_date_safe(raw: str):
    """Best-effort parse of an AI-extracted date string. None if unparseable."""
    if not raw or not str(raw).strip():
        return None
    try:
        return dateparser.parse(str(raw), fuzzy=True).date()
    except (ValueError, OverflowError, TypeError):
        return None


PICTOGRAM_DETECTION_MAX_PAGES = 3


def _detect_pictogram_icons(file_bytes: bytes, filename: str, tracker=None) -> list:
    """Three-tier pictogram icon detection, cheapest/most-certain first:

    1. Template match against embedded PDF images (template_matcher) --
       deterministic, zero AI cost. Works when the SDS software embedded
       each pictogram as its own image object (common, but not universal).
    2. Template match against OpenCV-found red-diamond regions on the
       rendered page (template_matcher) -- still zero AI cost, covers
       pictograms drawn directly into the page instead of embedded.
    3. AI vision fallback (pictogram_detector) -- only reached if both
       non-AI tiers found nothing, e.g. a scanned page or a rendering
       style neither tier recognizes.

    A failure anywhere degrades to trying the next tier (or "none
    detected" for the AI tier) rather than raising -- one broken step
    should never take down an otherwise-successful extraction.
    """
    try:
        found = template_matcher.match_embedded_images(
            file_bytes, filename, max_pages=PICTOGRAM_DETECTION_MAX_PAGES
        )
        if found:
            return found
    except Exception:
        pass

    try:
        page_images = pdf_to_page_images(file_bytes)[:PICTOGRAM_DETECTION_MAX_PAGES]
        found = set()
        for page_image in page_images:
            found.update(template_matcher.match_page_regions(page_image))
        if found:
            return sorted(found)
    except Exception:
        pass

    try:
        return pictogram_detector.detect_pictograms(
            file_bytes, filename, OPENAI_API_KEY, OCR_MODEL,
            max_pages=PICTOGRAM_DETECTION_MAX_PAGES, tracker=tracker,
        )
    except Exception:
        return []


def compute_derived_fields(fields: dict, filename: str, detected_pictogram_codes: list) -> dict:
    """Pure post-processing on top of the AI's raw field extraction:
    resolving pictograms (text/H-code match + vision-detected icons), the
    filename version fallback, date parsing, and the review-cadence
    computation.
    """
    ghs_selected = match_pictograms(
        fields["ghs_hazard_pictograms"], fields.get("hazard_statement_codes")
    )
    ghs_selected.update(detected_pictogram_codes)
    ghs_labels = [
        f"{pic['code']} - {pic['label']}"
        for pic in PICTOGRAM_DEFS
        if pic["code"] in ghs_selected
    ]

    version = fields["version"]
    version_source = "document" if version else ""
    if not version:
        filename_version = parse_version_from_filename(filename)
        if filename_version:
            version = filename_version
            version_source = "filename"

    revision_date_parsed = parse_date_safe(fields["revision_date"])
    issue_date_parsed = parse_date_safe(fields["issue_date"])

    # Review Frequency / Next Review Due default to the plain baseline, then
    # get pre-filled only if this document explicitly states a review
    # interval.
    review_frequency = REVIEW_FREQUENCIES[0]
    next_review_due = date.today()
    years = parse_review_interval_years(fields["review_frequency_stated"])
    if years is not None:
        review_frequency = {
            1: "Annually", 2: "Every 2 Years", 3: "Every 3 Years",
        }.get(years, "Custom")
        base_date = revision_date_parsed or issue_date_parsed
        if base_date is not None:
            next_review_due = base_date + relativedelta(years=years)

    return {
        "ghs_selected_codes": ghs_selected,
        "ghs_hazard_pictograms": ghs_labels,
        "version": version,
        "version_source": version_source,
        "revision_date_parsed": revision_date_parsed,
        "issue_date_parsed": issue_date_parsed,
        "review_frequency": review_frequency,
        "review_frequency_raw": fields["review_frequency_stated"],
        "next_review_due": next_review_due,
    }


KEYWORD_STOPWORDS = {
    "and", "or", "the", "of", "in", "for", "with", "a", "an", "is", "this",
    "not", "no", "to", "on", "as", "by", "at", "from",
    "category",  # structural word from category strings ("... Category 2"), not a descriptive tag
    "none",      # a literal "NONE" signal_word means "not applicable", not a hazard descriptor
}


def generate_keywords(fields: dict, derived: dict, raw_text: str) -> list:
    """SDS-Extraction-Contract.pdf v2's `keywords` field: 5-15 lowercased,
    de-duplicated search tags -- product name & synonyms, hazard words,
    physical state, and (when Section 1 states one) recommended use.
    Deliberately NOT sent through the AI: every term here is re-surfacing
    something already extracted elsewhere (product name, synonyms, the
    pictogram labels already resolved in compute_derived_fields, a plain
    regex match on Section 1), so a separate model call would just be
    spending tokens to restate data this function already has for free.

    Chemical-family classification (the contract's other keyword source,
    e.g. "aromatic hydrocarbon") is deliberately NOT attempted here --
    that's a real chemistry classification judgment, not something
    findable by pattern in the document text, and guessing it would be
    exactly the kind of invented value this app avoids everywhere else.
    """
    terms = []

    name = fields.get("product_chemical_name") or ""
    if name:
        terms.append(name.lower())
        terms.extend(re.split(r"[^a-z0-9]+", name.lower()))

    for synonym in fields.get("synonyms") or []:
        if synonym:
            terms.append(synonym.lower())

    for label in derived.get("ghs_hazard_pictograms") or []:
        if " - " in label:
            terms.append(label.split(" - ", 1)[1].lower())

    if fields.get("signal_word"):
        terms.append(fields["signal_word"].lower())

    if fields.get("physical_state"):
        terms.append(fields["physical_state"].lower())

    category = fields.get("category") or ""
    terms.extend(t for t in re.split(r"[^a-z0-9]+", category.lower()) if not t.isdigit())

    recommended_use = regex_extractor.extract_recommended_use(raw_text)
    if recommended_use:
        terms.append(recommended_use.lower())

    seen = set()
    keywords = []
    for term in terms:
        term = term.strip()
        if len(term) < 3 or term in KEYWORD_STOPWORDS or term in seen:
            continue
        seen.add(term)
        keywords.append(term)
        if len(keywords) >= 15:
            break

    return keywords


def _apply_regex_extraction(raw_text: str) -> dict:
    """Tier-1 field extraction: fixed-format values found by pattern
    matching, zero AI tokens (see regex_extractor.py's module docstring
    for the accuracy rationale and per-field hit rates this was validated
    against). Returns only the keys it actually found a confident value
    for -- a key that's missing here means "regex didn't find it for this
    document", not "this document has no value"; the caller asks the AI
    for whichever keys are missing, exactly as if this function didn't
    run at all.
    """
    sections = regex_extractor.split_sections(raw_text)
    found = {}

    def _set(key, value):
        if value:
            found[key] = value

    _set("version", regex_extractor.extract_version(raw_text))
    _set("revision_date", regex_extractor.extract_revision_date_raw(raw_text))
    _set("issue_date", regex_extractor.extract_issue_date_raw(raw_text))
    _set("physical_state", regex_extractor.extract_physical_state(raw_text))
    _set("product_code", regex_extractor.extract_product_code(raw_text))
    _set("synonyms", regex_extractor.extract_synonyms(raw_text))
    _set("regulation_basis", regex_extractor.extract_regulation_basis(raw_text))
    _set("signal_word", regex_extractor.extract_signal_word(raw_text))
    _set("flash_point", regex_extractor.extract_flash_point(sections))
    _set("nfpa", regex_extractor.extract_nfpa(raw_text))
    _set("transport", regex_extractor.extract_transport(sections))
    _set("rcra_waste_code", regex_extractor.extract_rcra_waste_code(sections))
    _set("ingredients", regex_extractor.extract_ingredients(sections))

    return found


def extract_and_derive(method: str, file_bytes: bytes, filename: str):
    """Run the full extraction pipeline for one document: text/OCR -> AI
    fields -> pictogram icon detection -> derived fields. No Streamlit
    calls, so this is safe to run inside a worker thread for concurrent
    (bulk) processing. Returns (fields, derived), or (None, None) if no
    text could be extracted.

    OCR mode gets pictogram-icon detection "for free" as part of reading
    the page images (see ocr.extract_text_and_pictograms) instead of a
    separate pass -- PDF Extraction mode still needs its own pictogram
    pass since it never renders page images at all.

    `derived["token_usage"]` holds this document's total token usage and
    estimated cost across every AI call made for it (field extraction +
    whichever pictogram/OCR calls ran) -- see usage_tracker.py.
    """
    tracker = UsageTracker()

    if method == "PDF Extraction":
        raw_text = pdf_extractor.extract_text(file_bytes)
        if not raw_text.strip():
            return None, None
        detected_pictogram_codes = _detect_pictogram_icons(file_bytes, filename, tracker)
    else:
        raw_text, detected_pictogram_codes = ocr.extract_text_and_pictograms(
            file_bytes, filename, OPENAI_API_KEY, OCR_MODEL, tracker=tracker
        )
        if not raw_text.strip():
            return None, None

    regex_fields = _apply_regex_extraction(raw_text)
    fields = ai_extractor.extract_fields(
        raw_text, OPENAI_API_KEY, AI_MODEL, tracker=tracker,
        skip_fields=set(regex_fields.keys()),
    )
    fields.update(regex_fields)

    derived = compute_derived_fields(fields, filename, detected_pictogram_codes)
    derived["token_usage"] = tracker.summary()
    # Kept on `derived` (not written to disk here) so a caller running this
    # inside a worker thread decides when/whether to persist it -- see
    # save_raw_text(). This is the same text already sent to the AI; saving
    # it means it doesn't have to be re-extracted to support full-text
    # search later.
    derived["raw_text"] = raw_text
    derived["keywords"] = generate_keywords(fields, derived, raw_text)
    return fields, derived


def save_raw_text(record_id: str, raw_text: str) -> None:
    """Persist one document's extracted text for later full-text search
    (see repository.py). Stored one file per record, separate from
    RECORDS_FILE, so the (frequently read-modify-written) main record
    store stays small regardless of how much raw text accumulates.
    """
    (RAW_TEXT_DIR / f"{record_id}.txt").write_text(raw_text, encoding="utf-8")


def compute_file_hash(file_bytes: bytes) -> str:
    """SHA-256 of the raw file bytes -- an exact-content fingerprint, not a
    filename comparison, so a renamed copy of the same file is still
    caught, and two different documents that happen to share a filename
    never get falsely flagged as duplicates.
    """
    return hashlib.sha256(file_bytes).hexdigest()


def load_existing_hashes() -> dict:
    """Map of file_sha256 -> a short summary of the existing record that
    hash belongs to (product name, when it was saved), for every saved
    record that has a hash on file. Records saved before this feature was
    added have no `file_sha256` and simply can't be matched against --
    they're not retroactively hashed.
    """
    existing = {}
    for record in load_existing_records():
        file_hash = record.get("file_sha256")
        if file_hash:
            existing[file_hash] = {
                "product_chemical_name": record.get("product_chemical_name") or "(unnamed)",
                "saved_at": record.get("saved_at", ""),
            }
    return existing


def append_record_to_store(record: dict) -> None:
    """Append one record dict to the JSON store.

    Bulk processing runs extraction concurrently across worker threads, but
    every call to this function happens back on the main thread as each
    result comes in -- so this read-modify-write is never racing against
    itself.
    """
    records = load_existing_records()
    records.append(record)
    RECORDS_FILE.write_text(json.dumps(records, indent=2), encoding="utf-8")
