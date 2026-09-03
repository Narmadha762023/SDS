# Backend / AI Engineer Guide

Audience: an engineer picking up this codebase who needs to understand how
data flows from an uploaded PDF to a saved record, what each AI call does,
and where to change things.

This app has no separate backend service today -- `streamlit run app.py`
*is* the whole application (UI + logic + storage in one process). Read
[FRONTEND_INTEGRATION_GUIDE.md](FRONTEND_INTEGRATION_GUIDE.md) for the
data shapes and a proposed API layer if you need to expose this to a
separate frontend.

## 1. File map

| File | Responsibility |
|---|---|
| `app.py` | Entry point. Page config + 3-page navigation (`st.navigation`). No business logic. |
| `upload_page.py` | The core pipeline lives here: extraction orchestration, field derivation, the single-document review form, saving. Other pages import functions from this file. |
| `bulk_upload.py` | Runs the same pipeline as `upload_page.py`, concurrently, across many files, with no review step. |
| `repository.py` | Reads `data/sds_records.json` and renders the search list + summary popup + PDF download. Read-only; no extraction logic. |
| `pdf_extractor.py` | `extract_text(pdf_bytes) -> str`. Reads a PDF's real text layer via `pdfplumber`. No AI involved. |
| `ocr.py` | Vision-based text reading for scanned/image documents, and (combined) pictogram-icon detection. |
| `pictogram_detector.py` | Standalone vision pass that recognizes GHS icon shapes. Used only by the PDF Extraction path (see step 3). |
| `image_utils.py` | `pdf_to_page_images()` / `image_to_data_url()` -- shared by `ocr.py` and `pictogram_detector.py`. |
| `ai_extractor.py` | `extract_fields(text, api_key, model) -> dict`. The single LLM call that pulls structured fields out of raw document text. |
| `data/sds_records.json` | The "database" -- a flat JSON array of saved records, read-modify-write on every save. |
| `uploads/` | The original PDF/image files, one per saved record, named `<record_id><ext>`. |

## 2. Step by step: one document, start to finish

This is what happens when a user uploads one file on the **Upload &
Extract** page (or, headlessly, when `upload_page.extract_and_derive()` is
called from `bulk_upload.py`).

### Step 1 -- Get raw text out of the document

The user picks one of two methods (never auto-selected):

- **PDF Extraction** -- `pdf_extractor.extract_text(file_bytes)`. Reads the
  PDF's real text layer via `pdfplumber`, page by page, joined with
  `--- Page N ---` separators. Pure text extraction, no AI call.
- **OCR** -- `ocr.extract_text_and_pictograms(file_bytes, filename, api_key, model)`.
  Renders each page to a PNG (`image_utils.pdf_to_page_images`, 200 DPI)
  and sends it to the vision model. See step 3 for why this call also
  returns pictogram codes.

If the resulting text is empty/whitespace-only, extraction stops there --
`extract_and_derive()` returns `(None, None)` and the caller shows an
error rather than proceeding with nothing.

### Step 2 -- AI pulls out the structured fields

`ai_extractor.extract_fields(raw_text, api_key, model)`:

- Sends the raw text (truncated to the first 60,000 characters) to the
  chat completions API with `temperature=0` and
  `response_format={"type": "json_object"}`.
- The prompt (`USER_PROMPT_TEMPLATE` in `ai_extractor.py`) lists every
  field with per-field instructions -- e.g. `version` explains what
  labels count as a match ("Version", "Rev.", etc.); `safety_hazards` is
  explicitly told to include *every* hazard statement found, not a
  trimmed subset, since dropping one would be a real safety gap.
- The system prompt enforces the core rule: never guess, return `""` /
  `[]` for anything not literally in the text.
- The raw model response is parsed as JSON and validated key-by-key
  against `FIELDS_SCHEMA` -- any missing/malformed key falls back to the
  schema's empty default rather than propagating a bad shape.

See `ai_extractor.FIELDS_SCHEMA` for the exact 16 keys this returns.

### Step 3 -- Pictogram icon detection

SDS hazard pictograms are almost always graphic icons, not text, so
neither of the above steps can see them. A dedicated vision pass looks
specifically for the 9 standard GHS icon shapes (see
`pictogram_detector.DETECT_PROMPT` for the exact icon descriptions used).

- **PDF Extraction path**: `_detect_pictogram_icons()` in `upload_page.py`
  calls `pictogram_detector.detect_pictograms()` as its own extra vision
  call over the first 3 pages -- since this pipeline never rendered any
  page images in step 1, there's nothing to piggyback on.
- **OCR path**: folded into the *same* call as step 1
  (`ocr.extract_text_and_pictograms`). Since OCR already renders and
  sends the first few pages as images to read their text, asking a
  second, separate question about the same images would upload them
  twice for no reason -- `ocr._transcribe_and_detect()` sends one combined
  prompt per page asking for both the transcribed text and the visible
  pictogram codes in one JSON response.

Both entry points fail soft: `_detect_pictogram_icons()` wraps
`pictogram_detector` in a try/except and degrades to `[]` (no pictograms
found) rather than taking down an otherwise-successful extraction.

### Step 4 -- Resolve pictograms (three signals merged)

`upload_page.compute_derived_fields()` builds the final pictogram set from
**three independent sources**, unioned together:

1. **Direct text match** -- `match_pictograms()` checks each AI-returned
   `ghs_hazard_pictograms` string against `PICTOGRAM_DEFS` (code, label, or
   a synonym list like "flame", "skull", "exclamation").
2. **H-code lookup (deterministic)** -- the AI separately extracted any
   literal hazard statement codes (`hazard_statement_codes`, e.g. "H225").
   Those are run through `H_CODE_TO_PICTOGRAMS`, a fixed Python dict
   encoding the official UN GHS H-code -> pictogram table. **This mapping
   is applied in code, not asked of the LLM** -- an earlier version asked
   the model to do this lookup itself and it missed most codes even with
   the full table in the prompt; moving it to a deterministic dict fixed
   that completely (verified: 5/5 H-codes correctly resolved after the
   change, vs 1/5 before).
3. **Vision icon detection** -- the codes from step 3.

### Step 5 -- Other derived fields (also in `compute_derived_fields`)

- **Version fallback**: if the AI found no version in the text,
  `parse_version_from_filename()` regexes the uploaded filename for a
  `vX.Y` pattern (a naming convention several real SDS files use). Only
  used as a fallback, and flagged in the UI as filename-sourced.
- **Date parsing**: `parse_date_safe()` runs `dateutil`'s fuzzy parser on
  whatever date string the AI extracted. Unparseable text is left as
  `None` (not guessed) with the raw AI text shown in a caption.
- **Review cadence**: `parse_review_interval_years()` regexes a stated
  interval (e.g. "Every 3 Years") out of the AI's
  `review_frequency_stated`. If found, `next_review_due` is *computed*
  (`revision_date + relativedelta(years=N)`) in Python -- never left to
  the model to do date arithmetic.

### Step 6 -- Save

`save_record()` (single doc, reads from `st.session_state`) or
`bulk_upload._build_bulk_record()` (headless) assemble the final record
dict (see the schema in `FRONTEND_INTEGRATION_GUIDE.md`), then:

- The original file bytes are written to `uploads/<record_id><ext>`.
- `append_record_to_store()` reads `data/sds_records.json`, appends the
  new record, writes the whole file back.

## 3. The bulk pipeline -- what's different

`bulk_upload.py` calls the exact same `upload_page.extract_and_derive()`
function per file -- there is no separate/different AI logic for bulk.
What changes is orchestration:

- All selected files' bytes are read into memory up front, on the main
  thread (`f.getvalue()` for each `UploadedFile`) -- Streamlit's uploaded
  file objects aren't safe to touch from a worker thread, so plain
  `bytes` are extracted first, while it's still safe to do so.
- `extract_and_derive()` is submitted to a `ThreadPoolExecutor` per file,
  capped at `MAX_WORKERS = 5` concurrent workers. Since almost all the
  time in each call is spent waiting on a network response, several can
  be "in flight" together without needing more CPU.
- As each future completes (`as_completed()`), the result is saved
  **back on the main thread**, one at a time -- so the
  read-modify-write on `data/sds_records.json` never races against
  itself, even though extraction itself is concurrent.
- No review form. Fields that are normally user-chosen (`Applies To Site`,
  `Review Owners`) get the same defaults a fresh single-upload form would
  start with.

## 4. Known limitations (be aware before extending)

- **No token/cost tracking or cap.** The only guards against runaway cost
  are the 60,000-character text cap (`ai_extractor.py`) and the 3-page
  cap on pictogram detection. A batch of very large/many documents has no
  estimate shown before it runs.
- **No memory cap on a bulk batch.** All selected files' bytes are held
  in RAM at once before processing starts.
- **`data/sds_records.json` is not concurrency-safe across processes** --
  fine for one Streamlit process (as used here, writes are serialized to
  the main thread), but would corrupt under multiple app instances
  writing at once. A real deployment needs a real database.
- **`uploads/` is local disk.** Won't survive most cloud redeploys and
  doesn't work across multiple app instances. A real deployment needs
  object storage (S3 or similar).
- **No authentication, no per-user isolation.** Every record is visible
  to everyone who can open the app.

## 5. Where to change things

| Want to... | Change... |
|---|---|
| Add/remove an extracted field | `ai_extractor.FIELDS_SCHEMA` + prompt text, then thread it through `upload_page.py`'s `run_extraction()`/`extract_and_derive()`/`save_record()`/the form UI/the Summary rows, and `repository.py`'s `SUMMARY_FIELDS` |
| Change pictogram matching rules | `upload_page.PICTOGRAM_DEFS` (synonyms) or `upload_page.H_CODE_TO_PICTOGRAMS` (H-code mapping) |
| Change which model is used | `.env` -- `OPENAI_MODEL` (field extraction) / `OPENAI_OCR_MODEL` (vision calls) |
| Change bulk concurrency | `bulk_upload.MAX_WORKERS` |
| Change how many pages get scanned for pictograms | `max_pages` / `max_pictogram_pages` params on `pictogram_detector.detect_pictograms()` / `ocr.extract_text_and_pictograms()` |
