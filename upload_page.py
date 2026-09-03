"""Upload & Extract page.

Two independent, user-selected document processing pipelines feed the same
AI extraction + review/edit + save flow:

  PDF Extraction:  PDF -> pdf_extractor.extract_text -> ai_extractor -> form
  OCR:             PDF/Image -> ocr.extract_text_and_pictograms -> ai_extractor -> form

The two pipelines never run automatically or fall back into one another --
the user explicitly picks one on the left. Either way, pictogram-icon
detection also runs -- SDS hazard pictograms are almost always graphic
icons, not text, so plain text extraction can't see them. For PDF
Extraction, that's a separate vision pass (pictogram_detector.py), since
that pipeline never renders page images at all. For OCR, it's folded into
the same vision call that reads the page's text (ocr.py), since OCR
already renders and sends those images -- asking two separate questions
about the same image in two separate calls would upload it twice for no
reason. The right side holds the review form: empty until a document is
processed, then auto-filled from the AI's output.

The fields a front-line worker needs at a glance (emergency contact,
hazards, first aid, PPE, storage) stay prominent in the form; lower-priority
document metadata (CAS number, physical state, version, dates) is tucked
into a collapsed section so the review stays quick even though there are
more fields than a bare-minimum form -- this matters for a repository of
thousands of SDS documents, where per-document review time adds up.

AI-extracted fields are plain editable text (never a forced choice, since
the value must reflect exactly what's in the document). Applies To Site
and Review Owners are always user-chosen. Review Frequency and Next Review
Due are pre-filled only when the document itself states a review cadence
(e.g. "review: Every 3 Years") -- the due date is then computed
deterministically from that stated interval plus the revision/issue date,
never left to the model to invent -- and stay fully editable either way.
A summary recap is shown before saving.
"""

import json
import os
import re
import uuid
from datetime import date, datetime
from pathlib import Path

import streamlit as st
from dateutil import parser as dateparser
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv

import ai_extractor
import ocr
import pictogram_detector
import pdf_extractor

load_dotenv()

APP_DIR = Path(__file__).parent
UPLOADS_DIR = APP_DIR / "uploads"
DATA_DIR = APP_DIR / "data"
RECORDS_FILE = DATA_DIR / "sds_records.json"

UPLOADS_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
AI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OCR_MODEL = os.getenv("OPENAI_OCR_MODEL", "gpt-4o-mini")

# GHS pictograms, in the display order used by the review form (3x3 grid).
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
OWNER_OPTIONS = ["EHS Manager", "Plant Supervisor", "Lab Manager", "Compliance Officer"]

FIELD_DEFAULTS = {
    "field_product_chemical_name": "",
    "field_manufacturer_supplier": "",
    "field_emergency_contact_phone": "",
    "field_cas_number": "",
    "field_ghs_selected": set(),
    "field_safety_hazards": "",
    "field_first_aid_measures": "",
    "field_personal_protection": "",
    "field_storage": "",
    "field_physical_state": "",
    "field_category": "",
    "field_version": "",
    "field_version_source": "",
    "field_revision_date_raw": "",
    "field_revision_date_parsed": None,
    "field_issue_date_raw": "",
    "field_issue_date_parsed": None,
    "field_applies_to_site": SITE_OPTIONS[0],
    "field_review_owners": [],
    "field_review_frequency": REVIEW_FREQUENCIES[0],
    "field_review_frequency_raw": "",
    "field_next_review_due": date.today(),
}


def init_field_defaults() -> None:
    """Ensure every form field exists (blank) before any widget binds to it."""
    for key, default in FIELD_DEFAULTS.items():
        st.session_state.setdefault(key, default)


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


def selected_pictogram_labels() -> list:
    return [
        f"{pic['code']} - {pic['label']}"
        for pic in PICTOGRAM_DEFS
        if pic["code"] in st.session_state.field_ghs_selected
    ]


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


def reset_extraction_state():
    for key in list(st.session_state.keys()):
        if key.startswith("field_") or key in (
            "extracted", "raw_text", "processed_file_id", "saved_record_id",
        ):
            del st.session_state[key]


def _detect_pictogram_icons(file_bytes: bytes, filename: str) -> list:
    """Best-effort vision-based pictogram icon detection.

    A failure here (network, API) should not take down the rest of an
    otherwise-successful extraction, so it degrades to "none detected"
    rather than raising.
    """
    try:
        return pictogram_detector.detect_pictograms(
            file_bytes, filename, OPENAI_API_KEY, OCR_MODEL
        )
    except Exception:
        return []


def compute_derived_fields(fields: dict, filename: str, detected_pictogram_codes: list) -> dict:
    """Pure post-processing on top of the AI's raw field extraction:
    resolving pictograms (text/H-code match + vision-detected icons), the
    filename version fallback, date parsing, and the review-cadence
    computation. No Streamlit calls, no network calls -- shared by both the
    interactive single-upload flow and headless bulk processing.
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


def extract_and_derive(method: str, file_bytes: bytes, filename: str):
    """Headless version of the full extraction pipeline: text/OCR -> AI
    fields -> pictogram icon detection -> derived fields. No Streamlit
    calls, so this is safe to run inside a worker thread for bulk
    processing. Returns (fields, derived), or (None, None) if no text
    could be extracted.

    OCR mode gets pictogram-icon detection "for free" as part of reading
    the page images (see ocr.extract_text_and_pictograms) instead of a
    separate pass -- PDF Extraction mode still needs its own pictogram
    pass since it never renders page images at all.
    """
    if method == "PDF Extraction":
        raw_text = pdf_extractor.extract_text(file_bytes)
        if not raw_text.strip():
            return None, None
        detected_pictogram_codes = _detect_pictogram_icons(file_bytes, filename)
    else:
        raw_text, detected_pictogram_codes = ocr.extract_text_and_pictograms(
            file_bytes, filename, OPENAI_API_KEY, OCR_MODEL
        )
        if not raw_text.strip():
            return None, None

    fields = ai_extractor.extract_fields(raw_text, OPENAI_API_KEY, AI_MODEL)
    derived = compute_derived_fields(fields, filename, detected_pictogram_codes)
    return fields, derived


def run_extraction(method: str, uploaded_file) -> None:
    file_bytes = uploaded_file.getvalue()

    if not OPENAI_API_KEY:
        st.error(
            "OPENAI_API_KEY is not set. Add it to your .env file before "
            "running extraction."
        )
        return

    if method == "PDF Extraction":
        with st.status("Processing with PDF Extraction...", expanded=True) as status:
            st.write("Extracting text from PDF...")
            raw_text = pdf_extractor.extract_text(file_bytes)
            if not raw_text.strip():
                status.update(label="No text found", state="error")
                st.error(
                    "No selectable text was found in this PDF. It may be a "
                    "scanned document -- try the OCR option instead."
                )
                return
            st.write("Sending extracted text to AI...")
            fields = ai_extractor.extract_fields(raw_text, OPENAI_API_KEY, AI_MODEL)
            st.write("Scanning for hazard pictogram icons...")
            detected_pictogram_codes = _detect_pictogram_icons(file_bytes, uploaded_file.name)
            status.update(label="Extraction complete", state="complete")
    else:
        with st.status("Processing with OCR...", expanded=True) as status:
            st.write("Reading document and scanning for hazard pictogram icons...")
            raw_text, detected_pictogram_codes = ocr.extract_text_and_pictograms(
                file_bytes, uploaded_file.name, OPENAI_API_KEY, OCR_MODEL
            )
            if not raw_text.strip():
                status.update(label="No text found", state="error")
                st.error("OCR could not read any text from this document.")
                return
            st.write("Sending extracted text to AI...")
            fields = ai_extractor.extract_fields(raw_text, OPENAI_API_KEY, AI_MODEL)
            status.update(label="Extraction complete", state="complete")

    st.session_state.raw_text = raw_text
    st.session_state.extracted = fields

    derived = compute_derived_fields(fields, uploaded_file.name, detected_pictogram_codes)

    st.session_state.field_product_chemical_name = fields["product_chemical_name"]
    st.session_state.field_manufacturer_supplier = fields["manufacturer_supplier"]
    st.session_state.field_emergency_contact_phone = fields["emergency_contact_phone"]
    st.session_state.field_cas_number = fields["cas_number"]
    st.session_state.field_ghs_selected = derived["ghs_selected_codes"]
    st.session_state.field_safety_hazards = fields["safety_hazards"]
    st.session_state.field_first_aid_measures = fields["first_aid_measures"]
    st.session_state.field_personal_protection = fields["personal_protection"]
    st.session_state.field_storage = fields["storage"]
    st.session_state.field_physical_state = fields["physical_state"]
    st.session_state.field_category = fields["category"]
    st.session_state.field_version = derived["version"]
    st.session_state.field_version_source = derived["version_source"]
    st.session_state.field_revision_date_raw = fields["revision_date"]
    st.session_state.field_revision_date_parsed = derived["revision_date_parsed"]
    st.session_state.field_issue_date_raw = fields["issue_date"]
    st.session_state.field_issue_date_parsed = derived["issue_date_parsed"]
    st.session_state.field_review_frequency = derived["review_frequency"]
    st.session_state.field_review_frequency_raw = derived["review_frequency_raw"]
    st.session_state.field_next_review_due = derived["next_review_due"]

    st.session_state.pop("saved_record_id", None)


def append_record_to_store(record: dict) -> None:
    """Append one record dict to the JSON store.

    Bulk processing runs extraction concurrently across worker threads, but
    every call to this function happens back on the main thread as each
    result comes in -- so this read-modify-write is never racing against
    itself.
    """
    records = []
    if RECORDS_FILE.exists():
        try:
            records = json.loads(RECORDS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            records = []
    records.append(record)
    RECORDS_FILE.write_text(json.dumps(records, indent=2), encoding="utf-8")


def save_record(uploaded_file, method: str) -> str:
    """Save the original document and the reviewed record. Returns record id."""
    record_id = str(uuid.uuid4())
    original_name = uploaded_file.name
    ext = Path(original_name).suffix
    stored_filename = f"{record_id}{ext}"
    stored_path = UPLOADS_DIR / stored_filename
    stored_path.write_bytes(uploaded_file.getvalue())

    record = {
        "id": record_id,
        "product_chemical_name": st.session_state.field_product_chemical_name,
        "manufacturer_supplier": st.session_state.field_manufacturer_supplier,
        "emergency_contact_phone": st.session_state.field_emergency_contact_phone,
        "cas_number": st.session_state.field_cas_number,
        "ghs_hazard_pictograms": selected_pictogram_labels(),
        "safety_hazards": st.session_state.field_safety_hazards,
        "first_aid_measures": st.session_state.field_first_aid_measures,
        "personal_protection": st.session_state.field_personal_protection,
        "storage": st.session_state.field_storage,
        "physical_state": st.session_state.field_physical_state,
        "category": st.session_state.field_category,
        "version": st.session_state.field_version,
        "revision_date": str(st.session_state.field_revision_date_parsed or ""),
        "issue_date": str(st.session_state.field_issue_date_parsed or ""),
        "applies_to_site": st.session_state.field_applies_to_site,
        "review_owners": st.session_state.field_review_owners,
        "review_frequency": st.session_state.field_review_frequency,
        "next_review_due": str(st.session_state.field_next_review_due),
        "processing_method": method,
        "original_filename": original_name,
        "stored_document": stored_filename,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }

    append_record_to_store(record)
    return record_id


def render() -> None:
    st.title("SDS Upload & AI Extraction")

    init_field_defaults()

    if st.session_state.get("saved_record_id"):
        st.success(
            f"SDS saved successfully (record ID: {st.session_state.saved_record_id}). "
            f"Find it anytime in the SDS Repository page."
        )

    left, right = st.columns([2, 3], gap="large")

    with left:
        st.markdown("**Select Document Processing Method**")
        method = st.radio(
            "Select Document Processing Method:",
            ["PDF Extraction", "OCR"],
            label_visibility="collapsed",
            key="method",
            on_change=reset_extraction_state,
        )

        if method == "PDF Extraction":
            st.caption("For PDFs that already contain selectable / machine-readable text.")
            uploaded_file = st.file_uploader("Upload SDS PDF", type=["pdf"], key="uploader_pdf")
        else:
            st.caption("Reads scanned/image-based documents using OCR.")
            uploaded_file = st.file_uploader(
                "Upload SDS PDF/Image",
                type=["pdf", "png", "jpg", "jpeg"],
                key="uploader_ocr",
            )

        if uploaded_file is not None:
            file_id = f"{method}:{uploaded_file.name}:{uploaded_file.size}"
            if st.session_state.get("processed_file_id") != file_id:
                run_extraction(method, uploaded_file)
                st.session_state.processed_file_id = file_id

            if "raw_text" in st.session_state:
                with st.expander("View extracted document text"):
                    st.text_area(
                        "Extracted text", st.session_state.raw_text, height=200, disabled=True
                    )
        else:
            st.info("Upload a document to auto-fill the form on the right.")

    with right:
        st.subheader("SDS Details")
        if uploaded_file is None:
            st.caption("This form will auto-fill once a document is processed on the left.")
        else:
            st.caption("AI extracted the following information. Please review and edit before saving.")

        st.text_input(
            "Product / Chemical Name *",
            key="field_product_chemical_name",
            placeholder="e.g. Diesel Fuel",
        )

        c1, c2 = st.columns(2)
        with c1:
            st.text_input(
                "Manufacturer / Supplier",
                key="field_manufacturer_supplier",
                placeholder="e.g. Shell",
            )
        with c2:
            st.text_input(
                "Emergency Contact Phone",
                key="field_emergency_contact_phone",
                placeholder="e.g. CHEMTREC 1-800-424-9300",
            )

        st.markdown("**GHS Hazard Pictograms**")
        tile_cols = st.columns(3) + st.columns(3) + st.columns(3)
        picto_toggled = False
        for col, pic in zip(tile_cols, PICTOGRAM_DEFS):
            with col:
                selected = pic["code"] in st.session_state.field_ghs_selected
                if st.button(
                    pic["label"],
                    icon=pic["icon"],
                    key=f"picto_{pic['code']}",
                    type="primary" if selected else "secondary",
                    use_container_width=True,
                ):
                    if selected:
                        st.session_state.field_ghs_selected.discard(pic["code"])
                    else:
                        st.session_state.field_ghs_selected.add(pic["code"])
                    picto_toggled = True

        st.markdown("**Safety & Handling**")
        st.caption("The critical parts a front-line worker needs at a glance.")
        st.text_area(
            "Safety Hazards (physical & health)",
            key="field_safety_hazards",
            placeholder="e.g. Causes severe skin burns and eye damage. Harmful if inhaled.",
            height=80,
        )
        st.text_area(
            "First Aid Measures",
            key="field_first_aid_measures",
            placeholder="e.g. Eyes: rinse cautiously with water for several minutes...",
            height=80,
        )
        st.text_area(
            "Personal Protection",
            key="field_personal_protection",
            placeholder="e.g. Chemical-resistant gloves, safety goggles, face shield.",
            height=80,
        )
        st.text_area(
            "Storage",
            key="field_storage",
            placeholder="e.g. Store in a cool, dry, well-ventilated area away from...",
            height=80,
        )

        with st.expander("Document Details (CAS number, physical state, version, dates)"):
            c3, c4 = st.columns(2)
            with c3:
                st.text_input(
                    "CAS Number", key="field_cas_number", placeholder="e.g. 68334-30-5"
                )
            with c4:
                st.text_input(
                    "Physical State", key="field_physical_state", placeholder="e.g. Liquid"
                )

            c5, c6 = st.columns(2)
            with c5:
                st.text_input(
                    "Category", key="field_category", placeholder="e.g. Flammable Liquid"
                )
            with c6:
                st.text_input("Version", key="field_version", placeholder="e.g. 1")
                if st.session_state.field_version_source == "filename":
                    st.caption("Not found in document text -- taken from the filename.")

            c7, c8 = st.columns(2)
            with c7:
                st.date_input(
                    "Revision Date", key="field_revision_date_parsed", format="YYYY-MM-DD"
                )
                if st.session_state.field_revision_date_raw and not st.session_state.field_revision_date_parsed:
                    st.caption(f"AI extracted (unparsed): “{st.session_state.field_revision_date_raw}”")
            with c8:
                st.date_input("Issue Date", key="field_issue_date_parsed", format="YYYY-MM-DD")
                if st.session_state.field_issue_date_raw and not st.session_state.field_issue_date_parsed:
                    st.caption(f"AI extracted (unparsed): “{st.session_state.field_issue_date_raw}”")

        st.divider()
        st.markdown("**Review Settings**")
        st.caption(
            "Applies To Site and Review Owners are set by you. Review Frequency "
            "and Next Review Due are pre-filled only if the document states a "
            "review cadence -- edit either as needed."
        )

        st.selectbox("Applies To Site", SITE_OPTIONS, key="field_applies_to_site")

        st.multiselect("Review Owners", OWNER_OPTIONS, key="field_review_owners")
        if st.session_state.field_review_owners:
            st.markdown("Selected owners:")
            for owner in st.session_state.field_review_owners:
                st.markdown(f"- {owner}")
        st.caption("All owners receive review reminders. Defaults to the uploader when left empty.")

        c9, c10 = st.columns(2)
        with c9:
            st.selectbox(
                "Review Frequency", REVIEW_FREQUENCIES, key="field_review_frequency"
            )
            if st.session_state.field_review_frequency_raw:
                st.caption(f"AI found in document: “{st.session_state.field_review_frequency_raw}”")
        with c10:
            st.date_input("Next Review Due", key="field_next_review_due", format="YYYY-MM-DD")

    st.divider()
    st.subheader("Summary")
    st.caption("Final overview -- review everything below before saving.")

    summary_rows = [
        ("Product / Chemical Name", st.session_state.field_product_chemical_name),
        ("Manufacturer / Supplier", st.session_state.field_manufacturer_supplier),
        ("Emergency Contact Phone", st.session_state.field_emergency_contact_phone),
        ("GHS Hazard Pictograms", ", ".join(selected_pictogram_labels())),
        ("Safety Hazards", st.session_state.field_safety_hazards),
        ("First Aid Measures", st.session_state.field_first_aid_measures),
        ("Personal Protection", st.session_state.field_personal_protection),
        ("Storage", st.session_state.field_storage),
        ("CAS Number", st.session_state.field_cas_number),
        ("Physical State", st.session_state.field_physical_state),
        ("Category", st.session_state.field_category),
        ("Version", st.session_state.field_version),
        ("Revision Date", str(st.session_state.field_revision_date_parsed or "")),
        ("Issue Date", str(st.session_state.field_issue_date_parsed or "")),
        ("Applies To Site", st.session_state.field_applies_to_site),
        ("Review Owners", ", ".join(st.session_state.field_review_owners)),
        ("Review Frequency", st.session_state.field_review_frequency),
        ("Next Review Due", str(st.session_state.field_next_review_due)),
        ("SDS Document", uploaded_file.name if uploaded_file is not None else ""),
    ]

    sc1, sc2 = st.columns([1, 2])
    for label, value in summary_rows:
        sc1.markdown(f"**{label}**")
        sc2.write(value if value else "—")

    if st.button("Save SDS", type="primary"):
        errors = []
        if not st.session_state.field_product_chemical_name.strip():
            errors.append("Product / Chemical Name is required.")
        if uploaded_file is None:
            errors.append("SDS Document is required.")

        if errors:
            for e in errors:
                st.error(e)
        else:
            record_id = save_record(uploaded_file, method)
            st.session_state.saved_record_id = record_id
            st.rerun()

    # Rerun once, now that every widget on the page has been instantiated, so
    # a toggled pictogram tile's highlight updates immediately instead of
    # lagging one interaction behind.
    if picto_toggled:
        st.rerun()
