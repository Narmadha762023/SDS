"""LLM-based SDS field extraction.

Takes raw SDS text (from either the PDF-extraction or OCR pipeline) and asks
the LLM to pull out a fixed set of fields as structured JSON. The model is
instructed never to guess -- any field it cannot find in the text must come
back empty.

`extract_fields()` accepts an optional `skip_fields` set -- fields the
caller already found deterministically (see regex_extractor.py) and
doesn't need the AI for. Those fields are dropped from the prompt entirely
rather than asked and discarded, so a document where regex found most
fields gets a smaller prompt and a smaller response, not just an ignored
answer. Every field still has an AI-extraction path as a fallback: this
is a per-document optimization, not a permanent removal -- if regex finds
nothing for a given document, that field is still asked for, exactly as
before regex extraction existed.
"""

import json

from openai import OpenAI

SYSTEM_PROMPT = (
    "You are an assistant that reads Safety Data Sheets (SDS) and extracts "
    "structured information. Read the SDS document text provided by the "
    "user and extract the requested fields. Return only the requested "
    "information. Do not guess or invent missing information. If a value "
    "cannot be found in the text, return an empty value for that field "
    "(empty string for text fields, empty list for list fields). Never "
    "fabricate a CAS number, date, or any other field."
)

FIELDS_SCHEMA = {
    "product_chemical_name": "",
    "manufacturer_supplier": "",
    "emergency_contact_phone": "",
    "cas_number": "",
    "ghs_hazard_pictograms": [],
    "hazard_statement_codes": [],
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
    # -- SDS-Extraction-Contract.pdf v2 additions. Each also has a
    # regex_extractor.py tier tried first (see extraction_pipeline.py);
    # the AI is only asked for whichever of these regex didn't find on a
    # given document, via skip_fields below.
    "product_code": [],
    "synonyms": [],
    "regulation_basis": "",
    "signal_word": "",
    "flash_point": "",
    "nfpa": {},
    "transport": {},
    "rcra_waste_code": "",
    "ingredients": [],
}

LIST_FIELDS = (
    "ghs_hazard_pictograms", "hazard_statement_codes", "product_code",
    "synonyms", "ingredients",
)
DICT_FIELDS = ("nfpa", "transport")

# One prompt snippet per field, keyed the same as FIELDS_SCHEMA, so the
# prompt sent to the AI can be built from only the fields still needed for
# a given document (see skip_fields in extract_fields()).
FIELD_PROMPTS = {
    "product_chemical_name": "- product_chemical_name (string)",
    "manufacturer_supplier": "- manufacturer_supplier (string)",
    "emergency_contact_phone": (
        "- emergency_contact_phone (string, the emergency contact phone number "
        "for chemical emergencies -- e.g. a 24-hour emergency number such as "
        "CHEMTREC, or the manufacturer's emergency line. Leave \"\" if none is "
        "stated.)"
    ),
    "cas_number": "- cas_number (string)",
    "ghs_hazard_pictograms": (
        "- ghs_hazard_pictograms (array of strings, e.g. \"GHS02 - Flame\"). "
        "Include a pictogram ONLY if it is named or coded directly in the "
        "text (e.g. \"GHS02\", \"Flame\", \"flammable liquid symbol\", a "
        "pictogram legend/table). Do not try to infer a pictogram from a "
        "hazard statement code here -- those go in the next field instead, "
        "and are matched to pictograms separately, not by you."
    ),
    "hazard_statement_codes": (
        "- hazard_statement_codes (array of strings): every GHS/CLP Hazard "
        "Statement code that literally appears in the text, copied exactly "
        "as written (e.g. \"H225\", \"H304\", \"H411\"). A hazard statement "
        "code is the letter \"H\" followed by exactly 3 digits. List every "
        "one you find, even if there are several. Do not invent a code that "
        "is not literally present in the text."
    ),
    "safety_hazards": (
        "- safety_hazards (string, EVERY physical and health hazard "
        "statement in the Hazard Identification section, not just one -- if "
        "the document lists several hazard statements (e.g. H225, H315, "
        "H336), include all of them, each in the document's own wording, "
        "separated by \". \" or newlines. Do not drop any hazard statement "
        "for brevity -- safety-critical information must not be truncated. "
        "Only trim non-hazard boilerplate (e.g. precautionary statement "
        "numbering) if present. Leave \"\" if no hazards are stated.)"
    ),
    "physical_state": "- physical_state (string, e.g. Liquid, Solid, Gas)",
    "category": "- category (string, hazard/product category as stated in the SDS)",
    "version": (
        "- version (string, the SDS document's own version/revision number, "
        "e.g. \"3.2\" or \"Rev. 4\" -- look for labels such as \"Version\", "
        "\"Version No.\", \"Rev.\", \"Revision Number\", or \"SDS Version\", "
        "often in a header, footer, or near the revision/issue date. Do not "
        "use a product version, model number, or anything unrelated to the "
        "SDS document itself.)"
    ),
    "revision_date": "- revision_date (string, as written in the document)",
    "issue_date": "- issue_date (string, as written in the document)",
    "first_aid_measures": (
        "- first_aid_measures (string, the first aid measures stated in the "
        "document -- prefer the document's own wording and specific "
        "instructions (e.g. by exposure route: eyes, skin, inhalation, "
        "ingestion) rather than a vague paraphrase. Leave \"\" if not "
        "stated.)"
    ),
    "personal_protection": (
        "- personal_protection (string, the personal protective equipment "
        "(PPE) required, as stated in the document, e.g. gloves, eye "
        "protection, respirator. Leave \"\" if not stated.)"
    ),
    "storage": (
        "- storage (string, the storage requirements/precautions as stated "
        "in the document. Leave \"\" if not stated.)"
    ),
    "review_frequency_stated": (
        "- review_frequency_stated (string, ONLY if the document itself "
        "states how often it should be reviewed, e.g. \"Every 3 Years\", "
        "\"Annually\", \"review: Every 3 Years\" -- copy the stated interval "
        "as written. Leave \"\" if no review cadence is stated anywhere in "
        "the text; do not infer one.)"
    ),
    "product_code": (
        "- product_code (array of strings, the manufacturer's catalog/product "
        "numbers, e.g. \"AC326980000\" -- usually near a label like \"Cat "
        "No.\", \"Product Number\", or \"Item No.\" in Section 1. Leave [] if "
        "none stated.)"
    ),
    "synonyms": (
        "- synonyms (array of strings, alternate names for the chemical "
        "stated in the document, e.g. \"Tol\", \"Methylbenzene\" -- usually "
        "under a \"Synonyms\" label in Section 1. Leave [] if none stated or "
        "if the document says no synonyms are available.)"
    ),
    "regulation_basis": (
        "- regulation_basis (string, the regulatory standard this SDS was "
        "prepared under, e.g. \"US OSHA HazCom 2024 (29 CFR 1910.1200)\" -- "
        "usually stated near the top of the document or in Section 2. Leave "
        "\"\" if not stated.)"
    ),
    "signal_word": (
        "- signal_word (string, exactly one of \"DANGER\", \"WARNING\", or "
        "\"NONE\" -- the GHS signal word stated in Section 2's Label "
        "Elements. Leave \"\" if not stated.)"
    ),
    "flash_point": (
        "- flash_point (string, the flash point value and unit as stated in "
        "Section 9, e.g. \"4 C\". Leave \"\" if not applicable/not stated.)"
    ),
    "nfpa": (
        "- nfpa (object with integer keys \"health\", \"flammability\", "
        "\"instability\", from the NFPA 704 diamond rating if the document "
        "states one, e.g. {\"health\": 3, \"flammability\": 3, "
        "\"instability\": 0}. Leave {} if no NFPA rating is stated.)"
    ),
    "transport": (
        "- transport (object with string keys \"un_no\", \"shipping_name\", "
        "\"hazard_class\", \"packing_group\" from Section 14 Transport "
        "Information, e.g. {\"un_no\": \"UN1294\", \"shipping_name\": "
        "\"TOLUENE\", \"hazard_class\": \"3\", \"packing_group\": \"II\"}. "
        "Leave {} if Section 14 states the product is not regulated for "
        "transport or gives no UN number.)"
    ),
    "rcra_waste_code": (
        "- rcra_waste_code (string, the EPA RCRA hazardous waste code from "
        "Section 13, e.g. \"U220\" -- a letter followed by 3-4 digits. Leave "
        "\"\" if not stated.)"
    ),
    "ingredients": (
        "- ingredients (array of objects with string keys \"name\", "
        "\"cas_number\", \"concentration\" -- one per component listed in "
        "Section 3's composition table, e.g. {\"name\": \"Toluene\", "
        "\"cas_number\": \"108-88-3\", \"concentration\": \"<=100%\"}. "
        "Include every row of the table. Leave [] if Section 3 has no "
        "table.)"
    ),
}

USER_PROMPT_HEADER = """Extract the following fields from the SDS text below and \
return them as a single JSON object with exactly these keys:

{field_descriptions}

Return ONLY a JSON object with these keys. If a value is not present in the \
text, use "" (or [] for array fields). Do not add extra keys.

SDS TEXT:
\"\"\"
{sds_text}
\"\"\"
"""


def extract_fields(
    sds_text: str,
    api_key: str,
    model: str = "gpt-4o-mini",
    tracker=None,
    skip_fields: set = None,
) -> dict:
    """Send extracted SDS text to the LLM and return the structured fields.

    `skip_fields`, if given, excludes those keys from the prompt entirely --
    used when regex_extractor.py already found a confident value for this
    document, so the AI isn't asked (and doesn't spend output tokens
    answering) a question whose answer is already known. Fields not in
    `skip_fields` are asked for exactly as before. The returned dict always
    has every FIELDS_SCHEMA key; skipped keys keep their empty default --
    the caller is expected to overlay the regex-found values on top.

    On any failure, returns the empty schema rather than guessing.
    `tracker`, if given a usage_tracker.UsageTracker, records this call's
    token usage onto it.
    """
    result = dict(FIELDS_SCHEMA)

    if not sds_text or not sds_text.strip():
        return result

    skip_fields = skip_fields or set()
    fields_to_ask = [k for k in FIELDS_SCHEMA if k not in skip_fields]
    if not fields_to_ask:
        return result

    client = OpenAI(api_key=api_key)

    field_descriptions = "\n".join(FIELD_PROMPTS[k] for k in fields_to_ask)
    prompt = USER_PROMPT_HEADER.format(
        field_descriptions=field_descriptions, sds_text=sds_text[:60000]
    )

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    if tracker is not None:
        tracker.record(response, model)

    content = response.choices[0].message.content or "{}"

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return result

    for key in fields_to_ask:
        if key in data and data[key] is not None:
            result[key] = data[key]

    for key in LIST_FIELDS:
        if not isinstance(result[key], list):
            result[key] = [result[key]] if result[key] else []

    for key in DICT_FIELDS:
        if not isinstance(result[key], dict):
            result[key] = {}

    return result
