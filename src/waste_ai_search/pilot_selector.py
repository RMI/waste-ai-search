from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from typing import Any

from .schema import normalize_scalar


PRIORITY_ISO3 = ["NGA", "PHL"]

GENERIC_EXACT_NAMES = {
    "openstreetmap landfill",
    "domestic landfill",
    "landfill",
    "dump",
    "dumps",
    "municipal garbage dump",
    "municipal dump",
    "solid waste landfill",
    "city landfill",
    "rubbish tip",
    "landfill site",
    "dumping",
    # Batch 4 (Germany, Russia, Turkey): kinds of site written as one word, or as a word for a
    # place rather than a waste site, that the rule below can't read as generic.
    "landfillmsw",
    "biolandfill",
    "pigsty",
    "sump",
    "ruins",
    "shooting range",
    "reclamation lands",
}

GENERIC_TOKENS = {
    "landfill",
    "dump",
    "dumpsite",
    "garbage",
    "waste",
    "rubbish",
    "tip",
    "trash",
    "disposal",
    # Spanish, for Mexico (batch 3), where 417 sites are named just "Basurero Municipal".
    "basurero", "basureros",
    "tiradero", "tiraderos",
    "relleno", "rellenos",
    "vertedero", "vertederos",
    "basura",
    "residuos",
    "disposicion", "disposición",
    # Batch 4. Names reach us in English, but OSM's Russian ones translate their words for a
    # landfill as "MSW" (ТБО, ТКО) and "polygon" (полигон), and the kind of non-municipal waste a
    # site holds as its whole name ("Woodworking waste", "Litter storage", "Oil pits").
    "msw", "tbo", "tko", "polygon", "poltgon", "dumping", "dumpsite", "dumps",
    "storage", "pit", "pits", "beds", "pockets", "yard", "hump", "area", "zone", "facility",
    # German and Russian spellings, for a name that reached us untranslated ("ehem. Deponie",
    # "Erdmassendeponie", "Wertstoffhof", "Бывшая свалка").
    "deponie", "mülldeponie", "muelldeponie", "erddeponie", "abfalldeponie", "bauschuttdeponie",
    "bodendeponie", "erdmassendeponie", "erdaushubdeponie", "altdeponie", "kreismülldeponie",
    "müllkippe", "muellkippe", "kippe", "schuttplatz", "wertstoffhof", "recyclinghof",
    "altlastsanierung",
    "свалка", "свалки", "полигон", "тбо", "тко",
}

# Words that, beside a generic word, only say what kind of site it is ("Municipal Landfill",
# "Relleno Sanitario Tipo C", "Tiradero a Cielo Abierto"), or that it has no name ("Unnamed
# open-air dump", "Tiradero sin nombre"), and the longer articles that join them. They don't make a
# name generic on their own: "Municipal" alone is still a name. Words for which site it is, such as
# a direction or a place, are left out on purpose ("North Landfill", "Central Landfill").
GENERIC_QUALIFIERS = {
    "municipal", "municpal", "intermunicipal", "regional", "city", "public", "sanitary", "controlled",
    "open", "air", "open-air", "final", "site", "solid",
    "unnamed", "unknown", "without", "name",
    "sanitario", "sanitaria", "controlado", "controlada", "cielo", "abierto", "sitio", "tipo",
    "solidos", "sólidos", "urbanos", "rsu", "sin", "nombre",
    "the", "del", "los", "las",
    # Batch 4. Words that say a site's state or what it held, never which site it is ("Former
    # landfill", "Spontaneous dump", "Excavated landfill", "Woodworking waste"). "Central", "East"
    # and the like are still left out: they say which site.
    "former", "old", "new", "closed", "inactive", "unused", "illegal", "unauthorized",
    "spontaneous", "elemental", "temporary", "currently", "incomplete", "reclaimed",
    "recultivated", "excavated", "district", "village", "household", "urban", "domestic",
    "industrial", "production", "woodworking", "deprocessing", "sawdust", "lignin", "litter",
    "livestock", "alkaline", "rubber", "goods", "oil", "drain", "wastewater", "asbestos", "lime",
    "soda", "ore", "mud", "peat", "clay", "earth", "green", "rubble", "space", "place", "sorting",
    "complex", "collection", "processing", "operating", "use", "fill", "dumping",
    "ehem", "ehemalige", "ehm", "städt", "stillgelegt", "rekultivierte",
    "бывшая", "бывший", "старая", "стихийная", "несанкционированная", "незаконная", "городская",
    "районная", "поселковая", "закрыта", "закрытая",
}

# Two-letter words that say nothing about which site it is: articles and prepositions (English,
# Spanish, Portuguese, French, Italian), "no" and "nr" before a number, and "lf" for landfill.
FILLER_SHORT_TOKENS = {
    "no", "nr", "lf",
    "of", "in", "at", "on", "to", "by", "or", "an", "as", "is", "it",
    "de", "do", "da", "di", "du", "el", "la", "le", "lo", "en", "et", "es", "na", "al", "os", "ao",
}
# "Landfill II" numbers a cell or phase; it doesn't name a site.
ROMAN_NUMERAL = re.compile(r"^x{0,3}(ix|iv|v?i{0,3})$")


def stable_int(*parts: Any) -> int:
    text = "|".join(normalize_scalar(part) for part in parts)
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], 16)


def sort_key(row: dict[str, Any], salt: str = "default") -> tuple[int, str]:
    return stable_int(row.get("site_id"), row.get("site_name"), row.get("country_iso3"), salt), normalize_scalar(
        row.get("site_id")
    )


def informative_tokens(text: str) -> list[str]:
    """The words of a lowercased name that could say which site it is.

    A word of three or more characters counts unless it is generic or a generic qualifier
    (GENERIC_QUALIFIERS). A shorter one counts when it
    looks like initials or a route code: two letters other than filler and roman numerals
    ("GB Landfill", "SR 85 Landfill"), or single letters joined by "&" ("K & W Landfill"). Short
    numbers ("Dump 1"), lone letters ("Landfill A") and roman numerals ("Landfill II") don't.
    """
    words = [word.strip(".,;:()[]{}\"'«»") for word in re.sub(r"[()/\[\]]", " ", text).split()]
    informative = []
    for index, word in enumerate(words):
        if not word or word in GENERIC_TOKENS or word in GENERIC_QUALIFIERS:
            continue
        if len(word) > 2:
            informative.append(word)
        elif not word.isalpha() or ROMAN_NUMERAL.match(word):
            continue
        elif len(word) == 2 and word not in FILLER_SHORT_TOKENS:
            informative.append(word)
        elif "&" in words[max(0, index - 1) : index + 2]:
            informative.append(word)
    return informative


def is_generic_site_name(name: Any) -> bool:
    text = normalize_scalar(name).lower().strip()
    if not text:
        return True
    if text in GENERIC_EXACT_NAMES:
        return True
    tokens = {token.strip(".,;:()[]{}\"'«»") for token in re.sub(r"[()/\[\]]", " ", text).split()}
    return bool(tokens & GENERIC_TOKENS) and not informative_tokens(text)


def add_unique(
    selected: list[dict[str, Any]],
    seen: set[str],
    row: dict[str, Any],
    reason: str,
) -> bool:
    site_id = normalize_scalar(row.get("site_id"))
    if not site_id or site_id in seen:
        return False
    copy = dict(row)
    copy["pilot_selection_reason"] = reason
    selected.append(copy)
    seen.add(site_id)
    return True


def select_mixed_pilot(
    sites: list[dict[str, Any]],
    size: int = 150,
    priority_iso3: list[str] | None = None,
) -> list[dict[str, Any]]:
    if size <= 0:
        return []
    priority_iso3 = priority_iso3 or PRIORITY_ISO3
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    by_country: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for site in sites:
        by_country[normalize_scalar(site.get("country_iso3"))].append(site)

    for iso3 in priority_iso3:
        for row in sorted(by_country.get(iso3, []), key=lambda item: sort_key(item, "priority")):
            add_unique(selected, seen, row, f"priority_country:{iso3}")
            if len(selected) >= size:
                return selected

    remaining_slots = size - len(selected)
    high_volume_target = min(50, max(0, remaining_slots // 2))
    top_countries = [
        iso3
        for iso3, _count in Counter(normalize_scalar(site.get("country_iso3")) for site in sites).most_common()
        if iso3 and iso3 not in priority_iso3
    ]
    country_queues = {
        iso3: sorted(by_country[iso3], key=lambda item: sort_key(item, "country_balance"))
        for iso3 in top_countries
    }
    added_high_volume = 0
    while added_high_volume < high_volume_target and len(selected) < size:
        progressed = False
        for iso3 in top_countries:
            queue = country_queues.get(iso3, [])
            while queue:
                row = queue.pop(0)
                if add_unique(selected, seen, row, f"country_balanced:{iso3}"):
                    added_high_volume += 1
                    progressed = True
                    break
            if added_high_volume >= high_volume_target or len(selected) >= size:
                break
        if not progressed:
            break

    generic_target = min(30, size - len(selected))
    generic_candidates = [
        site
        for site in sites
        if normalize_scalar(site.get("site_id")) not in seen and is_generic_site_name(site.get("site_name"))
    ]
    for row in sorted(generic_candidates, key=lambda item: sort_key(item, "generic_hard_case"))[:generic_target]:
        add_unique(selected, seen, row, "generic_name_hard_case")
        if len(selected) >= size:
            return selected

    remaining = [site for site in sites if normalize_scalar(site.get("site_id")) not in seen]
    for row in sorted(remaining, key=lambda item: sort_key(item, "deterministic_fill")):
        add_unique(selected, seen, row, "deterministic_fill")
        if len(selected) >= size:
            break

    return selected
