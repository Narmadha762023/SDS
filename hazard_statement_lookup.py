"""Official H-code -> hazard-statement text table (UN GHS / EU CLP).

Used in one direction only: when a document prints an H-code with no
sentence beside it, `text_for_code()` supplies that code's official
wording. The code always originates from the document.

The reverse direction is deliberately absent. An earlier version offered
it, and it was removed because deriving a code from a sentence asserts a
classification the document never printed. That risk is not theoretical:
told explicitly not to invent a code that wasn't literally present, the AI
still returned H225/H319/H335/H336/H373 for a document confirmed to
contain zero literal H-code characters -- it recognized the standard GHS
phrasing from training and filled the codes in from memory. They happened
to be correct, but "happened to be correct" isn't verifiable. A document
that prints hazard sentences without codes now yields no hazard statements
at all, rather than codes worked backwards from wording.

Source: Wikipedia's GHS hazard statements page, cross-checked against
codes seen in this project's real documents (H225, H226, H272, H290,
H304, H314, H319, H330 among them).
"""

H_CODE_TO_TEXT = {
    # Physical hazards
    "H204": "Fire or projection hazard",
    "H220": "Extremely flammable gas",
    "H221": "Flammable gas",
    "H222": "Extremely flammable aerosol",
    "H223": "Flammable aerosol",
    "H224": "Extremely flammable liquid and vapour",
    "H225": "Highly flammable liquid and vapour",
    "H226": "Flammable liquid and vapour",
    "H227": "Combustible liquid",
    "H228": "Flammable solid",
    "H229": "Pressurized container: may burst if heated",
    "H230": "May react explosively even in the absence of air",
    "H231": "May react explosively even in the absence of air at elevated pressure and/or temperature",
    "H232": "May ignite spontaneously if exposed to air",
    "H240": "Heating may cause an explosion",
    "H241": "Heating may cause a fire or explosion",
    "H242": "Heating may cause a fire",
    "H250": "Catches fire spontaneously if exposed to air",
    "H251": "Self-heating: may catch fire",
    "H252": "Self-heating in large quantities: may catch fire",
    "H260": "In contact with water releases flammable gases which may ignite spontaneously",
    "H261": "In contact with water releases flammable gas",
    "H270": "May cause or intensify fire: oxidizer",
    "H271": "May cause fire or explosion: strong oxidizer",
    "H272": "May intensify fire: oxidizer",
    "H280": "Contains gas under pressure: may explode if heated",
    "H281": "Contains refrigerated gas: may cause cryogenic burns or injury",
    "H282": "Extremely flammable chemical under pressure: May explode if heated",
    "H283": "Flammable chemical under pressure: May explode if heated",
    "H284": "Chemical under pressure: May explode if heated",
    "H290": "May be corrosive to metals",
    # Health hazards
    "H300": "Fatal if swallowed",
    "H301": "Toxic if swallowed",
    "H302": "Harmful if swallowed",
    "H303": "May be harmful if swallowed",
    "H304": "May be fatal if swallowed and enters airways",
    "H305": "May be harmful if swallowed and enters airways",
    "H310": "Fatal in contact with skin",
    "H311": "Toxic in contact with skin",
    "H312": "Harmful in contact with skin",
    "H313": "May be harmful in contact with skin",
    "H314": "Causes severe skin burns and eye damage",
    "H315": "Causes skin irritation",
    "H316": "Causes mild skin irritation",
    "H317": "May cause an allergic skin reaction",
    "H318": "Causes serious eye damage",
    "H319": "Causes serious eye irritation",
    "H320": "Causes eye irritation",
    "H330": "Fatal if inhaled",
    "H331": "Toxic if inhaled",
    "H332": "Harmful if inhaled",
    "H333": "May be harmful if inhaled",
    "H334": "May cause allergy or asthma symptoms of breathing difficulties if inhaled",
    "H335": "May cause respiratory irritation",
    "H336": "May cause drowsiness or dizziness",
    "H340": "May cause genetic defects",
    "H341": "Suspected of causing genetic defects",
    "H350": "May cause cancer",
    "H350i": "May cause cancer by inhalation",
    "H351": "Suspected of causing cancer",
    "H360": "May damage fertility or the unborn child",
    "H360D": "May damage the unborn child",
    "H360F": "May damage fertility",
    "H361": "Suspected of damaging fertility or the unborn child",
    "H362": "May cause harm to breast-fed children",
    "H370": "Causes damage to organs",
    "H371": "May cause damage to organs",
    "H372": "Causes damage to organs through prolonged or repeated exposure",
    "H373": "May cause damage to organs through prolonged or repeated exposure",
    # Environmental hazards
    "H400": "Very toxic to aquatic life",
    "H401": "Toxic to aquatic life",
    "H402": "Harmful to aquatic life",
    "H410": "Very toxic to aquatic life with long lasting effects",
    "H411": "Toxic to aquatic life with long lasting effects",
    "H412": "Harmful to aquatic life with long lasting effects",
    "H413": "May cause long lasting harmful effects to aquatic life",
    "H420": "Harms public health and the environment by destroying ozone in the upper atmosphere",
    "H421": "Harms public health and the environment by contributing to global warming",
}

def text_for_code(code: str):
    """Official statement text for an H-code the document itself printed,
    or None for a code not in the table.

    Forward direction only (code -> text). The reverse (text -> code) is
    deliberately NOT offered: inferring a code from a sentence asserts a
    classification the document never printed, which is the exact
    hallucination risk this module exists to remove -- see the module
    docstring.
    """
    if not code:
        return None
    return H_CODE_TO_TEXT.get(code)
