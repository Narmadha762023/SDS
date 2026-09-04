"""Bulk Upload page.

Two ways to bring in a batch, picked via the "Upload Source" radio -- both
feed the exact same downstream pipeline (duplicate check, extraction,
saving), so nothing about how a file is processed depends on which one
was used:

- **ZIP file**: for a whole batch at once. Every matching file inside the
  ZIP is processed, including ones in subfolders -- convenient when you
  already have a folder of documents and just want all of them in.
- **Individual files**: a normal multi-file picker, for when you only
  want specific documents processed rather than everything in a folder.

Either way, this is a normal browser file upload, not a native OS dialog
-- chosen deliberately over a folder picker: a real browser upload works
identically whether this app is run locally or hosted for other people
later, with no rebuild needed either way.

Each document is extracted (via extraction_pipeline.py) and saved
directly, with no per-document review step -- spot-check or correct
records later from the SDS Repository page. Since there's no human
reviewing each record before it's saved here, pictogram icon detection
still runs for every file rather than being skipped for speed -- otherwise
bulk-saved records would be quietly less complete, with nothing catching
the gap.

Before any AI call is made, every file is checked against a SHA-256
content hash of everything already saved (and against other files earlier
in the same ZIP) -- an exact-content fingerprint, not a filename
comparison, so a renamed copy of an already-saved document is still
caught, and two different documents that happen to share a filename are
never falsely skipped. Duplicates are logged and skipped before they ever
reach the AI, so re-running a batch that partly succeeded before doesn't
burn tokens re-processing files that already made it in.

Extraction for multiple documents runs concurrently, bounded to a small
number at a time. Each document's AI calls mostly spend their time waiting
on a network response rather than doing local computation, so several can
be "in flight" together without needing more CPU -- like several checkout
registers open at once instead of one. The concurrency cap keeps this from
overwhelming the AI service or tripping its rate limits. Every file's saved
document and JSON record write still happens back on the main thread, one
at a time, so there's no risk of two threads corrupting the shared
response/sds_records.json file by writing to it at the same moment.

Built for batches of up to a few dozen files in one session. Streamlit's
default upload limit (200MB per file) also caps how large a single ZIP can
be. A true bulk import of hundreds or thousands of documents would need a
background job that keeps running independently of this page (so a
browser refresh or laptop sleep doesn't lose progress), which is a bigger
piece of infrastructure than this page provides.
"""

import io
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import streamlit as st

import extraction_pipeline as up

MAX_WORKERS = 5

PDF_EXTENSIONS = {".pdf"}
OCR_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}


def _list_matching_entries(zf: zipfile.ZipFile, extensions: set) -> list:
    """Every file entry in the zip (at any depth) whose extension matches,
    skipping directories and macOS/system junk entries."""
    entries = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename
        if "__MACOSX" in name or Path(name).name.startswith("."):
            continue
        if Path(name).suffix.lower() in extensions:
            entries.append(info)
    entries.sort(key=lambda info: info.filename.lower())
    return entries


def _split_new_and_duplicate(jobs: list) -> tuple:
    """Partition (filename, file_bytes) jobs into new vs. duplicate, using
    a SHA-256 content hash -- both against already-saved records and
    against earlier files in this same batch. Returns
    (new_jobs, duplicate_jobs) where new_jobs items are
    (filename, file_bytes, file_hash) and duplicate_jobs items are
    (filename, reason).
    """
    existing_hashes = up.load_existing_hashes()
    seen_in_batch = {}
    new_jobs = []
    duplicate_jobs = []

    for filename, file_bytes in jobs:
        file_hash = up.compute_file_hash(file_bytes)
        if file_hash in existing_hashes:
            info = existing_hashes[file_hash]
            saved_date = info["saved_at"][:10] or "an earlier upload"
            duplicate_jobs.append(
                (filename, f'already saved as "{info["product_chemical_name"]}" on {saved_date}')
            )
        elif file_hash in seen_in_batch:
            duplicate_jobs.append(
                (filename, f'duplicate of "{seen_in_batch[file_hash]}" earlier in this ZIP')
            )
        else:
            seen_in_batch[file_hash] = filename
            new_jobs.append((filename, file_bytes, file_hash))

    return new_jobs, duplicate_jobs


def _build_bulk_record(filename: str, file_hash: str, method: str, fields: dict, derived: dict) -> dict:
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
        "token_usage_total": derived["token_usage"]["total_tokens"],
        "estimated_cost_usd": round(derived["token_usage"]["estimated_cost_usd"], 6),
        "file_sha256": file_hash,
    }


def _collect_jobs_from_zip(extensions: set):
    """Renders the ZIP-upload widget and, once the user clicks Start,
    returns the list of (filename, file_bytes) jobs found inside it. This
    is for when you have a whole batch to run -- every matching file in
    the ZIP gets processed, no picking and choosing.

    Returns None (having already rendered whatever message/error applies)
    if there's nothing to process yet.
    """
    zip_upload = st.file_uploader(
        "Upload a ZIP file of SDS documents",
        type=["zip"],
        key="bulk_zip_uploader",
    )
    if zip_upload is None:
        st.info("Select a ZIP file to begin. (Upload limit: 200MB.)")
        return None

    try:
        with zipfile.ZipFile(io.BytesIO(zip_upload.getvalue())) as zf:
            found_entries = _list_matching_entries(zf, extensions)

            if not found_entries:
                st.warning(
                    f"No matching files ({', '.join(sorted(extensions))}) "
                    f"found inside this ZIP."
                )
                return None

            st.write(f"**{len(found_entries)} file(s) found in ZIP.**")
            with st.expander("View file list"):
                for info in found_entries:
                    st.write(info.filename)

            if not st.button("Start Bulk Processing", type="primary"):
                return None

            if not up.OPENAI_API_KEY:
                st.error(
                    "OPENAI_API_KEY is not set. Add it to your .env file "
                    "before running extraction."
                )
                return None

            # Read every matching entry's bytes up front, on the main
            # thread, before handing them to worker threads.
            return [(Path(info.filename).name, zf.read(info.filename)) for info in found_entries]
    except zipfile.BadZipFile:
        st.error("This doesn't look like a valid ZIP file.")
        return None


def _collect_jobs_from_files(extensions: set):
    """Renders a multi-file picker and, once the user clicks Start,
    returns the list of (filename, file_bytes) jobs for just the files
    they picked -- for when you only want specific documents processed,
    not everything in a folder/ZIP.

    Returns None (having already rendered whatever message/error applies)
    if there's nothing to process yet.
    """
    uploaded_files = st.file_uploader(
        "Select SDS documents",
        type=sorted(ext.lstrip(".") for ext in extensions),
        accept_multiple_files=True,
        key="bulk_files_uploader",
    )
    if not uploaded_files:
        st.info("Select one or more files to begin.")
        return None

    st.write(f"**{len(uploaded_files)} file(s) selected.**")
    with st.expander("View file list"):
        for f in uploaded_files:
            st.write(f.name)

    if not st.button("Start Bulk Processing", type="primary"):
        return None

    if not up.OPENAI_API_KEY:
        st.error(
            "OPENAI_API_KEY is not set. Add it to your .env file before "
            "running extraction."
        )
        return None

    return [(f.name, f.getvalue()) for f in uploaded_files]


def render() -> None:
    st.title("Bulk Upload")
    st.caption(
        "Process many SDS documents at once -- automatically extracted and "
        "saved, no per-document review step. Spot-check or correct "
        "individual records afterward from the SDS Repository page."
    )

    method = st.radio(
        "Processing Method (applies to every file in this batch)",
        ["PDF Extraction", "OCR"],
        horizontal=True,
        key="bulk_method",
    )
    if method == "PDF Extraction":
        st.caption("For PDFs that already contain selectable / machine-readable text.")
        extensions = PDF_EXTENSIONS
    else:
        st.caption("Reads scanned/image-based documents using OCR.")
        extensions = OCR_EXTENSIONS

    source = st.radio(
        "Upload Source",
        ["ZIP file (a whole batch)", "Individual files (pick specific ones)"],
        horizontal=True,
        key="bulk_source",
    )

    if source.startswith("ZIP"):
        jobs = _collect_jobs_from_zip(extensions)
    else:
        jobs = _collect_jobs_from_files(extensions)

    if not jobs:
        return

    new_jobs, duplicate_jobs = _split_new_and_duplicate(jobs)

    if duplicate_jobs:
        st.warning(
            f"{len(duplicate_jobs)} file(s) look like duplicates (already "
            f"saved, or repeated within this ZIP) and will be skipped "
            f"before any AI processing -- no tokens spent on them."
        )
        with st.expander("View skipped duplicates"):
            for filename, reason in duplicate_jobs:
                st.write(f"⏭️ **{filename}** -- {reason}")

    if not new_jobs:
        st.info("Nothing new to process -- every matching file is a duplicate.")
        return

    st.write(f"**{len(new_jobs)} new file(s) will be processed.**")

    total_jobs = len(jobs)
    progress = st.progress(0.0, text=f"0 / {total_jobs} processed")
    usage_line = st.empty()
    log = st.container()
    saved, failed = 0, 0
    total_tokens, total_cost = 0, 0.0
    any_unpriced = False
    done = 0

    for filename, reason in duplicate_jobs:
        log.write(f"⏭️ **{filename}** -- skipped, {reason}")
        done += 1
        progress.progress(done / total_jobs, text=f"{done} / {total_jobs} processed")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_job = {
            executor.submit(up.extract_and_derive, method, file_bytes, filename): (filename, file_bytes, file_hash)
            for filename, file_bytes, file_hash in new_jobs
        }
        for future in as_completed(future_to_job):
            filename, file_bytes, file_hash = future_to_job[future]
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
                record = _build_bulk_record(filename, file_hash, method, fields, derived)
                (up.UPLOADS_DIR / record["stored_document"]).write_bytes(file_bytes)
                up.append_record_to_store(record)
                saved += 1
                usage = derived["token_usage"]
                total_tokens += usage["total_tokens"]
                total_cost += usage["estimated_cost_usd"]
                any_unpriced = any_unpriced or usage["has_unpriced_calls"]
                log.write(
                    f"✅ **{filename}** -> {fields['product_chemical_name'] or '(unnamed)'} "
                    f"({usage['total_tokens']:,} tokens)"
                )

            done += 1
            progress.progress(done / total_jobs, text=f"{done} / {total_jobs} processed")
            usage_line.caption(
                f"Running total: {total_tokens:,} tokens -- "
                f"~${total_cost:.4f} estimated"
                + (" (some calls used a model not in the pricing table, so cost is a partial estimate)" if any_unpriced else "")
            )

    st.divider()
    skipped = len(duplicate_jobs)
    summary_bits = [f"{saved} saved"]
    if failed:
        summary_bits.append(f"{failed} failed")
    if skipped:
        summary_bits.append(f"{skipped} duplicate(s) skipped")
    summary = ", ".join(summary_bits) + f", out of {total_jobs} total."
    if failed:
        st.warning(f"Done: {summary}")
    else:
        st.success(f"Done: {summary}")
    st.info(
        f"**Total token usage: {total_tokens:,} tokens** "
        f"(~${total_cost:.4f} estimated, at current OpenAI pricing for the "
        f"model(s) used). This is an estimate, not an invoice-accurate "
        f"figure -- check platform.openai.com/usage for the authoritative number."
    )
