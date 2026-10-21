"""One reader for every cached agent response, whichever shape it was written in.

The agent used to answer attribute-first: each attribute it found, with the sources behind it. It
now answers source-first: each source it used, with the facility that source describes - its name
and location as the source writes them - and the values it gives (build step 4 of the
source-matching plan). Every reader of a cached response goes through read_payload, which turns
either shape into the source-first one, so a run cached before the change still arbitrates: the
evidence, the link check, the run log's counts, validation, the merge of a follow-up pass, retry
detection and the agent's own leads.

An attribute-first response is converted like this:

- citations are grouped into one source per exact URL (normalize_url); a citation with no URL
  stays a source of its own. Where two citations of one URL describe it differently, each field
  takes the first value given, so an undated citation of a dated page gets its date;
- each source gets the values of the attributes that cited it, each with its own quote, and each
  keeps its evidence id and its place in the response's order;
- facility_name_in_source is the response's found_facility_name, for the sources cited for it;
- stated_location is the found latitude and longitude as "lat, lon", kind "coordinates", for the
  sources cited for both. They are no longer values: a found location comes from what each source
  states (locate.py), and half a location is none.
"""
from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .schema import COORDINATE_ATTRIBUTES, REQUESTABLE_ATTRIBUTES, canonical_attribute, normalize_scalar


ATTRIBUTE_FIRST = "attribute-first"
SOURCE_FIRST = "source-first"

# What each source reports about itself and the facility it describes.
SOURCE_FIELDS = [
    "url", "source_title", "publisher", "source_type", "publication_date", "language", "paywall_flag",
    "facility_name_in_source", "facility_name_english", "stated_location", "stated_location_kind",
]
# Kept from older responses, which carried them on each citation.
LEGACY_SOURCE_FIELDS = ["subscription_followup_needed", "source_tier"]
# What each value reports. The fields the code reads are the ones it read before the change.
VALUE_FIELDS = [
    "attribute_name", "value", "unit", "value_basis", "confidence_score", "value_date",
    "evidence_summary", "search_terms_used", "quoted_evidence_short",
]
REQUIRED_VALUE_FIELDS = ["attribute_name", "value", "value_basis", "confidence_score"]
# An attribute-first response's required fields, as validation reported them.
REQUIRED_ATTRIBUTE_FIELDS = [*REQUIRED_VALUE_FIELDS, "sources"]

# Marks a payload read_payload has already read, so reading twice is harmless, and a source it
# converted from an attribute-first response, whose problems the conversion already reported.
_READ = "_read"
_CONVERTED = "_converted"
# A converted value's place among the response's citations, and its attribute and citation number.
_ORDER = "_order"
_CITATION = "_citation"


def is_converted(source: dict[str, Any]) -> bool:
    return bool(source.get(_CONVERTED))


def normalize_url(url: Any) -> str:
    """The exact URL a source is filed under: one row in sources.csv, and one source_id.

    Only spellings that never change the page are cleaned: the scheme and host are lower-cased,
    and a leading "www.", a trailing slash on the path and the fragment are dropped. The path and
    the query string are kept as written, case included. Dropping the query string, as this once
    did, merged 103 different Natural Resources Wales documents, which differ only in their query,
    into one source.
    """
    text = normalize_scalar(url)
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        if not parts.scheme and not parts.netloc:
            parts = urlsplit("//" + text)  # written without a scheme
    except ValueError:
        return text
    netloc = parts.netloc.lower()
    netloc = netloc[4:] if netloc.startswith("www.") else netloc
    path = parts.path.rstrip("/")
    return urlunsplit(((parts.scheme or "https").lower(), netloc, path, parts.query, ""))


def source_id_for(url: Any, fallback: str) -> str:
    """A source's id, derived from its exact URL, so it is the same wherever the URL is cited.

    evidence.csv and sources.csv both carry it, so the two files join. A citation with no URL has
    nothing to derive an id from, and keeps `fallback`, its own per-citation id.
    """
    exact = normalize_url(url)
    if not exact:
        return fallback
    return "WAISRC-" + hashlib.sha256(exact.encode("utf-8")).hexdigest()[:12]


def read_payload(payload: Any) -> dict[str, Any]:
    """Any cached response as sources, each with its values. Never raises on a malformed one.

    Returns {"site_id", "sources", "unverified_leads", "search_notes", "shape", "problems"}, where
    `problems` lists what is malformed, in the words validation reports.
    """
    if isinstance(payload, dict) and payload.get(_READ):
        return payload
    out: dict[str, Any] = {
        _READ: True,
        "site_id": payload.get("site_id") if isinstance(payload, dict) else None,
        "sources": [],
        "unverified_leads": [],
        "search_notes": "",
        "shape": SOURCE_FIRST,
        "problems": [],
    }
    if not isinstance(payload, dict):
        out["problems"].append("Response is not a JSON object.")
        return out
    leads = payload.get("unverified_leads") or []
    out["unverified_leads"] = list(leads) if isinstance(leads, list) else [leads]
    out["search_notes"] = normalize_scalar(payload.get("search_notes"))
    # Attributes first: where a follow-up pass's response is merged with an older one, the older
    # is attribute-first, and reading it first keeps the passes in order.
    if "attributes" in payload:
        if "sources" not in payload:
            out["shape"] = ATTRIBUTE_FIRST
        out["sources"].extend(_from_attributes(payload.get("attributes"), out["problems"]))
    if "sources" in payload:
        out["sources"].extend(_read_sources(payload.get("sources"), out["problems"]))
    return out


def _read_sources(sources: Any, problems: list[str]) -> list[dict[str, Any]]:
    if not isinstance(sources, list):
        problems.append("Payload field 'sources' must be a list.")
        return []
    out = []
    for index, source in enumerate(sources, start=1):
        if not isinstance(source, dict):
            problems.append(f"sources[{index}] must be an object.")
            continue
        values = source.get("values", [])
        if not isinstance(values, list):
            problems.append(f"sources[{index}] values must be a list.")
            values = []
        kept = []
        for value_index, value in enumerate(values, start=1):
            if isinstance(value, dict):
                kept.append(dict(value))
            else:
                problems.append(f"sources[{index}].values[{value_index}] must be an object.")
        out.append({**source, "values": kept})
    return out


def _from_attributes(attributes: Any, problems: list[str]) -> list[dict[str, Any]]:
    """Convert an attribute-first response's attributes into sources (see the module docstring)."""
    if not isinstance(attributes, list):
        problems.append("Payload field 'attributes' must be a list.")
        return []
    sources: dict[str, dict[str, Any]] = {}
    cited_for: dict[str, dict[str, Any]] = {}
    order = 0
    for index, attribute in enumerate(attributes, start=1):
        if not isinstance(attribute, dict):
            problems.append(f"attributes[{index}] must be an object.")
            continue
        for field in REQUIRED_ATTRIBUTE_FIELDS:
            if field not in attribute:
                problems.append(f"attributes[{index}] missing required field {field!r}.")
        name = canonical_attribute(normalize_scalar(attribute.get("attribute_name")))
        if name and name not in REQUESTABLE_ATTRIBUTES and name not in COORDINATE_ATTRIBUTES:
            problems.append(f"attributes[{index}] has unsupported attribute_name {name!r}.")
        citations = attribute.get("sources", [])
        if not isinstance(citations, list):
            problems.append(f"attributes[{index}] sources must be a list.")
            continue
        if not any(isinstance(citation, dict) and citation.get("url") for citation in citations):
            problems.append(f"attributes[{index}] has no clickable source URL.")

        shared = {field: attribute[field] for field in VALUE_FIELDS if field in attribute and field != "quoted_evidence_short"}
        for citation_index, citation in enumerate(citations, start=1):
            if not isinstance(citation, dict):
                continue
            key = normalize_url(citation.get("url")) or f"#{index}-{citation_index}"
            source = sources.get(key)
            if source is None:
                source = {"values": [], _CONVERTED: True}
                sources[key] = source
                cited_for[key] = {}
            for field in [*SOURCE_FIELDS, *LEGACY_SOURCE_FIELDS]:
                if field in citation and (field not in source or normalize_scalar(source[field]) == ""):
                    source[field] = citation[field]
            cited_for[key].setdefault(name, attribute.get("value"))
            if name in COORDINATE_ATTRIBUTES:
                continue  # a location now comes from what each source states, below
            value = dict(shared)
            if "quoted_evidence_short" in citation:
                value["quoted_evidence_short"] = citation["quoted_evidence_short"]
            # Where the citation sat, so arbitration still reads them in the order the agent gave
            # them - the last tie-break between equal sources - and keeps their evidence ids.
            order += 1
            value[_ORDER] = order
            value[_CITATION] = f"{index:03d}-{citation_index:03d}"
            source["values"].append(value)

    out = []
    for key, source in sources.items():
        cited = cited_for[key]
        latitude, longitude = (normalize_scalar(cited.get(name)) for name in COORDINATE_ATTRIBUTES)
        name = normalize_scalar(cited.get("found_facility_name"))
        source.setdefault("facility_name_in_source", name)
        source.setdefault("facility_name_english", "")
        if latitude and longitude:
            source["stated_location"] = f"{latitude}, {longitude}"
            source["stated_location_kind"] = "coordinates"
        else:
            source.setdefault("stated_location", "")
            source.setdefault("stated_location_kind", "")
        out.append(source)
    return out


def merge_payloads(payloads: list[Any]) -> dict[str, Any]:
    """Combine every pass's response for one site into one read payload.

    Sources and leads accumulate across passes; search notes are joined so a tool failure in any
    pass stays visible. The passes are joined before they are read, so attribute-first passes
    convert as one response, numbered as they always were.
    """
    if len(payloads) == 1:
        return read_payload(payloads[0])
    combined: dict[str, Any] = {"unverified_leads": []}
    problems: list[str] = []
    notes: list[str] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            problems.append("Response is not a JSON object.")
            continue
        combined.setdefault("site_id", payload.get("site_id"))
        if payload.get(_READ):
            problems.extend(payload["problems"])
        for field in ("attributes", "sources"):
            if field in payload:
                part = payload[field]
                if isinstance(part, list):
                    combined.setdefault(field, []).extend(part)
                else:
                    problems.append(f"Payload field {field!r} must be a list.")
        leads = payload.get("unverified_leads") or []
        combined["unverified_leads"].extend(leads if isinstance(leads, list) else [leads])
        note = normalize_scalar(payload.get("search_notes"))
        if note:
            notes.append(note)
    combined["search_notes"] = " | ".join(notes)
    merged = read_payload(combined)
    merged["problems"][:0] = problems
    return merged


def citations(payload: Any) -> list[tuple[int, dict[str, Any], int, dict[str, Any]]]:
    """Every (source number, source, value number, value), in the order the agent gave them.

    A response converted from the attribute-first shape keeps that shape's order: attribute by
    attribute, each attribute's sources in turn.
    """
    found = [
        (source_index, source, value_index, value)
        for source_index, source in enumerate(read_payload(payload)["sources"], start=1)
        for value_index, value in enumerate(source["values"], start=1)
    ]
    return sorted(found, key=lambda item: (0, item[3][_ORDER]) if _ORDER in item[3] else (1, 0))


def citation_ids(site_id: str, source_index: int, value_index: int, value: dict[str, Any]) -> tuple[str, str]:
    """A citation's evidence id, and the source id it falls back on when its source has no URL.

    A converted response keeps its old ids, numbered by attribute and citation. A source-first one
    is numbered by source and value, and a source with no URL is one source, whatever it cites.
    """
    if value.get(_CITATION):
        return f"WAIEV-{site_id}-{value[_CITATION]}", f"WAISRC-{site_id}-{value[_CITATION]}"
    return f"WAIEV-{site_id}-S{source_index:03d}-{value_index:03d}", f"WAISRC-{site_id}-S{source_index:03d}"


def found_attributes(payload: Any) -> list[tuple[dict[str, Any], str]]:
    """Every (source, attribute name) a response gives a value for, in the current spelling."""
    return [
        (source, canonical_attribute(normalize_scalar(value.get("attribute_name"))))
        for source in read_payload(payload)["sources"]
        for value in source["values"]
    ]


def payload_urls(payload: Any) -> list[str]:
    """Every source URL in a response, as the agent wrote it."""
    urls = [normalize_scalar(source.get("url")) for source in read_payload(payload)["sources"]]
    return [url for url in urls if url]


def is_empty(payload: Any) -> bool:
    """Whether a response reports no source at all."""
    return not read_payload(payload)["sources"]
