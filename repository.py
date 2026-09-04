"""SDS Repository page.

A quick-lookup view for saved Safety Data Sheets, built for a front-line
worker who needs to find the critical parts of an SDS fast, not re-read the
whole document. Each saved record shows in a searchable list; clicking a
record pops up a summary card with the critical fields (product, emergency
contact, hazards, first aid, PPE, storage) and a button to open the full
original PDF.
"""

import json
from pathlib import Path

import streamlit as st

APP_DIR = Path(__file__).parent
UPLOADS_DIR = APP_DIR / "uploads"
RECORDS_FILE = APP_DIR / "response" / "sds_records.json"

SUMMARY_FIELDS = [
    ("Emergency Contact Phone", "emergency_contact_phone"),
    ("GHS Hazard Pictograms", "ghs_hazard_pictograms"),
    ("Safety Hazards (Physical & Health)", "safety_hazards"),
    ("First Aid Measures", "first_aid_measures"),
    ("Personal Protection", "personal_protection"),
    ("Storage", "storage"),
    ("CAS Number", "cas_number"),
    ("Physical State", "physical_state"),
    ("Category", "category"),
    ("Version", "version"),
    ("Revision Date", "revision_date"),
    ("Issue Date", "issue_date"),
]


def _load_records() -> list:
    if not RECORDS_FILE.exists():
        return []
    try:
        return json.loads(RECORDS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _field_value(record: dict, key: str) -> str:
    value = record.get(key)
    if isinstance(value, list):
        return ", ".join(value)
    return value or ""


@st.dialog("SDS Summary", width="large")
def _show_summary(record: dict) -> None:
    st.subheader(record.get("product_chemical_name") or "(unnamed product)")
    st.caption(record.get("manufacturer_supplier") or "")

    for label, key in SUMMARY_FIELDS:
        value = _field_value(record, key)
        st.markdown(f"**{label}**")
        st.write(value if value else "—")

    st.markdown("**AI Token Usage**")
    tokens = record.get("token_usage_total")
    cost = record.get("estimated_cost_usd")
    if tokens is None:
        st.write("— (saved before token tracking was added)")
    else:
        st.write(f"{tokens:,} tokens (~${cost:.4f} estimated)")

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

    records = _load_records()
    if not records:
        st.info("No SDS records saved yet. Use the Bulk Upload page to add some.")
        return

    search = st.text_input(
        "Search",
        placeholder="Search by product name, manufacturer, or CAS number...",
        label_visibility="collapsed",
    )
    if search.strip():
        needle = search.strip().lower()
        records = [
            r for r in records
            if needle in (r.get("product_chemical_name") or "").lower()
            or needle in (r.get("manufacturer_supplier") or "").lower()
            or needle in (r.get("cas_number") or "").lower()
        ]

    st.caption(f"{len(records)} record(s)")
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
