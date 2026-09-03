"""Bulk Upload page.

Runs the same extraction pipeline as the single-document Upload & Extract
page, but for many files at once, with no per-document review step -- each
document is extracted and saved directly, then can be spot-checked or
corrected later from the SDS Repository page. Since there's no human
reviewing each record before it's saved here, pictogram icon detection
still runs for every file (same as single upload) rather than being
skipped for speed -- otherwise bulk-saved records would be quietly less
complete than single-uploaded ones, with nothing catching the gap.

Extraction for multiple documents runs concurrently, bounded to a small
number at a time. Each document's AI calls mostly spend their time waiting
on a network response rather than doing local computation, so several can
be "in flight" together without needing more CPU -- like several checkout
registers open at once instead of one. The concurrency cap keeps this from
overwhelming the AI service or tripping its rate limits. Every file's saved
document and JSON record write still happens back on the main thread, one
at a time, so there's no risk of two threads corrupting the shared
data/sds_records.json file by writing to it at the same moment.

Built for batches of up to a few dozen files in one browser session. A
true bulk import of hundreds or thousands of documents would need a
background job that keeps running independently of this page (so a
browser refresh or laptop sleep doesn't lose progress), which is a bigger
piece of infrastructure than this page provides.
"""

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import streamlit as st

import upload_page as up

MAX_WORKERS = 5


def _build_bulk_record(filename: str, method: str, fields: dict, derived: dict) -> dict:
    record_id = str(uuid.uuid4())
    return {
        "id": record_id,
        "product_chemical_name": fields["product_chemical_name"],
        "manufacturer_supplier": fields["manufacturer_supplier"],
        "emergency_contact_phone": fields["emergency_contact_phone"],
        "cas_number": fields["cas_number"],
        "ghs_hazard_pictograms": derived["ghs_hazard_pictograms"],
        "safety_hazards": fields["safety_hazards"],
        "first_aid_measures": fields["first_aid_measures"],
        "personal_protection": fields["personal_protection"],
        "storage": fields["storage"],
        "physical_state": fields["physical_state"],
        "category": fields["category"],
        "version": derived["version"],
        "revision_date": str(derived["revision_date_parsed"] or ""),
        "issue_date": str(derived["issue_date_parsed"] or ""),
        # Bulk mode has no per-document review step, so these default the
        # same way a fresh single-upload form would start -- correct them
        # afterward from the SDS Repository page if a batch needs a
        # specific site/owner assigned.
        "applies_to_site": up.SITE_OPTIONS[0],
        "review_owners": [],
        "review_frequency": derived["review_frequency"],
        "next_review_due": str(derived["next_review_due"]),
        "processing_method": method,
        "original_filename": filename,
        "stored_document": f"{record_id}{Path(filename).suffix}",
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }


def render() -> None:
    st.title("Bulk Upload")
    st.caption(
        "Upload many SDS documents at once. Each is processed and saved "
        "automatically -- no per-document review step. Spot-check or "
        "correct individual records afterward from the SDS Repository page."
    )

    method = st.radio(
        "Processing Method (applies to every file in this batch)",
        ["PDF Extraction", "OCR"],
        horizontal=True,
        key="bulk_method",
    )
    if method == "PDF Extraction":
        st.caption("For PDFs that already contain selectable / machine-readable text.")
        file_types = ["pdf"]
    else:
        st.caption("Reads scanned/image-based documents using OCR.")
        file_types = ["pdf", "png", "jpg", "jpeg"]

    uploaded_files = st.file_uploader(
        "Upload SDS documents",
        type=file_types,
        accept_multiple_files=True,
        key="bulk_uploader",
    )

    if not uploaded_files:
        st.info("Select multiple files, then click Start Bulk Processing.")
        return

    st.write(f"**{len(uploaded_files)} file(s) selected.**")

    if not st.button("Start Bulk Processing", type="primary"):
        return

    if not up.OPENAI_API_KEY:
        st.error(
            "OPENAI_API_KEY is not set. Add it to your .env file before "
            "running extraction."
        )
        return

    # Read every file's bytes up front, on the main thread -- UploadedFile
    # objects are tied to this script run and shouldn't be touched from
    # worker threads.
    jobs = [(f.name, f.getvalue()) for f in uploaded_files]

    progress = st.progress(0.0, text=f"0 / {len(jobs)} processed")
    log = st.container()
    saved, failed = 0, 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_job = {
            executor.submit(up.extract_and_derive, method, file_bytes, filename): (filename, file_bytes)
            for filename, file_bytes in jobs
        }
        for i, future in enumerate(as_completed(future_to_job), start=1):
            filename, file_bytes = future_to_job[future]
            error_detail = None
            try:
                fields, derived = future.result()
            except Exception as e:
                fields, derived = None, None
                error_detail = str(e)

            if fields is None:
                failed += 1
                log.write(f"❌ **{filename}** -- {error_detail or 'no text could be extracted'}")
            else:
                record = _build_bulk_record(filename, method, fields, derived)
                (up.UPLOADS_DIR / record["stored_document"]).write_bytes(file_bytes)
                up.append_record_to_store(record)
                saved += 1
                log.write(f"✅ **{filename}** -> {fields['product_chemical_name'] or '(unnamed)'}")

            progress.progress(i / len(jobs), text=f"{i} / {len(jobs)} processed")

    st.divider()
    if failed:
        st.warning(f"Done: {saved} saved, {failed} failed, out of {len(jobs)} total.")
    else:
        st.success(f"Done: all {saved} document(s) saved successfully.")
