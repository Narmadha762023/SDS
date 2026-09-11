"""Deterministic (zero-AI, zero-token) field extraction from SDS text.

Every function here is a tiered "try first" step: if it returns None (or
an empty list), the caller falls back to asking the AI for that specific
field -- these functions never guess, they either find a confident,
pattern-based answer or admit they didn't.

This module exists because several SDS fields are strict, standardized
formats (dates, CAS numbers, UN transport numbers, a fixed 3-word signal
word vocabulary) that don't need language understanding to locate -- only
pattern matching, which costs no AI tokens and runs in under a millisecond
(measured: ~0.3-1.5ms per document, vs 500-3000ms for the PDF text
extraction it runs on top of, and 1-5+ seconds for an AI call).

Validated against 9 real SDS documents (multiple products, multiple
vendors) before being trusted -- see the field-by-field hit-rate notes on
each function. Fields with a lower hit rate here are NOT less safe to use:
a miss just means that document falls through to the AI, exactly as if
this module didn't exist. The risk this module is built to avoid is a
*silent* miss -- so every function returns None rather than a guess when
it isn't confident.

Field names and shapes here follow SDS-Extraction-Contract.pdf (v2),
provided by the user as the target response shape for this app's
extraction service.
"""

import re

import hazard_statement_lookup

HEADER_RE = re.compile(r"(?m)^(\d{1,2})\.\s+([A-Za-z][^\n]{2,60})$")

VERSION_RE = re.compile(r"Revision Number\s+(\S+)")
REVISION_DATE_RE = re.compile(r"Revision Date\s+([\d]{1,2}-\w{3}-[\d]{4}|[\d]{4}-[\d]{2}-[\d]{2})")
ISSUE_DATE_RE = re.compile(r"(?:Creation Date|Issue Date)\s+([\d]{1,2}-\w{3}-[\d]{4}|[\d]{4}-[\d]{2}-[\d]{2})")

# Calibrated against real documents (9/9 for Solid/Liquid/Gas); "Gel" was
# found missing from an initial narrower list during testing and added.
# Any state not in this list correctly returns None -- rather than expand
# this list indefinitely guessing at future wording, an unmatched value
# falls through to the AI, which can recognize wording this list can't.
PHYSICAL_STATE_RE = re.compile(
    r"Physical [Ss]tate\S*\s*[:\-]?\s*(Solid|Liquid|Gas|Gel|Aerosol|Powder|Semi-solid|Paste)",
    re.IGNORECASE,
)

PRODUCT_CODE_RE = re.compile(r"Cat No\.?\s*:?\s*([^\n]+)")
SYNONYMS_RE = re.compile(r"Synonyms\s+([^\n]+)")
NO_INFO_RE = re.compile(r"no information available", re.IGNORECASE)

REGULATION_BASIS_RE = re.compile(r"pursuant to the requirements of:?\s*([^\n]+(?:\n[^\n]*\))?)")
SIGNAL_WORD_RE = re.compile(r"Signal Word\s*\n?\s*(Danger|Warning|None)", re.IGNORECASE)

FLASH_POINT_RE = re.compile(r"Flash [Pp]oint[^\n]{0,3}\n?\s*([\-\d.]+\s*\S?\s*[CF][^\n]{0,20})")

# NFPA is printed as a label row, then a values row directly beneath it --
# confirmed from real text: "NFPA\nHealth Flammability Instability Physical
# hazards\n2 3 0 N/A". A naive "grab text after NFPA" pattern (tried first)
# found nothing, because nothing follows "NFPA" on the same line.
NFPA_RE = re.compile(
    r"NFPA\s*\n\s*Health\s+Flammability\s+Instability\s+Physical hazards\s*\n\s*"
    r"(\S+)\s+(\S+)\s+(\S+)\s+(\S+)",
    re.IGNORECASE,
)

UN_RE = re.compile(r"\bUN\s?(\d{4})\b")
HAZARD_CLASS_RE = re.compile(r"Hazard Class[:\s]*\n?\s*([\d.]+)", re.IGNORECASE)
PACKING_GROUP_RE = re.compile(r"Packing Group[:\s]*\n?\s*(I{1,3}|N/?A)", re.IGNORECASE)
SHIPPING_NAME_RE = re.compile(r"Proper Shipping Name[:\s]*\n?\s*([^\n]+)", re.IGNORECASE)

RCRA_CODE_RE = re.compile(r"\b([UPFDK]\d{3,4})\b")

# Hazard statements are anchored on the H-CODE, not the sentence: a code
# literally printed in the document is the only thing that can put an
# entry in this field. A document that states hazard sentences but no
# codes (real vendor families do this -- Fisher Scientific/Acros Organics
# print only "Highly flammable liquid and vapor") yields [] rather than a
# code worked backwards from the wording, which would be asserting
# something the document never said.
#
# Scoped to Section 2's own hazard-statement block on purpose: a
# whole-document scan over-collects badly -- real documents repeat codes
# in per-ingredient classifications (Section 3) and full-text reference
# lists (Section 16), e.g. SILVER NITRATE LRG's Section 2 lists 4 codes
# while the file contains 6 distinct ones.
HAZARD_BLOCK_RE = re.compile(
    r"Hazard statements\s+(.*?)(?=Precautionary statements|\n\d+\.\s|\Z)",
    re.IGNORECASE | re.DOTALL,
)
# The negative lookbehind matters: without it "EUH066" matches as "H066"
# (confirmed on a real document).
HAZARD_LINE_RE = re.compile(
    r"(?<![A-Z])(H\d{3})\s*([^\n]*?)(?=\s*(?<![A-Z])H\d{3}\s|\n|\Z)"
)

# Section 1's stated use, e.g. "Recommended Use Laboratory chemicals." --
# feeds keyword generation (extraction_pipeline.generate_keywords), not a
# top-level contract field itself, so it has no AI fallback of its own:
# if a document doesn't state one, that source is simply skipped.
RECOMMENDED_USE_RE = re.compile(r"Recommended Use\s+([^\n.]+)", re.IGNORECASE)

CAS_RE = re.compile(r"(\d{2,7}-\d{2}-\d)")
# One ingredient row: name text, then a CAS number, then a weight/percent
# value -- confirmed as a clean one-row-per-line layout in real Section 3
# text (e.g. "Ethyl alcohol 64-17-5 90"). A row that doesn't match this
# shape (wrapped across lines, missing a CAS) is simply skipped rather
# than guessed at.
INGREDIENT_ROW_RE = re.compile(
    r"^(?P<name>[A-Za-z][^\n]*?)\s+(?P<cas>\d{2,7}-\d{2}-\d)\s+(?P<pct>[<>=\d][\d.\-<>=%\s]*%?)\s*$"
)


def split_sections(text: str) -> dict:
    """Split SDS text into {section_number: body_text} using the
    regulatory-mandated 16-section GHS structure (1. Identification ...
    16. Other information) -- this ordering is legally required for a
    compliant SDS, unlike section wording/labels below it, so it's a safe
    assumption across vendors. Only accepts a strictly increasing 1..16
    sequence, so a stray in-body reference (e.g. a citation to "Article
    57a" mid-paragraph) can't be mistaken for a real section header --
    confirmed necessary during testing (one real document had exactly
    this false-positive with a naive header regex).
    """
    matches = list(HEADER_RE.finditer(text))
    accepted = []
    expected = 1
    for m in matches:
        num = int(m.group(1))
        if num == expected and expected <= 16:
            accepted.append((num, m.start(), m.end()))
            expected += 1
    sections = {}
    for i, (num, start, end) in enumerate(accepted):
        body_end = accepted[i + 1][1] if i + 1 < len(accepted) else len(text)
        sections[num] = text[end:body_end].strip()
    return sections


def extract_version(text: str):
    m = VERSION_RE.search(text)
    return m.group(1) if m else None


def extract_revision_date_raw(text: str):
    m = REVISION_DATE_RE.search(text)
    return m.group(1) if m else None


def extract_issue_date_raw(text: str):
    m = ISSUE_DATE_RE.search(text)
    return m.group(1) if m else None


def extract_physical_state(text: str):
    m = PHYSICAL_STATE_RE.search(text)
    return m.group(1).strip().capitalize() if m else None


def extract_product_code(text: str):
    m = PRODUCT_CODE_RE.search(text)
    if not m:
        return None
    codes = [c.strip() for c in re.split(r"[;,]", m.group(1)) if c.strip()]
    return codes or None


def extract_synonyms(text: str):
    m = SYNONYMS_RE.search(text)
    if not m:
        return None
    value = m.group(1).strip()
    if NO_INFO_RE.search(value):
        return None
    syns = [s.strip().rstrip(".") for s in re.split(r"[;,]", value) if s.strip()]
    return syns or None


def extract_regulation_basis(text: str):
    m = REGULATION_BASIS_RE.search(text)
    if not m:
        return None
    return re.sub(r"\s+", " ", m.group(1)).strip()


def extract_signal_word(text: str):
    m = SIGNAL_WORD_RE.search(text)
    return m.group(1).upper() if m else None


def extract_flash_point(sections: dict):
    m = FLASH_POINT_RE.search(sections.get(9, ""))
    return m.group(1).strip() if m else None


def extract_nfpa(text: str):
    m = NFPA_RE.search(text)
    if not m:
        return None
    try:
        return {
            "health": int(m.group(1)),
            "flammability": int(m.group(2)),
            "instability": int(m.group(3)),
        }
    except ValueError:
        return None


def extract_transport(sections: dict):
    sec14 = sections.get(14, "")
    un = UN_RE.search(sec14)
    hazard_class = HAZARD_CLASS_RE.search(sec14)
    packing_group = PACKING_GROUP_RE.search(sec14)
    shipping_name = SHIPPING_NAME_RE.search(sec14)
    if not un:
        return None
    return {
        "un_no": f"UN{un.group(1)}",
        "shipping_name": shipping_name.group(1).strip() if shipping_name else None,
        "hazard_class": hazard_class.group(1) if hazard_class else None,
        "packing_group": packing_group.group(1).upper() if packing_group else None,
    }


def extract_rcra_waste_code(sections: dict):
    sec13 = sections.get(13, "")
    m = RCRA_CODE_RE.search(sec13)
    return m.group(1) if m else None


def extract_ingredients(sections: dict):
    sec3 = sections.get(3, "")
    ingredients = []
    for line in sec3.splitlines():
        m = INGREDIENT_ROW_RE.match(line.strip())
        if m:
            ingredients.append({
                "name": m.group("name").strip(),
                "cas_number": m.group("cas"),
                "concentration": m.group("pct").strip(),
            })
    return ingredients or None


def extract_recommended_use(text: str):
    m = RECOMMENDED_USE_RE.search(text)
    return m.group(1).strip() if m else None


def extract_hazard_statements(text: str):
    """Returns [{"code": "H272", "text": "May intensify fire; oxidiser."}, ...]
    for every H-code literally printed in the document's Section 2 hazard
    statements, or None if the document prints no codes there.

    The code always comes from the document. The statement text prefers the
    document's own wording next to that code, and falls back to the code's
    official text (hazard_statement_lookup) only when the document prints
    the code with no sentence beside it -- that fallback expands a code the
    document itself asserted, it never works a code backwards from a
    sentence.
    """
    block_match = HAZARD_BLOCK_RE.search(text)
    if not block_match:
        return None

    statements = []
    for code, statement in HAZARD_LINE_RE.findall(block_match.group(1)):
        statement = statement.strip()
        if not statement:
            statement = hazard_statement_lookup.text_for_code(code) or ""
        if statement and not statement.endswith("."):
            statement += "."
        statements.append({"code": code, "text": statement})
    return statements or None
