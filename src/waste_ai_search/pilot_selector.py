from __future__ import annotations

import hashlib
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
}

GENERIC_TOKENS = {
    "landfill",
    "dump",
    "dumpsite",
    "garbage",
    "waste",
    "rubbish",
    "tip",
}


def stable_int(*parts: Any) -> int:
    text = "|".join(normalize_scalar(part) for part in parts)
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], 16)


def sort_key(row: dict[str, Any], salt: str = "default") -> tuple[int, str]:
    return stable_int(row.get("site_id"), row.get("site_name"), row.get("country_iso3"), salt), normalize_scalar(
        row.get("site_id")
    )


def is_generic_site_name(name: Any) -> bool:
    text = normalize_scalar(name).lower().strip()
    if not text:
        return True
    if text in GENERIC_EXACT_NAMES:
        return True
    tokens = {token.strip(".,;:()[]{}") for token in text.split()}
    informative = [token for token in tokens if token not in GENERIC_TOKENS and len(token) > 2]
    return bool(tokens & GENERIC_TOKENS) and len(informative) == 0


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
