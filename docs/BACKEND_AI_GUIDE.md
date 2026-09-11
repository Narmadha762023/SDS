# Backend / AI Engineer Guide

Audience: an engineer picking up this codebase who needs to understand how
data flows from an uploaded PDF to a saved record, what each AI call does,
and where to change things.

This app has no separate backend service today -- `streamlit run app.py`
*is* the whole application (UI + logic + storage in one process). Read
[FRONTEND_INTEGRATION_GUIDE.md](FRONTEND_INTEGRATION_GUIDE.md) for the
data shapes and a proposed API layer if you need to expose this to a
separate frontend.

**Note on field set**: the record shape (this file's step 2 and the field
map below) reflects `SDS-Extraction-Contract.pdf` (v2), supplied by the
product owner as the target response shape -- new fields, the regex-first
extraction tiers, and the validation notes throughout this doc trace back
to that document. Fields it lists that aren't implemented yet
(`precautionary_statements` pairs, `disposal`, `regulatory_flags`,
`language`, the `status`/`confidence`/`warnings` envelope) are a
deliberate later phase -- see this file's "Known limitations" section.
`keywords` **is** implemented -- see step 5. `hazard_statements` **is**
implemented -- see step 4a.

## 1. File map

| File | Responsibility |
|---|---|
| `app.py` | Entry point. Page config + 2-page navigation (`st.navigation`). No business logic. |
| `bulk_upload.py` | The only upload page. Accepts a ZIP file or individual files (both browser uploads), skips exact-content duplicates via SHA-256, runs `extraction_pipeline.py`'s functions concurrently across every new file, with no review step. |
| `repository.py` | Reads `response/sds_records.json` and renders the search list + summary popup + PDF download. Read-only; no extraction logic. |
| `extraction_pipeline.py` | The core pipeline: extraction orchestration, field derivation, saving. No Streamlit dependency -- plain Python, safe to call from a worker thread. `bulk_upload.py` is its only caller today. |
| `pdf_extractor.py` | `extract_text(pdf_bytes) -> str`. Reads a PDF's real text layer via `pdfplumber`. No AI involved. |
| `regex_extractor.py` | Non-AI field extraction: fixed-format fields (dates, version, CAS-based ingredients, NFPA rating, transport codes, etc.) found by pattern matching, zero AI tokens. Tried before the AI call for every document; a field it doesn't find for a given document is asked of the AI instead (see step 2a). |
| `hazard_statement_lookup.py` | Official H-code -> hazard-statement-text table (UN GHS/EU CLP). Forward direction only: supplies official wording for a code the document printed. Deliberately offers no text -> code direction -- see step 4a. |
| `ocr.py` | Vision-based text reading for scanned/image documents. Falls back to a combined text+pictogram vision call only if non-AI pictogram detection found nothing. |
| `template_matcher.py` | Non-AI GHS pictogram detection (tiers 1-2, see step 3): embedded-image extraction + perceptual-hash template match, and OpenCV red-diamond region match. No AI involved, no tokens spent. |
| `pictogram_templates/` | The 9 official GHS pictogram reference images (public domain UN artwork, sourced from Wikimedia Commons) that `template_matcher.py` matches against. |
| `pictogram_detector.py` | AI vision fallback (tier 3) that recognizes GHS icon shapes, used only when `template_matcher.py`'s non-AI tiers find nothing. |
| `image_utils.py` | `pdf_to_page_images()` / `image_to_data_url()` -- shared by `ocr.py`, `pictogram_detector.py`, and `template_matcher.py`. |
| `ai_extractor.py` | `extract_fields(text, api_key, model) -> dict`. The single LLM call that pulls structured fields out of raw document text. |
| `pdf_word_index.py` | Non-AI, position-aware search index: per-page word text + bounding boxes read straight from the PDF text layer (`pdfplumber`). PDF Extraction documents only. |
| `pdf_render.py` | Renders one PDF page as a clean image (PyMuPDF), optionally with a real highlight annotation over a matched phrase's bounding box. |
| `storage_paths.py` | Shared file-storage locations (`UPLOADS_DIR`, `RECORDS_FILE`, `RAW_TEXT_DIR`, `WORD_INDEX_DIR`) and `load_existing_records()` -- the single source of truth for both `extraction_pipeline.py` and `repository.py`, kept dependency-free so the read-only Repository page doesn't need the AI/extraction stack just to know where files live. |
| `usage_tracker.py` | `UsageTracker` -- accumulates per-call token usage across however many AI calls make up one document, with `input_cost_usd()`/`output_cost_usd()` split by `MODEL_PRICING`. |
| `token_usage_log.py` | Appends one row per processed document to `response/token_usage_log.xlsx` -- a standalone billing/tracking reference for the app owner, separate from the app's own UI (see step 6). |
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

### Step 2a -- Regex tries first, zero AI tokens

`extraction_pipeline._apply_regex_extraction(raw_text)` runs before the AI
call, for every document. It uses `regex_extractor.py` to look for
fixed-format fields that don't need language understanding to locate --
just a known label and shape:

- `version`, `revision_date`, `issue_date` -- exact-label patterns
  (`Revision Number N`, `Revision Date ...`), validated 9/9 against real
  documents.
- `physical_state` -- a small fixed vocabulary (Solid/Liquid/Gas/Gel/...),
  validated 8/9 (one miss during testing -- "Gel" wasn't in the initial
  list -- fixed and now falls through to AI for any wording still outside
  the list, rather than expanding the list indefinitely on guesses).
- `product_code`, `synonyms`, `regulation_basis`, `signal_word`,
  `flash_point`, `nfpa`, `transport`, `rcra_waste_code`, `ingredients` --
  from `SDS-Extraction-Contract.pdf` v2. Hit rates vary per field (some
  fields are legitimately absent from many documents, e.g. `rcra_waste_code`
  only applies to EPA-listed hazardous chemicals) -- see the module
  docstring in `regex_extractor.py` for the tested rate on each. A lower
  hit rate doesn't mean less safe: a miss just falls through to the AI for
  that field on that document, same as if this tier didn't run.
- `split_sections()` splits text by the regulatory-mandated 16-section GHS
  structure (1. Identification ... 16. Other information) -- this
  numbering/order is legally required for a compliant SDS, so it's a safe
  structural assumption across vendors, unlike the label wording within
  each section.

Every function here returns `None` (or an empty list) rather than a
guess when it isn't confident -- the field-gap checklist below (step 2b)
is what actually fills the rest.

### Step 2b -- AI fills in whatever regex didn't find

`ai_extractor.extract_fields(raw_text, api_key, model, skip_fields=...)`:

- `skip_fields` is the set of keys step 2a already found for this
  document -- those are dropped from the prompt entirely, not just
  ignored in the response. A document where regex finds most fields gets
  a smaller prompt *and* a smaller response, not just a discarded answer.
  Measured on a real document: total tokens dropped from 8,805 (pictogram
  tiers already optimized) to 4,508 once these fields were removed from
  what's asked.
- Sends the raw text (truncated to the first 60,000 characters) to the
  chat completions API with `temperature=0` and
  `response_format={"type": "json_object"}`.
- The prompt (`FIELD_PROMPTS` in `ai_extractor.py`, one snippet per field,
  joined dynamically based on what's still needed) gives per-field
  instructions -- e.g. `version` explains what labels count as a match
  ("Version", "Rev.", etc.); `safety_hazards` is explicitly told to
  include *every* hazard statement found, not a trimmed subset, since
  dropping one would be a real safety gap.
- The system prompt enforces the core rule: never guess, return `""` /
  `[]`/`{}` for anything not literally in the text.
- The raw model response is parsed as JSON and validated key-by-key
  against `FIELDS_SCHEMA` -- any missing/malformed key falls back to the
  schema's empty default rather than propagating a bad shape.
- The pipeline then overlays step 2a's regex-found values on top of the
  AI's response (`fields.update(regex_fields)`), so a regex-found value
  always wins for its own key -- the AI was never even asked about it.

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
2. **H-code lookup (deterministic)** -- codes from `hazard_statement_codes`
   (AI-extracted) plus every `hazard_statements[].code` (step 4a) are run
   through `H_CODE_TO_PICTOGRAMS`, a fixed Python dict encoding the
   official UN GHS H-code -> pictogram table. **This mapping is applied in
   code, not asked of the LLM** -- an earlier version asked the model to
   do this lookup itself and it missed most codes even with the full
   table in the prompt; moving it to a deterministic dict fixed that
   completely (verified: 5/5 H-codes correctly resolved after the change,
   vs 1/5 before).
3. **Vision icon detection** -- the codes from step 3.

### Step 4a -- `hazard_statements`: anchored on the document's own H-codes

`hazard_statements` (array of `{code, text}`) is **100% non-AI** --
`regex_extractor.extract_hazard_statements()` produces it, there is no
`FIELD_PROMPTS` entry for it, and the AI is never asked. The rule is:

> An H-code literally printed in the document is the only thing that can
> put an entry in this field. No codes printed -> `[]`.

- **Code present** -> entry created. Text prefers the document's own
  wording next to that code; if the document prints a bare code with no
  sentence beside it (real pattern: per-ingredient tables, Section 16
  reference lists), `hazard_statement_lookup.text_for_code()` supplies the
  official text. That expands a code the document itself asserted.
- **No codes present** -> `[]`. Some vendors (Fisher Scientific/Acros
  Organics) print only sentences -- "Highly flammable liquid and vapor" --
  with no H-code character anywhere on the page. Those yield nothing here.
  The sentences are not lost: `safety_hazards` still carries them.

**Why no reverse (sentence -> code) lookup, even a deterministic one.**
An earlier version had both an AI tier and a phrase->code table. The AI
tier was removed after a confirmed failure: told explicitly not to invent
a code that wasn't literally present, it still returned
H225/H319/H335/H336/H373 for a document verified (direct text search) to
contain zero literal H-code characters -- it recognized the standard GHS
phrasing from training and filled the codes in from memory. Same failure
mode as the pictogram H-code lookup above. The phrase->code table was then
removed too, because deriving a code from wording asserts a classification
the document never printed, regardless of how the derivation happens.

**Scoping matters.** Extraction is confined to Section 2's hazard-statement
block. A whole-document scan over-collects: `SILVER NITRATE LRG` lists 4
codes in Section 2 but contains 6 distinct ones across per-ingredient and
reference sections. A negative lookbehind also stops `EUH066` matching as
`H066` -- a real false positive seen on `ACETONE LRG`.

Verified on real documents: 19/20 extraction (the 1 miss is a consumer
product with genuinely zero hazard statements), Fisher-style documents
correctly yield `[]`, and unit tests confirm the bare-code, sentences-only,
and `EUH` cases all behave as described.

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
- **`keywords`** (`generate_keywords()`, in `extract_and_derive()` after
  `compute_derived_fields()`): 5-15 lowercased, de-duplicated search tags,
  built entirely from fields already extracted -- product name & synonyms,
  the pictogram labels already resolved in step 4, signal word, physical
  state, and (when Section 1 states one) `regex_extractor.extract_recommended_use()`.
  No AI call -- every term here is re-surfacing data this function already
  has, so a model call would just spend tokens restating it. Deliberately
  does NOT attempt chemical-family classification (the contract's other
  keyword source, e.g. "aromatic hydrocarbon") -- that's a real chemistry
  judgment, not something findable by pattern in the text, and guessing it
  would be exactly the kind of invented value this app avoids everywhere
  else.

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
- `token_usage_log.append_entry()` appends one row to
  `response/token_usage_log.xlsx` for this document -- input/output
  tokens, the model's per-1M-token rate for each, the actual
  input/output/total cost incurred, model(s) used, call count. A separate
  file from `sds_records.json` and never surfaced in the app's own UI --
  purely a billing/tracking reference for whoever owns the OpenAI account.
  Wrapped in try/except so a failure here (e.g. the file open elsewhere)
  never blocks the actual record save.

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

- **`SDS-Extraction-Contract.pdf` v2 fields not yet implemented**: structured
  `precautionary_statements` (paired code+text, deduped), `disposal`,
  `regulatory_flags` (Prop 65/SARA 313/CERCLA/REACH/TSCA), `language`, and
  the `status`/`overall_confidence`/`needs_review`/`warnings` response
  envelope. (`keywords` is implemented -- step 5. `hazard_statements` is
  implemented -- step 4a, including the vendors that only state a
  hazard-statement sentence with no H-code anywhere on the page.) The
  fields above remain unimplemented because testing showed real accuracy
  risk for regex: section-boundary text splitting disagreed with AI
  extraction on 3 of 9 real documents for free-text fields like
  `first_aid_measures`/`storage`/`disposal`. `next_review_due`'s
  fallback-when-no-cadence-stated policy is also still open -- see the
  field-gap discussion for the "invent a default vs. flag as
  needs-review" trade-off.
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
| Add/remove an AI-extracted field | `ai_extractor.FIELDS_SCHEMA` + `FIELD_PROMPTS`, then thread it through `extraction_pipeline.py`'s `compute_derived_fields()`, `bulk_upload.py`'s `_build_bulk_record()`, and `repository.py`'s `SUMMARY_FIELDS` |
| Add/change a regex-first field | `regex_extractor.py` (new extractor function) + `extraction_pipeline._apply_regex_extraction()` (wire it in) -- also add the same key to `ai_extractor.FIELDS_SCHEMA`/`FIELD_PROMPTS` so it still has an AI fallback for documents the regex misses. Test against real documents before trusting -- see the module docstring's rationale on why some fields (H-codes, free-text safety fields) were deliberately NOT moved here |
| Change pictogram matching rules | `extraction_pipeline.PICTOGRAM_DEFS` (synonyms) or `extraction_pipeline.H_CODE_TO_PICTOGRAMS` (H-code mapping) |
| Add/fix an official H-code phrase | `hazard_statement_lookup.H_CODE_TO_TEXT` (add the code+text) and `_SPELLING_VARIANTS` (British/American wording differences) |
| Change which model is used | `.env` -- `OPENAI_MODEL` (field extraction) / `OPENAI_OCR_MODEL` (vision calls) |
| Change bulk concurrency | `bulk_upload.MAX_WORKERS` |
| Change how many pages get scanned for pictograms | `extraction_pipeline.PICTOGRAM_DETECTION_MAX_PAGES`, and the `max_pictogram_pages` param on `ocr.extract_text_and_pictograms()` |
| Change the non-AI pictogram match sensitivity | `template_matcher.py` -- `HASH_MAX_DISTANCE` (how close a stricter/looser match counts), or `WHITE_MIN`/`RED_MIN`/`RED_MAX` (the color-pattern filter that rejects non-GHS diamond graphics) |
| Change what counts as a duplicate | `bulk_upload._split_new_and_duplicate()` (currently: exact SHA-256 content match only -- no filename/CAS-number matching, deliberately, since either would risk false positives) |
