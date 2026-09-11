# SDS Management

A Streamlit app with two pages:

- **Bulk Upload** -- upload many Safety Data Sheet (SDS) documents at
  once. Each is automatically processed with an LLM (text/OCR extraction,
  field extraction, GHS hazard pictogram detection) and saved directly --
  no per-document review step, built for clearing a backlog fast.
- **SDS Repository** -- a searchable list of saved SDS records built for
  quick lookup: click one to pop up a summary of the critical fields with a
  button to open the full original PDF.

**Further reading**: [docs/BACKEND_AI_GUIDE.md](docs/BACKEND_AI_GUIDE.md)
walks through the extraction pipeline step by step for anyone extending
the AI/backend logic. [docs/FRONTEND_INTEGRATION_GUIDE.md](docs/FRONTEND_INTEGRATION_GUIDE.md)
documents the exact input/output data shapes and proposes an API contract
for a separate frontend to integrate against (not implemented today --
this app has no HTTP API of its own, see that doc for why). The field set
and validation rules follow `SDS-Extraction-Contract.pdf` (v2), the target
response shape supplied by the product owner -- see `BACKEND_AI_GUIDE.md`
for which parts of it are implemented and which are a later phase.

## How it works

Two independent, user-selected processing methods feed the same AI
extraction step. The user explicitly picks one -- the app never
auto-switches between them.

**PDF Extraction** (for PDFs with a real text layer):

```
PDF -> extract text (pdfplumber) -> AI extracts fields
```

**OCR** (for scanned PDFs or images):

```
PDF/Image -> OCR via LLM vision -> AI extracts fields
```

Either way, **pictogram icon detection** also runs, since SDS hazard
pictograms are almost always graphic icons, not text -- plain text
extraction can't see them. Results are combined with any pictograms/
hazard-statement (H-)codes found in the text -- H-codes are mapped to
their official pictogram via a fixed Python lookup table, not left to the
model to apply itself.

Pictogram icon detection itself is **three tiers, cheapest and most
certain first** (`template_matcher.py`), falling back to AI only when
needed:

1. **Embedded-image template match** -- many SDS PDFs embed each
   pictogram as its own image object rather than drawing it on the page.
   When that's true, the image is pulled out directly (PyMuPDF) and
   compared against the 9 official GHS icons (`pictogram_templates/`,
   public-domain UN artwork) via perceptual-image hashing. Deterministic,
   zero AI tokens -- either it's a confident match or it's not, never a
   guess. A color-pattern filter (white background + thin red border)
   rejects other diamond-shaped graphics that aren't GHS pictograms --
   e.g. PPE icons or transport/DOT hazard labels, which use different
   color patterns entirely.
2. **OpenCV region match** -- for pictograms drawn directly into the page
   instead of embedded separately: finds red-bordered diamond-shaped
   regions on the rendered page and runs the same template match against
   each. Still zero AI tokens.
3. **AI vision fallback** -- only reached if both non-AI tiers find
   nothing (e.g. a scanned page, or a rendering style neither tier
   recognizes). This is the original approach, now a safety net rather
   than the default:
   - **PDF Extraction mode**: since that pipeline never renders page
     images for any other reason, this fallback is a dedicated vision pass
     over the first few pages (`pictogram_detector.py`).
   - **OCR mode**: OCR already renders and sends those page images to read
     the text, so when the AI fallback is needed, pictogram detection is
     folded into that *same* vision call instead of a second one
     (`ocr.extract_text_and_pictograms`) -- asking two separate questions
     about the same image in two separate calls would upload it twice for
     no reason.

Verified against a real SDS document: tiers 1-2 found the identical result
the AI vision call used to find (same two pictogram codes), at roughly
8,800 tokens for the whole document instead of ~120,000 -- because the
pictogram vision call, previously the dominant cost, didn't run at all.

### Extracted fields

`Product/Chemical Name`, `Manufacturer/Supplier`, `Emergency Contact
Phone`, `CAS Number`, `GHS Hazard Pictograms`, `Safety Hazards (physical &
health)`, `First Aid Measures`, `Personal Protection`, `Storage`,
`Physical State`, `Category`, `Version`, `Revision Date`, `Issue Date`.

The AI is instructed to never guess -- any field not present in the
document is left empty. `Safety Hazards` is explicitly instructed to
include *every* hazard statement found, not a trimmed-down subset, since
dropping one for brevity would be a real safety gap, not just a style
choice. `Version` falls back to parsing the uploaded filename (e.g. "... -
v3.0 (2023-02-15).pdf") when the document text itself has no version,
since that's a common naming convention.

### Fields not extracted by AI

`Applies To Site` and `Review Owners` default to a plain baseline (`All
Sites`, no owners) since there's no per-document review step -- correct a
specific record from the SDS Repository page if needed. `Review
Frequency` and `Next Review Due` are pre-filled only when the document
itself states a review cadence (e.g. "review: Every 3 Years") -- the due
date is then computed from that stated interval plus the revision/issue
date, never left to the model to invent.

## Bulk Upload

Pick an **Upload Source**, then **Start Bulk Processing**:

- **ZIP file** -- zip up a folder of SDS documents and upload the ZIP.
  Every matching file inside is processed, including ones in subfolders.
  Good for a whole batch at once.
- **Individual files** -- a normal multi-file picker, for when you only
  want specific documents processed rather than everything in a folder.

Both feed the exact same pipeline below -- nothing about how a file is
processed depends on which one you used. Either way, this is a normal
browser file upload, not a native OS dialog. Chosen deliberately over a
folder picker: a real browser upload works identically whether this app
is run locally or hosted for other people later, with no rebuild needed
either way.

Behind the scenes:

- Every file's bytes are read up front, on the main thread, before any
  concurrent processing starts.
- **Before any AI call is made**, every file is checked against a SHA-256
  content hash -- both of everything already saved, and of other files
  earlier in the same batch. This is an exact-content fingerprint, not a
  filename comparison, so a renamed copy of an already-saved document is
  still caught (verified: uploading the same file under a different name
  was correctly flagged), and two different documents that happen to
  share a filename are never falsely skipped. Duplicates are logged
  (⏭️) and skipped before they reach the AI, so no tokens are spent
  re-processing a file that's already saved.
- Documents are then extracted **concurrently**, a handful at a time
  (5 by default), instead of one after another -- most of the time in
  each document's AI calls is spent waiting on a network response, not
  doing local work, so several can be "in flight" together. This is what
  actually fixes the slowness of processing many documents in sequence.
  The concurrency cap keeps this from overwhelming the AI service or
  tripping its rate limits.
- A session-level guard blocks starting a second batch while one is still
  running in the same browser session -- e.g. an accidental double-click
  on "Start Bulk Processing." Without it, two overlapping runs could both
  check "has this file been saved yet?" before either had written its
  result, both see no, and both save it -- producing two identical
  records (confirmed: this happened on a real batch before the guard was
  added). This covers same-session double-firing; two genuinely separate
  browser sessions racing on the same file at the same instant isn't
  covered, and would need a cross-process lock to close entirely.
- Each document is saved the moment its own extraction finishes -- no
  review form, no summary step.
- Saving itself (writing the file + appending to the JSON record store)
  always happens back on the main thread, one document at a time, even
  though extraction runs in parallel -- so there's no risk of two
  documents' writes corrupting the shared `response/sds_records.json` file.
- One failed document doesn't stop the batch; a live log shows ✅/❌ per
  file (with each file's token count) as it finishes, plus a running
  token/cost total and a final summary once the batch completes. Every
  saved record also stores its own token usage internally (not shown in
  the SDS Repository summary popup, since that's a content-lookup view,
  not a processing-info view). See `usage_tracker.py` -- this is an
  *estimate* based on a fixed, manually-maintained price table (OpenAI
  doesn't expose pricing via the API), not an invoice-accurate figure;
  check platform.openai.com/usage for the real number.
- **`response/token_usage_log.xlsx`** -- a standalone Excel file, separate
  from the app's own UI, for tracking/billing purposes. One row is
  appended per processed document (`token_usage_log.py`, right after that
  document is saved): input/output tokens, the model's per-1M-token rate
  for each, the actual input/output/total cost incurred, model(s) used,
  and AI call count. Never rewritten for past rows -- a durable log, not a
  live report.
- **Known gaps, being upfront about them**: there's no *cap* on total
  cost for a batch (it's shown after the fact, not checked before
  starting), no retry/backoff if a call gets rate-limited (that one file
  just fails), and no size limit on how much gets held in memory at once
  -- fine for the tens of files this page is built for, but worth
  addressing before pointing it at
  hundreds+ files. And this is an in-page feature: if the browser tab
  closes or the connection drops mid-batch, the batch stops -- there's no
  background job that keeps running independently. A true "thousands of
  documents" bulk import would need that as separate infrastructure.

## SDS Repository

Lists every saved record with a search box. Search checks the extracted
fields first (product name, manufacturer, CAS number); if a term isn't
found there, it falls back to searching the document itself -- for
finding a detail that's genuinely in the SDS but wasn't one of the ~25
fields the app extracts into structured data. This is exact substring
matching, not semantic/AI search -- it finds literal text, the same as
Ctrl+F, just automated across every document, with **zero AI calls and
zero tokens** either way.

Two tiers, most precise first:

1. **`pdf_word_index.py`** -- searches word positions read directly off
   the PDF's own text layer (`pdfplumber.extract_words()`), not a
   flattened text dump. A match carries a real bounding box, so
   `pdf_render.py` can render just that page as a clean image with the
   matched phrase highlighted exactly, like a real PDF annotation --
   rather than embedding the browser's native PDF viewer (which brings
   its own toolbar and gets cluttered fast with more than one result
   open). Only built for **PDF Extraction** documents, since only a real
   text layer has positions to read -- an OCR'd/scanned page has none
   (the transcription was never tied to on-page coordinates).
2. **Page-level fallback** (`response/raw_text/`) -- plain substring
   search over the document's flat extracted text, page number only (no
   highlight box). Only saved for **OCR** documents, since `word_index`
   fully supersedes it for PDF Extraction (exact bounding box vs.
   page-only) -- saving both would just be the same text twice.

Records saved before this feature existed have neither and aren't
searchable this way until reprocessed.

**This Streamlit app is a reference implementation, not the final UI** --
the underlying capability (position-aware search, real bounding boxes) is
what matters for a real integration; a separate application would consume
`response/word_index/<id>.json` directly to draw its own highlight overlay
in whatever PDF viewer it uses, rather than re-rendering a static image the
way this reference app does for simplicity.

Clicking **View** on a record pops up a summary of the critical fields
(signal word, emergency contact, pictograms, hazards, first aid, PPE,
storage, CAS number, product code(s), synonyms, physical state, flash
point, category, regulation basis, RCRA waste code, version, dates, NFPA
rating, transport information, and an ingredients table) plus an **Open
Full SDS** button that downloads the original document from `uploads/`.

## Project structure

```
sds_app/
├── app.py                 # Entry point: page config + 2-page navigation
├── bulk_upload.py          # Bulk Upload page (concurrent extraction, no review)
├── repository.py          # SDS Repository page (search, summary popup, PDF link)
├── extraction_pipeline.py # Shared pipeline: text/OCR -> regex -> AI fields (fallback) -> derived fields -> save
├── storage_paths.py       # Shared file-storage locations + load_existing_records() --
│                           # single source of truth, dependency-free so read-only
│                           # pages don't pull in the whole AI/extraction stack
├── regex_extractor.py     # Non-AI field extraction (dates, version, CAS/ingredients,
│                           # NFPA, transport, etc.) -- tried before the AI call
├── hazard_statement_lookup.py  # Official H-code -> hazard-statement-text
│                           # table; supplies wording for a code the document
│                           # printed. No text -> code direction, so a code is
│                           # never derived from a sentence
├── pdf_extractor.py       # PDF text-layer extraction
├── ocr.py                 # OCR via LLM vision (transcribes text; falls back to
│                           # combined text+pictogram call only if non-AI
│                           # detection found nothing)
├── template_matcher.py    # Non-AI GHS pictogram detection: embedded-image
│                           # template match + OpenCV region match (tiers 1-2)
├── pictogram_templates/   # The 9 official GHS pictogram reference PNGs
│                           # (public domain UN artwork, sourced from Wikimedia
│                           # Commons) used for template matching
├── pictogram_detector.py  # AI vision fallback (tier 3) that recognizes GHS
│                           # icon shapes when the non-AI tiers find nothing
├── image_utils.py         # Shared PDF page rasterizing (PyMuPDF)
├── ai_extractor.py        # LLM field-extraction (structured JSON)
├── usage_tracker.py       # Token usage + estimated cost tracking
├── token_usage_log.py     # Appends one row per processed document to
│                           # response/token_usage_log.xlsx -- owner's own
│                           # billing/tracking reference, not shown in the app
├── pdf_word_index.py      # Position-aware search index: word bounding boxes
│                           # read straight from the PDF text layer (PDF
│                           # Extraction documents only), zero AI
├── pdf_render.py           # Renders one PDF page as a clean image, with a
│                           # real highlight over a matched phrase if given
│                           # a bounding box (PyMuPDF)
├── requirements.txt
├── .env                    # OPENAI_API_KEY (not committed with a real value)
├── docs/
│   ├── BACKEND_AI_GUIDE.md            # Pipeline internals, step by step
│   └── FRONTEND_INTEGRATION_GUIDE.md  # I/O data shapes + proposed API
├── response/
│   ├── sds_records.json   # The saved-record "database" (see below)
│   ├── token_usage_log.xlsx  # Owner's billing/tracking reference -- one
│   │                       # row per processed document, never shown in the app
│   ├── raw_text/           # One .txt per record -- page-level search
│   │                       # fallback -- OCR documents only; PDF Extraction
│   │                       # documents get word_index/ instead, never both
│   └── word_index/         # One .json per record (PDF Extraction only) --
│                           # per-page word text + bounding boxes, the
│                           # data a real integration would use to draw
│                           # its own highlight overlay
├── uploads/                # Copies of every saved document (see below)
└── README.md
```

**`uploads/` vs `response/` -- why both exist**: `uploads/` holds a
permanent copy of each original document (renamed `<record_id><ext>`),
which is what "Open Full SDS" serves. `response/sds_records.json` holds
only the *extracted information about* each document -- the AI's
structured response (product name, hazards, etc.), not the documents
themselves -- it's the searchable index the Repository page reads. The
`uploads/` copy matters because the source you uploaded from (a ZIP, in
this app) isn't kept anywhere after processing -- once a batch finishes,
`uploads/` is the *only* remaining copy of that original document.

## Setup

1. Create a virtual environment and install dependencies:

   ```bash
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. Add your OpenAI API key to `.env`:

   ```
   OPENAI_API_KEY=sk-...
   ```

3. Run the app:

   ```bash
   streamlit run app.py
   ```

## Notes

- OCR uses the LLM's vision capability to transcribe page images rather than
  a local OCR engine, so no system-level OCR binary (e.g. Tesseract) needs
  to be installed.
- The AI is explicitly instructed to return empty values instead of
  guessing when information isn't present in the SDS.
- This is a minimal demonstration app: no authentication, no database, no
  multi-user handling.
