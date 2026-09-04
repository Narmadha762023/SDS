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
this app has no HTTP API of its own, see that doc for why).

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

- **PDF Extraction mode**: since that pipeline never renders page images,
  pictogram detection is a separate vision pass over the first few pages
  (`pictogram_detector.py`).
- **OCR mode**: OCR already renders and sends those page images to read
  the text, so pictogram detection is folded into that *same* vision call
  instead of a second one (`ocr.extract_text_and_pictograms`) -- asking
  two separate questions about the same image in two separate calls would
  upload it twice for no reason, roughly doubling vision cost on those
  pages. One call now asks for both the transcribed text and the visible
  pictogram icons together.

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

Zip up your SDS documents and upload that ZIP file, pick one processing
method for the whole batch, then **Start Bulk Processing**. Every matching
file inside the ZIP is processed, including ones in subfolders. Behind the
scenes:

- This is a normal browser file upload (the ZIP is just one file) -- not
  a native OS dialog. Chosen deliberately over a folder picker: a real
  browser upload works identically whether this app is run locally or
  hosted for other people later, with no rebuild needed either way.
- Every matching entry's bytes are read out of the ZIP up front, on the
  main thread, before any concurrent processing starts.
- Documents are then extracted **concurrently**, a handful at a time
  (5 by default), instead of one after another -- most of the time in
  each document's AI calls is spent waiting on a network response, not
  doing local work, so several can be "in flight" together. This is what
  actually fixes the slowness of processing many documents in sequence.
  The concurrency cap keeps this from overwhelming the AI service or
  tripping its rate limits.
- Each document is saved the moment its own extraction finishes -- no
  review form, no summary step.
- Saving itself (writing the file + appending to the JSON record store)
  always happens back on the main thread, one document at a time, even
  though extraction runs in parallel -- so there's no risk of two
  documents' writes corrupting the shared `response/sds_records.json` file.
- One failed document doesn't stop the batch; a live log shows ✅/❌ per
  file (with each file's token count) as it finishes, plus a running
  token/cost total and a final summary once the batch completes. Every
  saved record also stores its own token usage, visible later in the SDS
  Repository summary popup. See `usage_tracker.py` -- this is an
  *estimate* based on a fixed, manually-maintained price table (OpenAI
  doesn't expose pricing via the API), not an invoice-accurate figure;
  check platform.openai.com/usage for the real number.
- **Known gaps, being upfront about them**: there's no *cap* on total
  cost for a batch (it's shown after the fact, not checked before
  starting), no retry/backoff if a call gets rate-limited (that one file
  just fails), no duplicate-check against existing records (re-running a
  folder after a partial failure re-saves everything, including files
  that already succeeded, as new duplicate records), and no size limit on
  how much gets held in memory at once -- fine for the tens of files this
  page is built for, but worth addressing before pointing it at
  hundreds+ files. And this is an in-page feature: if the browser tab
  closes or the connection drops mid-batch, the batch stops -- there's no
  background job that keeps running independently. A true "thousands of
  documents" bulk import would need that as separate infrastructure.

## SDS Repository

Lists every saved record with a search box (matches product name,
manufacturer, or CAS number). Clicking **View** on a record pops up a
summary of the critical fields (emergency contact, pictograms, hazards,
first aid, PPE, storage, category, version, dates) plus an **Open Full
SDS** button that downloads the original document from `uploads/`.

## Project structure

```
sds_app/
├── app.py                 # Entry point: page config + 2-page navigation
├── bulk_upload.py          # Bulk Upload page (concurrent extraction, no review)
├── repository.py          # SDS Repository page (search, summary popup, PDF link)
├── extraction_pipeline.py # Shared pipeline: text/OCR -> AI fields -> derived fields -> save
├── pdf_extractor.py       # PDF text-layer extraction
├── ocr.py                 # OCR via LLM vision (transcribes text; combined with
│                           # pictogram detection in one call per page)
├── pictogram_detector.py  # LLM vision pass that recognizes GHS icon shapes
│                           # (used standalone for PDF Extraction mode)
├── image_utils.py         # Shared PDF page rasterizing (PyMuPDF)
├── ai_extractor.py        # LLM field-extraction (structured JSON)
├── usage_tracker.py       # Token usage + estimated cost tracking
├── requirements.txt
├── .env                    # OPENAI_API_KEY (not committed with a real value)
├── docs/
│   ├── BACKEND_AI_GUIDE.md            # Pipeline internals, step by step
│   └── FRONTEND_INTEGRATION_GUIDE.md  # I/O data shapes + proposed API
├── response/
│   └── sds_records.json   # The saved-record "database" (see below)
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
