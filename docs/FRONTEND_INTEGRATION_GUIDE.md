# Frontend Integration Guide

Audience: a frontend engineer who wants to call this system from their own
application.

## Read this first: there is no API today

This project is a self-contained **Streamlit app** -- `streamlit run
app.py` renders the UI *and* runs the extraction logic *and* writes to
local files, all in one process. There is currently no HTTP endpoint a
separate frontend can call, no request/response contract, nothing to
`fetch()` against.

This document has two parts:

1. **What exists today** -- the exact data shapes (input and output) the
   underlying Python functions use, so you understand the actual
   input/output contract of the system regardless of how it's exposed.
2. **A proposed API** -- a thin wrapper design that would expose those
   same functions over HTTP, so a frontend could integrate against this
   as a real backend. **This is a proposed design, not implemented.**
   Building it is a separate task from anything in this repo today.

---

## Part 1 -- The actual data shapes

### Input

One document at a time: raw file bytes + a filename + a method choice.

```
file_bytes: bytes            # the raw PDF (or PNG/JPG for OCR) content
filename:   str               # e.g. "SDS - Acetone - v3.2.pdf"
method:     "PDF Extraction" | "OCR"
```

`method` is a hard choice, never auto-detected -- "PDF Extraction" is for
PDFs with a real, selectable text layer; "OCR" is for scanned PDFs or
images with no text layer. Picking the wrong one either fails cleanly
(PDF Extraction on a scanned PDF returns no text and errors out) or costs
more than necessary (OCR on a text PDF works, but calls the vision model
unnecessarily).

### Output: one saved record

This is the exact JSON shape written to `data/sds_records.json` and read
back by the repository/search page. Every field marked "AI-extracted" can
legitimately be `""` / `[]` -- the AI is instructed to never guess, so a
field simply not present in the source document comes back empty rather
than fabricated.

```json
{
  "id": "e0629327-f12a-4557-8ce9-808a455d613c",
  "product_chemical_name": "Acetone",
  "manufacturer_supplier": "ChemSupply",
  "emergency_contact_phone": "1-800-111-1111",
  "cas_number": "67-64-1",
  "ghs_hazard_pictograms": ["GHS02 - Flammable"],
  "safety_hazards": "H225 - Highly flammable liquid and vapor.",
  "first_aid_measures": "Eyes - rinse with water.",
  "personal_protection": "",
  "storage": "Keep in cool dry place.",
  "physical_state": "Liquid",
  "category": "Flammable Liquid",
  "version": "3.2",
  "revision_date": "2025-03-03",
  "issue_date": "",
  "applies_to_site": "All Sites",
  "review_owners": ["EHS Manager"],
  "review_frequency": "Every 3 Years",
  "next_review_due": "2028-03-03",
  "processing_method": "PDF Extraction",
  "original_filename": "SDS - Acetone - v3.2.pdf",
  "stored_document": "e0629327-f12a-4557-8ce9-808a455d613c.pdf",
  "saved_at": "2026-09-03T15:41:22"
}
```

| Field | Type | Source |
|---|---|---|
| `id` | string (UUID) | generated on save |
| `product_chemical_name` | string | AI-extracted |
| `manufacturer_supplier` | string | AI-extracted |
| `emergency_contact_phone` | string | AI-extracted |
| `cas_number` | string | AI-extracted |
| `ghs_hazard_pictograms` | array of `"GHSxx - Label"` strings | AI-extracted (text match + H-code lookup) + vision icon detection, merged |
| `safety_hazards` | string | AI-extracted |
| `first_aid_measures` | string | AI-extracted |
| `personal_protection` | string | AI-extracted |
| `storage` | string | AI-extracted |
| `physical_state` | string | AI-extracted |
| `category` | string | AI-extracted |
| `version` | string | AI-extracted, or parsed from filename as fallback |
| `revision_date` | string, `YYYY-MM-DD` or `""` | AI-extracted, parsed |
| `issue_date` | string, `YYYY-MM-DD` or `""` | AI-extracted, parsed |
| `applies_to_site` | string | user-chosen (single upload) or defaulted (bulk) |
| `review_owners` | array of strings | user-chosen (single upload) or `[]` (bulk) |
| `review_frequency` | string | AI-extracted if stated in doc, else default `"Annually"` |
| `next_review_due` | string, `YYYY-MM-DD` | computed (`revision_date` + stated interval), else today |
| `processing_method` | `"PDF Extraction"` \| `"OCR"` | which pipeline processed this document |
| `original_filename` | string | as uploaded |
| `stored_document` | string | filename under `uploads/` -- `<id><ext>` |
| `saved_at` | string, ISO 8601 datetime | generated on save |

### The underlying Python function contract

If you're calling into this as a library rather than over HTTP, the
functions to know (all in `upload_page.py`, no Streamlit dependency
except `run_extraction`/`save_record` which read `st.session_state`):

```python
# Headless -- no Streamlit dependency, safe to call from any context.
fields, derived = upload_page.extract_and_derive(method, file_bytes, filename)
# fields:  dict matching ai_extractor.FIELDS_SCHEMA (16 keys, see below)
# derived: dict with ghs_selected_codes, ghs_hazard_pictograms,
#          version, version_source, revision_date_parsed, issue_date_parsed,
#          review_frequency, review_frequency_raw, next_review_due
# Returns (None, None) if no text could be extracted from the document.
```

`ai_extractor.FIELDS_SCHEMA` (the raw AI output, before derivation):

```python
{
    "product_chemical_name": "",
    "manufacturer_supplier": "",
    "emergency_contact_phone": "",
    "cas_number": "",
    "ghs_hazard_pictograms": [],       # e.g. ["GHS02 - Flame"]
    "hazard_statement_codes": [],       # e.g. ["H225", "H304"]
    "safety_hazards": "",
    "physical_state": "",
    "category": "",
    "version": "",
    "revision_date": "",
    "issue_date": "",
    "first_aid_measures": "",
    "personal_protection": "",
    "storage": "",
    "review_frequency_stated": "",
}
```

---

## Part 2 -- Proposed API contract (not implemented)

A thin [FastAPI](https://fastapi.tiangolo.com/) service wrapping the
existing functions would look like this. It reuses `upload_page.py`'s
functions directly -- no extraction logic would need to be rewritten,
only exposed.

### `POST /api/v1/extract`

Run extraction on one document **without saving it** -- for a frontend
that wants its own review/edit UI before committing a record (mirroring
what the current Upload & Extract Streamlit page does).

**Request**: `multipart/form-data`
| Field | Type | Notes |
|---|---|---|
| `file` | file | the PDF/image |
| `method` | string | `"PDF Extraction"` \| `"OCR"` |

**Response** `200`: the field set from Part 1 (`fields` + `derived`
merged into one flat object), plus `raw_text` (the extracted document
text, for a "view extracted text" panel) -- everything **except** `id`,
`saved_at`, `stored_document` (nothing is saved yet).

**Response** `422`: `{"error": "no_text_extracted", "message": "..."}` --
mirrors today's "No selectable text was found" case.

### `POST /api/v1/records`

Save a record -- called after the frontend's own review step, so the
body is whatever the user confirmed/edited, not necessarily the raw
`/extract` output verbatim.

**Request**: `multipart/form-data` with the original `file` plus the full
field set from the "Output: one saved record" schema above (minus `id`,
`saved_at`, `stored_document`, which the server generates).

**Response** `201`: the full saved record, as in Part 1.

### `GET /api/v1/records?q=<search>`

List/search saved records -- mirrors the SDS Repository page's search
(matches product name, manufacturer, or CAS number).

**Response** `200`: `{"records": [ ...record objects... ], "count": N}`

### `GET /api/v1/records/{id}`

**Response** `200`: one record object. `404` if not found.

### `GET /api/v1/records/{id}/document`

Streams the original file (`Content-Type: application/pdf` or the
appropriate image type) -- this is what backs the Repository page's
"Open Full SDS" button.

### `POST /api/v1/bulk-upload`

**Request**: `multipart/form-data`, multiple `files[]`, plus `method`.

Mirrors `bulk_upload.py`: extracts and saves each file directly, no
review step, running concurrently server-side.

**Response** `200`:
```json
{
  "total": 3,
  "saved": 2,
  "failed": 1,
  "results": [
    {"filename": "a.pdf", "status": "saved", "record_id": "..."},
    {"filename": "b.pdf", "status": "saved", "record_id": "..."},
    {"filename": "c.pdf", "status": "failed", "error": "no_text_extracted"}
  ]
}
```

For large batches (see the known-limitations note in
`BACKEND_AI_GUIDE.md` -- there's no cost/size cap today), a production
version of this endpoint should be async (return a job id immediately,
poll or webhook for completion) rather than holding the HTTP request open
for the whole batch, and should have a real size/cost guard before
accepting a batch. Neither exists in the current code.

### What this proposal deliberately doesn't solve

- **Storage**: the proposed endpoints assume the same local
  `data/sds_records.json` + `uploads/` storage as today. That's a real
  scaling limit (see `BACKEND_AI_GUIDE.md` section 4) independent of
  whether an API layer exists -- adding an API on top of the current
  storage doesn't fix concurrency-safety or make it survive a redeploy.
- **Auth**: none proposed here. A real integration needs to decide who's
  allowed to call these endpoints before anything else.
- **Editing a saved record**: there's no update endpoint proposed above
  because there's no equivalent capability in the app today -- saved
  records are currently write-once (create) and read-only afterward.
