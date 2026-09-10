"""Per-document token/cost tracking in a standalone Excel file, for the
app owner's own usage/billing reference -- kept separate from the app's
own UI and from sds_records.json on purpose (see repository.py, where the
token usage display was deliberately removed from the summary popup: the
Repository page is a content-lookup view, not a processing-info view).

One row is appended per processed document, right after it's saved (see
bulk_upload.py) -- never rewritten or recomputed for existing rows, so
this file is a durable log, not a live-queried report.
"""

from openpyxl import Workbook, load_workbook

from storage_paths import RESPONSE_DIR
from usage_tracker import MODEL_PRICING

LOG_FILE = RESPONSE_DIR / "token_usage_log.xlsx"

COLUMNS = [
    "Record ID", "Original Filename", "Product / Chemical Name",
    "Processed At", "Processing Method", "Model(s) Used",
    "Input Tokens", "Output Tokens", "Total Tokens",
    "Input Price (USD / 1M tokens)", "Output Price (USD / 1M tokens)",
    "Input Cost (USD)", "Output Cost (USD)", "Total Cost (USD)",
    "AI Call Count", "Has Unpriced Calls",
]


def _rates_for(models: list) -> tuple:
    """Input/output USD-per-1M rate for the row's Model(s) Used column.
    Single model (the common case): that model's real rate. Multiple
    models: shown as "N/A (mixed)" rather than averaging two different
    models' rates into a number that wouldn't mean anything -- the actual
    cost columns are still correct either way, since those come from
    usage_tracker's own per-call accounting, not from this display rate.
    """
    priced = [MODEL_PRICING[m] for m in models if m in MODEL_PRICING]
    if len(priced) == 1 and len(models) == 1:
        return priced[0]["input"], priced[0]["output"]
    return "N/A (mixed)", "N/A (mixed)"


def append_entry(record: dict, derived: dict) -> None:
    """Add one row for a just-processed document. `record` is the saved
    record dict (bulk_upload._build_bulk_record output); `derived` is the
    matching return value from extraction_pipeline.extract_and_derive(),
    whose `token_usage` holds this document's usage_tracker.summary().
    """
    usage = derived["token_usage"]
    models = usage.get("models_used") or []
    input_rate, output_rate = _rates_for(models)

    row = [
        record["id"],
        record["original_filename"],
        record["product_chemical_name"],
        record["saved_at"],
        record["processing_method"],
        ", ".join(models) if models else "",
        usage["prompt_tokens"],
        usage["completion_tokens"],
        usage["total_tokens"],
        input_rate,
        output_rate,
        round(usage["input_cost_usd"], 6),
        round(usage["output_cost_usd"], 6),
        round(usage["estimated_cost_usd"], 6),
        usage["call_count"],
        "Yes" if usage["has_unpriced_calls"] else "No",
    ]

    if LOG_FILE.exists():
        wb = load_workbook(LOG_FILE)
        ws = wb.active
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = "Token Usage"
        ws.append(COLUMNS)

    ws.append(row)
    wb.save(LOG_FILE)
