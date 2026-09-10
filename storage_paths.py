"""Shared file-storage locations, plus the one "read the record store
safely" helper every reader of it needs.

Kept dependency-free (no AI/OpenAI/extraction imports) on purpose: a
read-only consumer like repository.py should be able to know where
sds_records.json lives without pulling in the entire extraction stack
(ai_extractor, ocr, pictogram_detector, ...) just for that -- see
BACKEND_AI_GUIDE.md's file map, which documents repository.py as
read-only with no extraction logic. Previously these constants and
load_existing_records() were duplicated between extraction_pipeline.py
and repository.py; this module is the single source of truth for both.
"""

import json
from pathlib import Path

APP_DIR = Path(__file__).parent
UPLOADS_DIR = APP_DIR / "uploads"
RESPONSE_DIR = APP_DIR / "response"
RECORDS_FILE = RESPONSE_DIR / "sds_records.json"
RAW_TEXT_DIR = RESPONSE_DIR / "raw_text"
WORD_INDEX_DIR = RESPONSE_DIR / "word_index"

UPLOADS_DIR.mkdir(exist_ok=True)
RESPONSE_DIR.mkdir(exist_ok=True)
RAW_TEXT_DIR.mkdir(exist_ok=True)
WORD_INDEX_DIR.mkdir(exist_ok=True)


def load_existing_records() -> list:
    """All records currently in the store, or [] if none/unreadable."""
    if not RECORDS_FILE.exists():
        return []
    try:
        return json.loads(RECORDS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
