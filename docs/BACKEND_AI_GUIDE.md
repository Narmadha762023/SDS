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
| `app.py` | Entry point. Page config + 2-page navigation (`st.navigation`). No business logic. |
| `bulk_upload.py` | The only upload page. Accepts a ZIP file or individual files (both browser uploads), skips exact-content duplicates via SHA-256, runs `extraction_pipeline.py`'s functions concurrently across every new file, with no review step. |
| `repository.py` | Reads `response/sds_records.json` and renders the search list + summary popup + PDF download. Read-only; no extraction logic. |
| `extraction_pipeline.py` | The core pipeline: extraction orchestration, field derivation, saving. No Streamlit dependency -- plain Python, safe to call from a worker thread. `bulk_upload.py` is its only caller today. |
| `pdf_extractor.py` | `extract_text(pdf_bytes) -> str`. Reads a PDF's real text layer via `pdfplumber`. No AI involved. |
| `ocr.py` | Vision-based text reading for scanned/image documents. Falls back to a combined text+pictogram vision call only if non-AI pictogram detection found nothing. |
| `template_matcher.py` | Non-AI GHS pictogram detection (tiers 1-2, see step 3): embedded-image extraction + perceptual-hash template match, and OpenCV red-diamond region match. No AI involved, no tokens spent. |
| `pictogram_templates/` | The 9 official GHS pictogram reference images (public domain UN artwork, sourced from Wikimedia Commons) that `template_matcher.py` matches against. |
| `pictogram_detector.py` | AI vision fallback (tier 3) that recognizes GHS icon shapes, used only when `template_matcher.py`'s non-AI tiers find nothing. |
| `image_utils.py` | `pdf_to_page_images()` / `image_to_data_url()` -- shared by `ocr.py`, `pictogram_detector.py`, and `template_matcher.py`. |
| `ai_extractor.py` | `extract_fields(text, api_key, model) -> dict`. The single LLM call that pulls structured fields out of raw document text. |
| `response/sds_records.json` | The "database" -- a flat JSON array of saved records, read-modify-write on every save. |
| `uploads/` | The original PDF/image files, one per saved record, named `<record_id><ext>`. |

**Note on history**: this app originally had a third page, "Upload &
Extract" -- a single-document flow with an editable review form before
saving. It was removed to keep the app to Bulk Upload + SDS Repository
only. The pipeline logic it used to call directly now lives in
`extraction_pipeline.py` (it was extracted out of what used to be
`upload_page.py`), which is why that module has no Streamlit dependency
even though it's the heart of the app.

## 2. Step by step: one document, start to finish

This is what happens for each file in a batch on the **Bulk Upload**
page -- i.e. every call to `extraction_pipeline.extract_and_derive()`.

### Step 1 -- Get raw text out of the document

The user picks one of two methods for the whole batch (never
auto-selected):

- **PDF Extraction** -- `pdf_extractor.extract_text(file_bytes)`. Reads the
  PDF's real text layer via `pdfplumber`, page by page, joined with
  `--- Page N ---` separators. Pure text extraction, no AI call.
- **OCR** -- `ocr.extract_text_and_pictograms(file_bytes, filename, api_key, model)`.
  Renders each page to a PNG (`image_utils.pdf_to_page_images`, 200 DPI)
  and sends it to the vision model. See step 3 for why this call also
  returns pictogram codes.

If the resulting text is empty/whitespace-only, extraction stops there --
`extract_and_derive()` returns `(None, None)` and the caller (`bulk_upload.py`)
logs that file as failed rather than saving anything.

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
neither of the above steps can see them. Three tiers run in order,
cheapest and most certain first, in both `_detect_pictogram_icons()`
(`extraction_pipeline.py`, PDF Extraction path) and
`ocr.extract_text_and_pictograms()` (OCR path):

1. **`template_matcher.match_embedded_images()`** -- many SDS PDFs embed
   each pictogram as its own image object (inserted by whatever software
   generated the SDS) rather than drawing it directly on the page.
   PyMuPDF's `page.get_images()` pulls those out; each candidate is
   filtered by color pattern (a real GHS pictogram is a white background
   with a thin red diamond border -- calibrated against real examples:
   ~77-80% white / ~14% red, vs. a PPE icon's ~33% white / 0% red, or a
   transport/DOT hazard label's ~50% white / ~42% red -- these bounds
   cleanly separate all three) then compared to the 9 reference icons in
   `pictogram_templates/` via perceptual image hashing (`imagehash.phash`,
   distance <= 10 counts as a match). Fully deterministic, zero AI calls.
2. **`template_matcher.match_page_regions()`** -- for pictograms drawn
   directly into the page content instead of embedded as a separate image
   object. Runs on the same rendered page images `image_utils.pdf_to_page_images()`
   already produces: OpenCV (`cv2.inRange` on an HSV red mask +
   `findContours`) finds diamond-shaped red-bordered regions, crops each,
   and runs the same template match. Still zero AI calls.
3. **AI vision fallback** -- only reached if both tiers above find
   nothing (e.g. a scanned page, or an icon rendering style neither tier
   recognizes):
   - **PDF Extraction path**: `_detect_pictogram_icons()` calls
     `pictogram_detector.detect_pictograms()` as its own extra vision call
     over the first 3 pages -- since this pipeline never rendered any page
     images in step 1, there's nothing to piggyback on.
   - **OCR path**: folded into the *same* call as step 1
     (`ocr.extract_text_and_pictograms`). Since OCR already renders and
     sends the first few pages as images to read their text, asking a
     second, separate question about the same images would upload them
     twice for no reason -- `ocr._transcribe_and_detect()` sends one
     combined prompt per page asking for both the transcribed text and the
     visible pictogram codes in one JSON response. This combined call is
     now skipped entirely (falls back to the plain transcribe-only prompt)
     if tiers 1-2 already found a result.

All three tiers fail soft: an exception at any tier falls through to the
next one rather than raising, and the final AI tier degrades to `[]` (no
pictograms found) rather than taking down an otherwise-successful
extraction.

**Verified on a real document**: tiers 1-2 found the identical pictogram
codes the AI vision call used to find, while the document's total token
usage dropped from 120,285 to 8,805 -- because the pictogram vision call
(previously the dominant cost, since a full-page image at `detail: high`
is tens of thousands of tokens) never needed to run at all. See
`template_matcher.py`'s module docstring for the full rationale, including
why `detail: "low"` was tried and rejected first (real-document testing
showed it changed the detected pictograms on 4 of 5 documents -- an
accuracy trade-off, not a free optimization, whereas template matching is
either an exact match or a clean fallback to the original AI behavior).

### Step 4 -- Resolve pictograms (three signals merged)

`extraction_pipeline.compute_derived_fields()` builds the final pictogram
set from **three independent sources**, unioned together:

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
  used as a fallback, and flagged in the Repository summary as
  filename-sourced.
- **Date parsing**: `parse_date_safe()` runs `dateutil`'s fuzzy parser on
  whatever date string the AI extracted. Unparseable text is left as
  `None` (not guessed).
- **Review cadence**: `parse_review_interval_years()` regexes a stated
  interval (e.g. "Every 3 Years") out of the AI's
  `review_frequency_stated`. If found, `next_review_due` is *computed*
  (`revision_date + relativedelta(years=N)`) in Python -- never left to
  the model to do date arithmetic.

### Step 6 -- Save

`bulk_upload._build_bulk_record()` assembles the final record dict (see
the schema in `FRONTEND_INTEGRATION_GUIDE.md`) from the `fields` +
`derived` output of `extract_and_derive()`, plus plain baseline defaults
for the fields that would normally be user-chosen (`applies_to_site`,
`review_owners`) since there's no review step. Then:

- The original file bytes are written to `uploads/<record_id><ext>`.
- `extraction_pipeline.append_record_to_store()` reads
  `response/sds_records.json`, appends the new record, writes the whole file
  back.

## 3. Concurrency -- how the whole batch actually runs

`bulk_upload.py` calls `extraction_pipeline.extract_and_derive()` once
per file -- there is no separate/different AI logic for a batch of many
vs. one document. What makes it "bulk" is orchestration:

- The user picks an **Upload Source** -- ZIP file or individual files --
  both are normal browser uploads (never a native OS dialog, so this
  works the same whether the app is run locally or hosted later) and
  both end up producing the same `[(filename, file_bytes), ...]` list,
  so everything downstream is identical regardless of which was used.
  `bulk_upload._list_matching_entries()` walks every entry in a ZIP (at
  any depth) matching the selected method's extensions, skipping
  directories and `__MACOSX`/dotfile junk; for individual files,
  `st.file_uploader(accept_multiple_files=True)` needs no such filtering
  since its `type=` restricts selection up front. Either way, every
  file's bytes are read up front, on the main thread, before any
  concurrent processing starts.
- **Before any AI call**, `bulk_upload._split_new_and_duplicate()`
  computes a SHA-256 of each file's bytes (`extraction_pipeline.compute_file_hash()`)
  and checks it against `extraction_pipeline.load_existing_hashes()`
  (every already-saved record's hash) and against other files earlier in
  the same batch. A match means an exact-content duplicate -- it's
  logged and skipped, never reaching `extract_and_derive()`, so no
  tokens are spent on it. This is a content fingerprint, not a filename
  comparison: verified that a byte-identical file under a different name
  is still caught, and that a fresh batch with only already-saved
  content correctly finds nothing new to process.
- `extract_and_derive()` is submitted to a `ThreadPoolExecutor` per
  non-duplicate file,
  capped at `MAX_WORKERS = 5` concurrent workers. Since almost all the
  time in each call is spent waiting on a network response, several can
  be "in flight" together without needing more CPU.
- As each future completes (`as_completed()`), the result is saved
  **back on the main thread**, one at a time -- so the
  read-modify-write on `response/sds_records.json` never races against
  itself, even though extraction itself is concurrent.
- No review form, ever. Fields that would normally be user-chosen
  (`Applies To Site`, `Review Owners`) get a plain baseline default.

## 4. Known limitations (be aware before extending)

- **No token/cost cap.** Usage is tracked and shown per-document and per-batch
  (`usage_tracker.py`), but nothing stops a batch before it runs -- the
  only upstream guards against runaway cost are the 60,000-character text
  cap (`ai_extractor.py`) and the 3-page cap on pictogram detection.
- **No memory cap on a bulk batch.** All selected files' bytes are held
  in RAM at once before processing starts.
- **`response/sds_records.json` is not concurrency-safe across processes** --
  fine for one Streamlit process (as used here, writes are serialized to
  the main thread), but would corrupt under multiple app instances
  writing at once. A real deployment needs a real database.
- **`uploads/` is local disk.** Won't survive most cloud redeploys and
  doesn't work across multiple app instances. A real deployment needs
  object storage (S3 or similar).
- **No authentication, no per-user isolation.** Every record is visible
  to everyone who can open the app.
- **No per-document review.** Since the Upload & Extract page was
  removed, there's no UI path to review/correct a record before it's
  saved -- only after, via the SDS Repository page's read-only summary
  (it doesn't currently support editing a saved record either).

## 5. Where to change things

| Want to... | Change... |
|---|---|
| Add/remove an extracted field | `ai_extractor.FIELDS_SCHEMA` + prompt text, then thread it through `extraction_pipeline.py`'s `compute_derived_fields()`, `bulk_upload.py`'s `_build_bulk_record()`, and `repository.py`'s `SUMMARY_FIELDS` |
| Change pictogram matching rules | `extraction_pipeline.PICTOGRAM_DEFS` (synonyms) or `extraction_pipeline.H_CODE_TO_PICTOGRAMS` (H-code mapping) |
| Change which model is used | `.env` -- `OPENAI_MODEL` (field extraction) / `OPENAI_OCR_MODEL` (vision calls) |
| Change bulk concurrency | `bulk_upload.MAX_WORKERS` |
| Change how many pages get scanned for pictograms | `extraction_pipeline.PICTOGRAM_DETECTION_MAX_PAGES`, and the `max_pictogram_pages` param on `ocr.extract_text_and_pictograms()` |
| Change the non-AI pictogram match sensitivity | `template_matcher.py` -- `HASH_MAX_DISTANCE` (how close a stricter/looser match counts), or `WHITE_MIN`/`RED_MIN`/`RED_MAX` (the color-pattern filter that rejects non-GHS diamond graphics) |
| Change what counts as a duplicate | `bulk_upload._split_new_and_duplicate()` (currently: exact SHA-256 content match only -- no filename/CAS-number matching, deliberately, since either would risk false positives) |
