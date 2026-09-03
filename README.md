# SDS Management

A Streamlit app with three pages:

- **Upload & Extract** -- upload one Safety Data Sheet (SDS), automatically
  extract key fields with an LLM, review/edit them, and save the record
  together with the original document.
- **Bulk Upload** -- upload many SDS documents at once. Same extraction
  pipeline, run concurrently across documents, with no per-document review
  step -- each is extracted and saved directly.
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

Two independent, user-selected pipelines feed the same AI extraction step.
The user explicitly picks one -- the app never auto-switches between them.

**PDF Extraction** (for PDFs with a real text layer):

```
Upload SDS PDF -> extract text (pdfplumber) -> AI extracts fields -> form
```

**OCR** (for scanned PDFs or images):

```
Upload SDS PDF/Image -> OCR via LLM vision -> AI extracts fields -> form
```

OCR only runs when the user explicitly selects it.

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

### AI-extracted fields

Kept prominent in the review form, since these are the parts a front-line
worker needs at a glance: `Product/Chemical Name*`, `Manufacturer/Supplier`,
`Emergency Contact Phone`, `GHS Hazard Pictograms`, `Safety Hazards
(physical & health)`, `First Aid Measures`, `Personal Protection`,
`Storage`.

Lower-priority document metadata is extracted too but tucked into a
collapsed "Document Details" section to keep the review quick: `CAS
Number`, `Physical State`, `Category`, `Version`, `Revision Date`, `Issue
Date`. `Version` falls back to parsing the uploaded filename (e.g. "... -
v3.0 (2023-02-15).pdf") when the document text itself has no version, since
that's a common naming convention -- the form shows a caption when this
fallback is used.

The AI is instructed to never guess -- any field not present in the
document is left empty. `Safety Hazards` is explicitly instructed to
include *every* hazard statement found, not a trimmed-down subset, since
dropping one for brevity would be a real safety gap, not just a style
choice.

### Manually-selected fields (not extracted by AI)

`Applies To Site` and `Review Owners` are always user-chosen. `Review
Frequency` and `Next Review Due` are pre-filled only when the document
states a review cadence (e.g. "review: Every 3 Years") -- the due date is
then computed from that stated interval plus the revision/issue date, never
left to the model to invent -- and remain fully editable either way.

## Bulk Upload

For getting many documents in fast (e.g. clearing a backlog) rather than
reviewing one at a time. Select multiple files, pick one processing method
for the whole batch, and click **Start Bulk Processing**. Behind the
scenes:

- Every file's bytes are read into memory up front, on the main page --
  not because of any special caching, just because the uploaded-file
  objects Streamlit hands back aren't safe to use from a background
  worker, so the plain bytes are extracted first while it's safe to do so.
- Documents are then extracted **concurrently**, a handful at a time
  (5 by default), instead of one after another -- most of the time in
  each document's AI calls is spent waiting on a network response, not
  doing local work, so several can be "in flight" together. This is what
  actually fixes the slowness of processing many documents in sequence.
  The concurrency cap keeps this from overwhelming the AI service or
  tripping its rate limits.
- Each document is saved the moment its own extraction finishes -- no
  review form, no summary step. `Applies To Site` and `Review Owners`
  default the same way a fresh form would; fix a specific record
  afterward from the SDS Repository page if needed.
- Saving itself (writing the file + appending to the JSON record store)
  always happens back on the main thread, one document at a time, even
  though extraction runs in parallel -- so there's no risk of two
  documents' writes corrupting the shared `data/sds_records.json` file.
- One failed document doesn't stop the batch; a live log shows ✅/❌ per
  file as each one finishes, with a final saved/failed count.
- **Known gaps, being upfront about them**: there's currently no
  estimate or cap on total AI token/cost usage for a batch, and no size
  limit on how much gets held in memory at once -- fine for the tens of
  files this page is built for, but worth addressing before pointing it
  at hundreds+ files. And this is an in-page feature: if the browser tab
  closes or the connection drops mid-batch, the batch stops -- there's no
  background job that keeps running independently. A true "thousands of
  documents" bulk import would need that as separate infrastructure.

## Project structure

```
sds_app/
├── app.py                 # Entry point: page config + 3-page navigation
├── upload_page.py         # Upload & Extract page (extraction, review form, save)
├── bulk_upload.py         # Bulk Upload page (concurrent extraction, no review)
├── repository.py          # SDS Repository page (search, summary popup, PDF link)
├── pdf_extractor.py       # PDF text-layer extraction
├── ocr.py                 # OCR via LLM vision (transcribes text; combined with
│                           # pictogram detection in one call per page)
├── pictogram_detector.py  # LLM vision pass that recognizes GHS icon shapes
│                           # (used standalone for PDF Extraction mode)
├── image_utils.py         # Shared PDF page rasterizing (PyMuPDF)
├── ai_extractor.py        # LLM field-extraction (structured JSON)
├── requirements.txt
├── .env                    # OPENAI_API_KEY (not committed with a real value)
├── docs/
│   ├── BACKEND_AI_GUIDE.md            # Pipeline internals, step by step
│   └── FRONTEND_INTEGRATION_GUIDE.md  # I/O data shapes + proposed API
└── README.md
```

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

## Saving

Clicking **Save SDS**:

- Validates required fields (`Product/Chemical Name`, `SDS Document`).
- Copies the original uploaded file into `uploads/`.
- Appends the reviewed record (including the manually-selected review
  fields) to `data/sds_records.json`.
- Shows a success message with the saved record ID, and the record
  immediately appears in the SDS Repository page.

## SDS Repository

Lists every saved record with a search box (matches product name,
manufacturer, or CAS number). Clicking **View** on a record pops up a
summary of the critical fields (emergency contact, pictograms, hazards,
first aid, PPE, storage) plus an **Open Full SDS** button that downloads
the original document from `uploads/`.

## Notes

- OCR uses the LLM's vision capability to transcribe page images rather than
  a local OCR engine, so no system-level OCR binary (e.g. Tesseract) needs
  to be installed.
- The AI is explicitly instructed to return empty values instead of
  guessing when information isn't present in the SDS.
- This is a minimal demonstration app: no authentication, no database, no
  multi-user handling.
