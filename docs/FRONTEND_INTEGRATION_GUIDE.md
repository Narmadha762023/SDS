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

Per document: raw file bytes + a filename + a method choice. The app
accepts these either as individual files or bundled in a ZIP (every
matching entry inside, at any depth) -- both reduce to the same list of
`(filename, file_bytes)` pairs before anything else happens, so this
input shape is what matters regardless of which way they arrived.

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

Before any AI call, each file's SHA-256 content hash is checked against
every already-saved record and against other files in the same batch --
an exact match is treated as a duplicate and skipped before it reaches
the AI (see `file_sha256` below). This is a content fingerprint, not a
filename comparison.

### Output: one saved record

This is the exact JSON shape written to `response/sds_records.json` and read
back by the repository/search page. Every field marked "AI-extracted" can
legitimately be `""` / `[]` -- the AI is instructed to never guess, so a
field simply not present in the source document comes back empty rather
than fabricated.

Fields marked "regex, AI fallback" are found by pattern matching first
(zero AI tokens, see `regex_extractor.py`) and only sent to the AI for a
given document if the pattern isn't found there -- from the caller's side
this is invisible, the field is populated either way, just sometimes for
free. This field set follows `SDS-Extraction-Contract.pdf` v2; a few of
that contract's fields (structured hazard/precautionary statement pairs,
`disposal`, `regulatory_flags`, `language`, and the
`status`/`confidence`/`warnings` envelope) aren't implemented yet -- see
`BACKEND_AI_GUIDE.md` section 4 for why. `keywords` **is** implemented,
deterministically (no AI) -- see `extraction_pipeline.generate_keywords()`.

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
  "product_code": ["AC326980000", "AC326981000"],
  "synonyms": ["Tol", "Methylbenzene"],
  "regulation_basis": "US OSHA Hazard Communication Standard 2024 (29 CFR 1910.1200)",
  "signal_word": "DANGER",
  "flash_point": "4 C",
  "nfpa": {"health": 3, "flammability": 3, "instability": 0},
  "transport": {"un_no": "UN1294", "shipping_name": "TOLUENE", "hazard_class": "3", "packing_group": "II"},
  "rcra_waste_code": "U220",
  "ingredients": [{"name": "Toluene", "cas_number": "108-88-3", "concentration": "<=100%"}],
  "keywords": ["toluene", "flammable", "irritant", "liquid", "laboratory chemicals"],
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
  "saved_at": "2026-09-03T15:41:22",
  "token_usage_total": 38249,
  "estimated_cost_usd": 0.005816,
  "file_sha256": "b1946ac92492d2347c6235b4d2611184..."
}
```

| Field | Type | Source |
|---|---|---|
| `id` | string (UUID) | generated on save |
| `product_chemical_name` | string | AI-extracted |
| `manufacturer_supplier` | string | AI-extracted |
| `emergency_contact_phone` | string | AI-extracted |
| `cas_number` | string | AI-extracted |
| `ghs_hazard_pictograms` | array of `"GHSxx - Label"` strings | AI-extracted (text match + H-code lookup) + icon detection (template match against embedded/rendered images, falling back to AI vision only if needed), merged -- see `BACKEND_AI_GUIDE.md` step 3 |
| `safety_hazards` | string | AI-extracted |
| `first_aid_measures` | string | AI-extracted |
| `personal_protection` | string | AI-extracted |
| `storage` | string | AI-extracted |
| `physical_state` | string | regex (fixed vocabulary: Solid/Liquid/Gas/Gel/...), AI fallback |
| `category` | string | AI-extracted |
| `product_code` | array of strings | regex (Section 1 catalog numbers), AI fallback |
| `synonyms` | array of strings | regex (Section 1 "Synonyms" label), AI fallback |
| `regulation_basis` | string | regex, AI fallback |
| `signal_word` | `"DANGER"` \| `"WARNING"` \| `"NONE"` \| `""` | regex, AI fallback |
| `flash_point` | string | regex (Section 9), AI fallback |
| `nfpa` | object `{health, flammability, instability}` or `{}` | regex (Section 5/16 NFPA 704 diamond), AI fallback |
| `transport` | object `{un_no, shipping_name, hazard_class, packing_group}` or `{}` | regex (Section 14), AI fallback |
| `rcra_waste_code` | string | regex (Section 13), AI fallback |
| `ingredients` | array of `{name, cas_number, concentration}` | regex (Section 3 composition table, one row per line), AI fallback |
| `keywords` | array of strings, 5-15 lowercased tags | deterministic, no AI -- derived from product name/synonyms/pictogram labels/signal word/physical state/recommended use (`extraction_pipeline.generate_keywords()`); powers tag-based filtering, not free-text search |
| `version` | string | regex (`Revision Number N`), AI fallback, or parsed from filename as last resort |
| `revision_date` | string, `YYYY-MM-DD` or `""` | regex (`Revision Date ...`), AI fallback, parsed |
| `issue_date` | string, `YYYY-MM-DD` or `""` | regex (`Creation Date`/`Issue Date`), AI fallback, parsed |
| `applies_to_site` | string | defaulted to `"All Sites"` -- no review step sets this today |
| `review_owners` | array of strings | defaulted to `[]` -- no review step sets this today |
| `review_frequency` | string | AI-extracted if stated in doc, else default `"Annually"` |
| `next_review_due` | string, `YYYY-MM-DD` | computed (`revision_date` + stated interval), else today |
| `processing_method` | `"PDF Extraction"` \| `"OCR"` | which pipeline processed this document |
| `original_filename` | string | as uploaded |
| `stored_document` | string | filename under `uploads/` -- `<id><ext>` |
| `saved_at` | string, ISO 8601 datetime | generated on save |
| `token_usage_total` | integer | sum of every AI call's tokens for this document (see `usage_tracker.py`) |
| `estimated_cost_usd` | number | estimated from a fixed price table, not invoice-accurate -- see `usage_tracker.MODEL_PRICING` |
| `file_sha256` | string, hex | SHA-256 of the original file bytes -- used for duplicate detection on future uploads |

### The underlying Python function contract

If you're calling into this as a library rather than over HTTP, the
functions to know are all in `extraction_pipeline.py`, which has **no
Streamlit dependency at all** -- plain Python, safe to call from any
context (including a real API server):

```python
fields, derived = extraction_pipeline.extract_and_derive(method, file_bytes, filename)
# fields:  dict matching ai_extractor.FIELDS_SCHEMA (25 keys, see below) --
#          each key is populated by regex_extractor.py first where a tier
#          exists for it, falling back to the AI for whatever it missed
#          (extraction_pipeline._apply_regex_extraction() + skip_fields)
# derived: dict with ghs_selected_codes, ghs_hazard_pictograms,
#          version, version_source, revision_date_parsed, issue_date_parsed,
#          review_frequency, review_frequency_raw, next_review_due
# Returns (None, None) if no text could be extracted from the document.
```

`ai_extractor.FIELDS_SCHEMA` (the full field set, before derivation --
this is also the schema `regex_extractor.py` fills in wherever it can):

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
    # -- SDS-Extraction-Contract.pdf v2 additions (regex-first, AI fallback) --
    "product_code": [],
    "synonyms": [],
    "regulation_basis": "",
    "signal_word": "",
    "flash_point": "",
    "nfpa": {},                         # {"health": 3, "flammability": 3, "instability": 0}
    "transport": {},                    # {"un_no": "UN1294", "shipping_name": ..., "hazard_class": ..., "packing_group": ...}
    "rcra_waste_code": "",
    "ingredients": [],                  # [{"name": ..., "cas_number": ..., "concentration": ...}]
}
```

### Position-aware search data (for a real "highlight this match" UI)

The **SDS Repository** page's search (see `repository.py`) can find a term
that's genuinely in a document but wasn't one of the extracted fields
above, and open the source page with the exact phrase highlighted. In this
reference app, that's rendered as a static image (`pdf_render.py`) for
simplicity -- **a real integration wouldn't do that**. It would instead
consume the underlying data directly and draw its own highlight overlay in
whatever PDF viewer/component it already uses (e.g. PDF.js).

That data lives in `response/word_index/<record_id>.json`
(`pdf_word_index.py`, PDF Extraction documents only -- see the module
docstring for why OCR documents have no equivalent), shaped as:

```python
[
  {
    "page": 5,
    "text": "4-Methylbenzyl alcohol Revision Date 19-Dec-2025 ...",  # all words on the page, space-joined
    "words": [
      {"text": "4-Methylbenzyl", "x0": 53.9, "top": 37.7, "x1": 125.3, "bottom": 47.7},
      {"text": "alcohol", "x0": 128.1, "top": 37.7, "x1": 163.1, "bottom": 47.7},
      ...
    ]
  },
  ...  # one entry per page
]
```

`x0`/`x1`/`top`/`bottom` are in the PDF's own page-point coordinate space
(the same system `pdfplumber` and `PyMuPDF`/most PDF renderers use for an
unrotated page -- no conversion needed). `pdf_word_index.find_match(record_id,
needle)` shows the matching logic: substring-search the page's joined
`text`, map the matched character range back to the word(s) it spans, and
take the union of their bounding boxes as the highlight rectangle.

---

## Part 2 -- Proposed API contract (not implemented)

A thin [FastAPI](https://fastapi.tiangolo.com/) service wrapping the
existing functions would look like this. It reuses `extraction_pipeline.py`'s
functions directly -- no extraction logic would need to be rewritten,
only exposed.

### `POST /api/v1/extract`

Run extraction on one document **without saving it** -- for a frontend
that wants its own review/edit UI before committing a record. Nothing in
this app builds that UI today (there's no per-document review step
anywhere in the current app, single or bulk), so this endpoint would be
new capability, not a wrapper around an existing page.

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
  `response/sds_records.json` + `uploads/` storage as today. That's a real
  scaling limit (see `BACKEND_AI_GUIDE.md` section 4) independent of
  whether an API layer exists -- adding an API on top of the current
  storage doesn't fix concurrency-safety or make it survive a redeploy.
- **Auth**: none proposed here. A real integration needs to decide who's
  allowed to call these endpoints before anything else.
- **Editing a saved record**: there's no update endpoint proposed above
  because there's no equivalent capability in the app today -- saved
  records are currently write-once (create) and read-only afterward.
