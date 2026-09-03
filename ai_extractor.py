"""LLM-based SDS field extraction.

Takes raw SDS text (from either the PDF-extraction or OCR pipeline) and asks
the LLM to pull out a fixed set of fields as structured JSON. The model is
instructed never to guess -- any field it cannot find in the text must come
back empty.
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
}

LIST_FIELDS = ("ghs_hazard_pictograms", "hazard_statement_codes")

USER_PROMPT_TEMPLATE = """Extract the following fields from the SDS text below and \
return them as a single JSON object with exactly these keys:

- product_chemical_name (string)
- manufacturer_supplier (string)
- emergency_contact_phone (string, the emergency contact phone number for \
chemical emergencies -- e.g. a 24-hour emergency number such as CHEMTREC, \
or the manufacturer's emergency line. Leave "" if none is stated.)
- cas_number (string)
- ghs_hazard_pictograms (array of strings, e.g. "GHS02 - Flame"). Include a \
pictogram ONLY if it is named or coded directly in the text (e.g. "GHS02", \
"Flame", "flammable liquid symbol", a pictogram legend/table). Do not try \
to infer a pictogram from a hazard statement code here -- those go in the \
next field instead, and are matched to pictograms separately, not by you.
- hazard_statement_codes (array of strings): every GHS/CLP Hazard Statement \
code that literally appears in the text, copied exactly as written (e.g. \
"H225", "H304", "H411"). A hazard statement code is the letter "H" followed \
by exactly 3 digits. List every one you find, even if there are several. \
Do not invent a code that is not literally present in the text.
- safety_hazards (string, EVERY physical and health hazard statement in the \
Hazard Identification section, not just one -- if the document lists \
several hazard statements (e.g. H225, H315, H336), include all of them, \
each in the document's own wording, separated by ". " or newlines. Do not \
drop any hazard statement for brevity -- safety-critical information must \
not be truncated. Only trim non-hazard boilerplate (e.g. precautionary \
statement numbering) if present. Leave "" if no hazards are stated.)
- physical_state (string, e.g. Liquid, Solid, Gas)
- category (string, hazard/product category as stated in the SDS)
- version (string, the SDS document's own version/revision number, e.g. \
"3.2" or "Rev. 4" -- look for labels such as "Version", "Version No.", \
"Rev.", "Revision Number", or "SDS Version", often in a header, footer, or \
near the revision/issue date. Do not use a product version, model number, \
or anything unrelated to the SDS document itself.)
- revision_date (string, as written in the document)
- issue_date (string, as written in the document)
- first_aid_measures (string, the first aid measures stated in the \
document -- prefer the document's own wording and specific instructions \
(e.g. by exposure route: eyes, skin, inhalation, ingestion) rather than a \
vague paraphrase. Leave "" if not stated.)
- personal_protection (string, the personal protective equipment (PPE) \
required, as stated in the document, e.g. gloves, eye protection, \
respirator. Leave "" if not stated.)
- storage (string, the storage requirements/precautions as stated in the \
document. Leave "" if not stated.)
- review_frequency_stated (string, ONLY if the document itself states how \
often it should be reviewed, e.g. "Every 3 Years", "Annually", "review: \
Every 3 Years" -- copy the stated interval as written. Leave "" if no \
review cadence is stated anywhere in the text; do not infer one.)

Return ONLY a JSON object with these keys. If a value is not present in the \
text, use "" (or [] for the two array fields). Do not add extra keys.

SDS TEXT:
\"\"\"
{sds_text}
\"\"\"
"""


def extract_fields(sds_text: str, api_key: str, model: str = "gpt-4o-mini") -> dict:
    """Send extracted SDS text to the LLM and return the structured fields.

    On any failure, returns the empty schema rather than guessing.
    """
    if not sds_text or not sds_text.strip():
        return dict(FIELDS_SCHEMA)

    client = OpenAI(api_key=api_key)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": USER_PROMPT_TEMPLATE.format(sds_text=sds_text[:60000]),
            },
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )

    content = response.choices[0].message.content or "{}"

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return dict(FIELDS_SCHEMA)

    result = dict(FIELDS_SCHEMA)
    for key in FIELDS_SCHEMA:
        if key in data and data[key] is not None:
            result[key] = data[key]

    for key in LIST_FIELDS:
        if not isinstance(result[key], list):
            result[key] = [result[key]] if result[key] else []

    return result
