"""SDS Repository page.

A quick-lookup view for saved Safety Data Sheets, built for a front-line
worker who needs to find the critical parts of an SDS fast, not re-read the
whole document. Each saved record shows in a searchable list; clicking a
record pops up a summary card with the critical fields (product, emergency
contact, hazards, first aid, PPE, storage) and a button to open the full
original PDF.
"""

import re

import streamlit as st

import pdf_render
import pdf_word_index
from storage_paths import UPLOADS_DIR, RAW_TEXT_DIR, load_existing_records

PAGE_MARKER_RE = re.compile(r"--- Page (\d+) ---")

SNIPPET_RADIUS = 40  # characters of context on either side of a match -- kept short so the snippet reads as one line

SUMMARY_FIELDS = [
    ("Signal Word", "signal_word"),
    ("Emergency Contact Phone", "emergency_contact_phone"),
    ("GHS Hazard Pictograms", "ghs_hazard_pictograms"),
    ("Safety Hazards (Physical & Health)", "safety_hazards"),
    ("First Aid Measures", "first_aid_measures"),
    ("Personal Protection", "personal_protection"),
    ("Storage", "storage"),
    ("CAS Number", "cas_number"),
    ("Product Code(s)", "product_code"),
    ("Synonyms", "synonyms"),
    ("Physical State", "physical_state"),
    ("Flash Point", "flash_point"),
    ("Category", "category"),
    ("Regulation Basis", "regulation_basis"),
    ("RCRA Waste Code", "rcra_waste_code"),
    ("Version", "version"),
    ("Revision Date", "revision_date"),
    ("Issue Date", "issue_date"),
    ("Keywords", "keywords"),
]


def _search_raw_text_fallback(record_id: str, needle: str):
    """Page-only fallback tier: plain substring search over the flat saved
    text (extraction_pipeline.save_raw_text). Used when there's no
    position-aware word index for this record -- an OCR/scanned document
    (no real PDF text layer to pull word positions from), or a record
    saved before pdf_word_index.py existed. Returns (snippet, page, bbox)
    with bbox always None, since a flat text offset carries no on-page
    position -- only the page-marker-derived page number.
    """
    path = RAW_TEXT_DIR / f"{record_id}.txt"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    idx = text.lower().find(needle)
    if idx == -1:
        return None
    start = max(0, idx - SNIPPET_RADIUS)
    end = min(len(text), idx + len(needle) + SNIPPET_RADIUS)

    before = text[start:idx].replace("\n", " ")
    match = text[idx:idx + len(needle)]  # original casing, not the lowered needle
    after = text[idx + len(needle):end].replace("\n", " ")
    snippet = f"{before}**{match}**{after}".strip()

    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    snippet = f"{prefix}{snippet}{suffix}"

    page = 1
    for m in PAGE_MARKER_RE.finditer(text):
        if m.start() > idx:
            break
        page = int(m.group(1))

    return snippet, page, None


def _search_document(record_id: str, needle: str):
    """Search one document for `needle`, position-aware tier first.
    Returns (snippet, page, bbox) or None. `bbox` is a real bounding box
    (see pdf_word_index.find_match) when available -- lets the matched
    phrase be highlighted exactly, not just the right page shown. Falls
    back to the flat-text, page-only tier (bbox always None) for
    documents with no word index.
    """
    match = pdf_word_index.find_match(record_id, needle)
    if match:
        page, bbox, snippet = match
        return snippet, page, bbox
    return _search_raw_text_fallback(record_id, needle)


def _field_value(record: dict, key: str) -> str:
    value = record.get(key)
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return value or ""


def _format_nfpa(nfpa: dict) -> str:
    if not nfpa:
        return ""
    return (
        f"Health {nfpa.get('health', '—')} / "
        f"Flammability {nfpa.get('flammability', '—')} / "
        f"Instability {nfpa.get('instability', '—')}"
    )


def _format_transport(transport: dict) -> str:
    if not transport:
        return ""
    parts = [
        transport.get("un_no"),
        transport.get("shipping_name"),
        f"Class {transport.get('hazard_class')}" if transport.get("hazard_class") else None,
        f"PG {transport.get('packing_group')}" if transport.get("packing_group") else None,
    ]
    return " · ".join(p for p in parts if p)


def _show_matched_page(record: dict, page: int, bbox: dict) -> None:
    """Renders `page` of the original PDF as a single clean image (see
    pdf_render.py) -- not the browser's native PDF viewer, which brings
    its own toolbar/controls and gets cluttered with more than one result
    open. When `bbox` is available, the matched phrase is highlighted
    directly on the page, same as a real PDF annotation.
    """
    stored_name = record.get("stored_document", "")
    doc_path = UPLOADS_DIR / stored_name if stored_name else None
    if not doc_path or not doc_path.exists():
        st.error("The original document file is missing from uploads/.")
        return
    png_bytes = pdf_render.render_page(doc_path.read_bytes(), page, bbox)
    st.image(png_bytes, caption=f"Page {page}", use_container_width=True)


@st.dialog("SDS Summary", width="large")
def _show_summary(record: dict) -> None:
    st.subheader(record.get("product_chemical_name") or "(unnamed product)")
    st.caption(record.get("manufacturer_supplier") or "")

    for label, key in SUMMARY_FIELDS:
        value = _field_value(record, key)
        st.markdown(f"**{label}**")
        st.write(value if value else "—")

    st.markdown("**NFPA Rating**")
    st.write(_format_nfpa(record.get("nfpa")) or "—")

    st.markdown("**Transport Information**")
    st.write(_format_transport(record.get("transport")) or "—")

    ingredients = record.get("ingredients") or []
    if ingredients:
        st.markdown("**Ingredients**")
        st.table([
            {
                "Name": ing.get("name", ""),
                "CAS Number": ing.get("cas_number", ""),
                "Concentration": ing.get("concentration", ""),
            }
            for ing in ingredients
        ])

    hazard_statements = record.get("hazard_statements") or []
    if hazard_statements:
        st.markdown("**Hazard Statements**")
        st.table([
            {
                "Code": h.get("code") or "—",
                "Statement": h.get("text", ""),
            }
            for h in hazard_statements
        ])

    st.divider()
    stored_name = record.get("stored_document", "")
    doc_path = UPLOADS_DIR / stored_name if stored_name else None
    if doc_path and doc_path.exists():
        st.download_button(
            "Open Full SDS",
            data=doc_path.read_bytes(),
            file_name=record.get("original_filename", stored_name),
            mime="application/pdf",
            type="primary",
            use_container_width=True,
        )
    else:
        st.error("The original document file is missing from uploads/.")


def render() -> None:
    st.title("SDS Repository")
    st.caption(
        "Quick lookup for saved Safety Data Sheets. Click a record to see "
        "the critical details and open the full document."
    )

    records = load_existing_records()
    if not records:
        st.info("No SDS records saved yet. Use the Bulk Upload page to add some.")
        return

    search = st.text_input(
        "Search",
        placeholder="Search by product name, manufacturer, CAS number, keyword, or anything in the document...",
        label_visibility="collapsed",
    )

    snippets = {}  # record id -> (snippet, page, bbox), text-only matches
    if search.strip():
        needle = search.strip().lower()
        field_matches = []
        text_only_matches = []
        for r in records:
            if (
                needle in (r.get("product_chemical_name") or "").lower()
                or needle in (r.get("manufacturer_supplier") or "").lower()
                or needle in (r.get("cas_number") or "").lower()
                or any(needle in k for k in (r.get("keywords") or []))
            ):
                field_matches.append(r)
                continue
            match = _search_document(r["id"], needle)
            if match:
                snippets[r["id"]] = match
                text_only_matches.append(r)
        records = field_matches + text_only_matches

    st.caption(f"{len(records)} record(s)")
    if snippets:
        st.caption(
            f"{len(snippets)} matched only in the document text, not in the "
            f"listed fields -- click the snippet below to open that page."
        )
    st.divider()

    header = st.columns([3, 2, 2, 1])
    header[0].markdown("**Product / Chemical Name**")
    header[1].markdown("**Manufacturer**")
    header[2].markdown("**Pictograms**")
    header[3].markdown("")

    for record in records:
        row = st.columns([3, 2, 2, 1])
        row[0].write(record.get("product_chemical_name") or "(unnamed)")
        row[1].write(record.get("manufacturer_supplier") or "—")
        pictograms = record.get("ghs_hazard_pictograms") or []
        codes = ", ".join(p.split(" - ")[0] for p in pictograms) if pictograms else "—"
        row[2].write(codes)
        if row[3].button("View", key=f"view_{record['id']}", use_container_width=True):
            _show_summary(record)

        if record["id"] in snippets:
            snippet, page, bbox = snippets[record["id"]]
            if st.button(
                f"📄 \"{snippet}\"",
                key=f"snippet_{record['id']}",
                type="tertiary",
                use_container_width=True,
            ):
                # Only one preview open at a time -- toggling a new one
                # replaces whichever was open, instead of stacking several
                # full-page images on top of each other.
                currently_open = st.session_state.get("open_preview_id")
                st.session_state["open_preview_id"] = (
                    None if currently_open == record["id"] else record["id"]
                )
            if st.session_state.get("open_preview_id") == record["id"]:
                _show_matched_page(record, page, bbox)
