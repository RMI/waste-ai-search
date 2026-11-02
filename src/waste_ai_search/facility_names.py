# Started as a copy of RMI/waste_data_ingestion_pipeline#225's name scoring
# (facility_etl/entity_linkage/facility_names.py, as of b79aa81 on entity-linkage-precise-v1), and is
# this repository's own since. It no longer matches sources to sites (step 2 of the approve-sources
# plan removed the matcher); datasets_found.csv tells a page's facility names apart by their
# distinctive words. AI search rows join the facility named by the wastemap_id they carry (#264), so
# this needn't agree with entity linkage. Where this copy has moved on, the generic words this corpus
# needs and the comparison of operators' names, it is marked below.
"""
Comparing facility names: whether two source records' names describe the same place. And comparing
operators' names, so two spellings of one body's name are not read as two sources disagreeing
(same_operator).

Names are compared by their distinctive words. Words that say what a facility is or who
runs it ("landfill", "sanitary", "county", "waste management"), company suffixes ("Ltd",
"GmbH", "d.o.o.") and permit or registration codes ("EPR/BV7877IR") are dropped, common
abbreviations are expanded ("LF" -> landfill), and accents and punctuation are ignored. A
name left with no distinctive words can't be judged. Qualifiers that tell parts of one
site apart (North/South, I/II) or places on one road (27th / 19th Avenue) count against a
match whenever two names have different ones.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# Words that say what a facility is, or who runs it, rather than which one it is.
GENERIC_NAME_WORDS = {
    "landfill", "landfills", "sanitary", "municipal", "regional", "county", "city", "site",
    "dump", "dumpsite", "dumping", "facility", "facilities", "waste", "solid", "disposal",
    "recycling", "transfer", "station", "center", "centre", "lfgte", "gas", "energy",
    "project", "inc", "llc", "co", "corp", "company", "management", "services", "domestic",
    "garbage", "trash", "rubbish", "ground", "urban", "land", "mrf", "the", "of", "and",
    "at", "de", "do", "da", "del", "la", "el", "los", "las", "aterro", "sanitario", "lixao",
    "controlado", "vertedero", "relleno", "botadero", "deponie", "discarica",
    # OpenStreetMap's placeholder for a landfill with no name of its own.
    "openstreetmap",
    # Added here: Australian and British words for a dump ("Rubbish Tip", "Waste Depot",
    # "Waste Disposal Grounds", Manitoba's "Nuisance Grounds") and Quebec's "lieu d'enfouissement
    # technique" (LET), with the elided d' and l' of French names, which would otherwise count as
    # words. Without them, batch 1's seven "Rubbish Tip" sites each matched 68 others.
    "tip", "refuse", "depot", "grounds", "nuisance", "lieu", "enfouissement", "technique", "let", "d", "l",
    # Also added here: the words of other kinds of site and their small words. Australia's
    # "Resource Recovery Centre", which moved Noosa's page 20 km to Nambour's; Canada's "Integrated
    # Waste Management Facility" (IWMF), which kept Prairie Green's page from matching our "Prairie
    # Green IWMF"; France's "Installation de Stockage de Déchets Non Dangereux", in English
    # "Landfill for Non-Hazardous Waste", which 187 pairs of French sites share; Mexico's "tiradero
    # a cielo abierto del municipio" ("open-air dump"); Catalonia's "dipòsit controlat"; Brazil's
    # "aterro em valas" ("landfill in ditches"); and the French "à", "des" and "du".
    "resource", "recovery", "integrated", "iwmf", "installation", "stockage", "dechets", "non",
    "dangereux", "hazardous", "open", "air", "tiradero", "cielo", "abierto", "municipio", "diposit",
    "controlat", "controlled", "ditches", "trenches", "for", "in", "a", "des", "du",
    # And words for a site's state, which say what became of it, not which one it is. A council's
    # list can call every row "Closed Landfill", and none of them is told apart by it.
    "closed", "former", "disused", "abandoned", "inactive", "decommissioned", "historic", "historical",
}
# Company suffixes, written as distinctive_words sees them: lowercase, with the dots of a
# dotted form like "d.o.o." or "S.p.A." removed. A company form names the operator's legal
# type, not the facility ("Biffa Waste Services Ltd, Risley Landfill Site").
COMPANY_SUFFIXES = {
    "ltd", "limited", "plc", "llp", "lp", "gmbh", "mbh", "ag", "kg", "ohg", "ev", "sa", "sas", "sau",
    "sarl", "eurl", "srl", "spa", "scrl", "bv", "nv", "vof", "oy", "oyj", "ab", "as", "asa", "aps",
    "kft", "zrt", "nyrt", "bt", "doo", "dd", "sro", "pty", "ltda", "lda", "sl", "slu", "cv",
}
# Permit and register prefixes that come with a code, as in E-PRTR's "EPR/BV7877IR".
PERMIT_WORDS = {"epr"}

NAME_ABBREVIATIONS = {
    "lf": "landfill", "slf": "landfill", "ldfl": "landfill", "sani": "sanitary", "msw": "waste",
    "cnty": "county", "twp": "township",
}
# Spelled-out ordinals, read as numbers so "Nineteenth Avenue" and "19th Avenue" agree.
ORDINAL_WORDS = {
    "first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th", "fifth": "5th", "sixth": "6th",
    "seventh": "7th", "eighth": "8th", "ninth": "9th", "tenth": "10th", "eleventh": "11th",
    "twelfth": "12th", "thirteenth": "13th", "fourteenth": "14th", "fifteenth": "15th",
    "sixteenth": "16th", "seventeenth": "17th", "eighteenth": "18th", "nineteenth": "19th",
    "twentieth": "20th", "thirtieth": "30th", "fortieth": "40th", "fiftieth": "50th",
}
# Words that tell two parts of one site apart. Any number or ordinal counts too ("Cell 10" /
# "Cell 11", "27th Avenue" / "19th Avenue").
QUALIFIER_WORDS = {
    "north", "south", "east", "west", "northeast", "northwest", "southeast", "southwest",
    "upper", "lower", "old", "new", "i", "ii", "iii", "iv", "1", "2", "3", "4",
}


def distinctive_words(name) -> set:
    """The words of a name that identify the facility, lowercased and without accents.
    Generic words, company suffixes and permit codes are left out (see the module notes)."""
    if not isinstance(name, str):
        return set()
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    # Join dotted single letters, so "d.o.o." and "S.p.A." read as one word ("doo", "spa").
    plain = re.sub(r"\b([a-z])\.(?=[a-z]\b)", r"\1", plain)
    words = (
        ORDINAL_WORDS.get(word, NAME_ABBREVIATIONS.get(word, word)) for word in re.findall(r"[a-z0-9]+", plain)
    )
    return {
        word for word in words
        if word not in GENERIC_NAME_WORDS and word not in COMPANY_SUFFIXES and word not in PERMIT_WORDS
        and not _is_code(word)
    }


def _is_code(word: str) -> bool:
    """Whether a word mixes letters and digits, like a permit number ("bv7877ir"). Numbers
    and ordinals are kept: they tell parts of a site or places on a road apart ("Cell 10" /
    "Cell 11", "27th Avenue" / "19th Avenue")."""
    return bool(re.search(r"[a-z]", word)) and bool(re.search(r"[0-9]", word)) and not _is_ordinal(word)


def _is_ordinal(word: str) -> bool:
    return re.fullmatch(r"[0-9]+(st|nd|rd|th)", word) is not None


def qualifiers(words: set) -> set:
    """The words in a set that tell parts of one site apart: QUALIFIER_WORDS, numbers and
    ordinals."""
    return {word for word in words if word in QUALIFIER_WORDS or word.isdigit() or _is_ordinal(word)}


def qualifiers_conflict(name_a, name_b) -> bool:
    """Whether both names carry qualifiers (North/South, I/II, numbers) and none are shared."""
    qualifiers_a = qualifiers(distinctive_words(name_a))
    qualifiers_b = qualifiers(distinctive_words(name_b))
    return bool(qualifiers_a and qualifiers_b and not qualifiers_a & qualifiers_b)


def name_similarity(name_a, name_b) -> float | None:
    """How alike two facility names are, from 0 to 1, comparing only their distinctive
    words. None when either name is missing or has no distinctive words, so it can't be
    judged. Names whose qualifiers conflict score 0."""
    words_a, words_b = distinctive_words(name_a), distinctive_words(name_b)
    if not words_a or not words_b:
        return None
    if qualifiers_conflict(name_a, name_b):
        return 0.0
    # Matching qualifiers say nothing more, so compare the rest - unless that leaves either
    # name with nothing, in which case both keep them.
    if words_a - qualifiers(words_a) and words_b - qualifiers(words_b):
        words_a, words_b = words_a - qualifiers(words_a), words_b - qualifiers(words_b)
    # Share of the shorter name's words found in the longer one, or how alike the two
    # read as strings - whichever is higher, so a typo or a run-together word still counts.
    shorter, longer = sorted([words_a, words_b], key=len)
    containment = len(shorter & longer) / len(shorter)
    spelling = SequenceMatcher(None, " ".join(sorted(words_a)), " ".join(sorted(words_b))).ratio()
    return max(containment, spelling)


# --- operators -----------------------------------------------------------------------------------
# Added here: words that don't say which body runs a site. Spanish small words; a company's or
# association's legal form, however it is spaced or dotted ("S.A. de C.V.", "SA de CV", "S.A.P.I.",
# "S. de R.L.", "A.C."), with the suffixes above; and the "H." or "Honorable" of a town council.
OPERATOR_SMALL_WORDS = {"de", "del", "la", "las", "los", "el", "y"}
OPERATOR_LEGAL_FORMS = COMPANY_SUFFIXES | {"sapi", "sab", "s", "rl", "ac", "sc"}
OPERATOR_TITLES = {"h", "honorable"}


def operator_words(name) -> tuple[set, set]:
    """An operator name's words, lowercased and without accents, and the acronyms it gives in
    brackets ("(SOS)"), kept aside. Small words, legal forms, titles and administration years
    ("2024-2027") are left out."""
    if not isinstance(name, str):
        return set(), set()
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    # Join dotted single letters, spaced or not, so "S.A." and "S. A." read as "sa".
    plain = re.sub(r"\b([a-z])\.\s?(?=[a-z]\b)", r"\1", plain)

    def kept(text: str) -> set:
        return {
            word for word in re.findall(r"[a-z0-9]+", text)
            if word not in OPERATOR_SMALL_WORDS | OPERATOR_LEGAL_FORMS | OPERATOR_TITLES
            and not re.fullmatch(r"(19|20)[0-9]{2}", word)
        }

    acronyms = {"".join(sorted(kept(part))) for part in re.findall(r"\(([^()]*)\)", plain)} - {""}
    return kept(re.sub(r"\([^()]*\)", " ", plain)), acronyms


def same_operator(name_a, name_b) -> bool:
    """Whether two operator names are one body's, spelled two ways: one name's words (operator_words)
    are all among the other's, or one name is the other's bracketed acronym, or both give the same
    acronym. "Servicios, Operaciones y Suministros de la Isla (SOS)" and "Servicios, Operaciones y
    Suministros (SOS)" are one operator, and so are "H. Ayuntamiento de Tixkokob 2024-2027" and
    "Ayuntamiento de Tixkokob"; "Ayuntamiento de Tixkokob" and "Ayuntamiento de Motul" are two. A bare
    "Ayuntamiento" is any town council's, which within one site is that site's."""
    words_a, acronyms_a = operator_words(name_a)
    words_b, acronyms_b = operator_words(name_b)
    if words_a and words_b and (words_a <= words_b or words_b <= words_a):
        return True
    if acronyms_a & acronyms_b or any(words_b == {acronym} for acronym in acronyms_a) or any(words_a == {acronym} for acronym in acronyms_b):
        return True
    # Two names of small words and legal forms only have nothing to tell them apart.
    return not (words_a or words_b or acronyms_a or acronyms_b)
