from __future__ import annotations

import json
from typing import Any

from .credibility import (
    SOURCE_TYPES,
    TIER_DEFINITIONS,
    trusted_baseline_coordinates,
    trusted_baseline_name,
)
from .schema import (
    CONTRADICTION_CHECK_SOURCES,
    CORROBORATING_ATTRIBUTES,
    FACILITY_TYPE_ALLOWED,
    NOT_A_WASTE_FACILITY,
    COORDINATES_ONLY_ISO3,
    COORDINATE_ATTRIBUTES,
    DEFINITION_VALUES,
    GCCS_ATTRIBUTES,
    normalize_scalar,
    parse_tristate_bool,
    GAP_FILL_ATTRIBUTES,
    IDENTITY_ATTRIBUTES,
    UNSEARCHED_ATTRIBUTES,
    is_blank,
)


# Only these units are ever requested, so the agent is never asked to invent one.
ATTRIBUTE_UNITS = {
    "annual_incoming_waste_metric_tonnes": "metric tonnes/year",
    "waste_in_place_metric_tonnes": "metric tonnes",
    "found_latitude": "decimal degrees",
    "found_longitude": "decimal degrees",
    "opening_year": "year",
    "closing_year": "year",
    "waste_depth": "meters",
    "area_square_meters": "square meters",
    "gccs_ch4_flared_metric_tonnes": "metric tonnes CH4",
    "gccs_ch4_generated_metric_tonnes": "metric tonnes CH4",
    "gccs_ch4_collected_metric_tonnes": "metric tonnes CH4",
    "gccs_ch4_flow_to_project_metric_tonnes": "metric tonnes CH4",
    "gccs_collection_efficiency": "fraction between 0 and 1",
    "flare_efficiency": "fraction between 0 and 1",
}

# Definitions for each facility_type (WP-531). Labelled with the enum values exactly, since the
# agent must answer with those strings; the SME's "open dumpsite" is the `Dumpsite` value.
#
# Prompted by pilot site 14146, "DIPOSIT CONTROLAT DE MANRESA (II)": the agent read Catalan
# "dipòsit controlat" literally as Controlled Dumpsite, but the site is a sanitary landfill. The
# definitions say what each type IS; the last sentence is what stops the name from deciding it.
FACILITY_TYPE_DEFINITIONS = (
    " Definitions: "
    "Dumpsite - an open dumpsite: an unmanaged area where mixed waste is disposed of without "
    "liners or cover systems, allowing anaerobic decomposition under uncontrolled conditions that "
    "lead to diffuse methane emissions. "
    "Controlled Dumpsite - a dumpsite with some operational measures to control emissions, such as "
    "compaction, limited soil cover, or restricted access; some have simple gas collection "
    "equipment installed. "
    "Sanitary Landfill - a fully engineered facility with liners, leachate and groundwater "
    "management, and often but not necessarily a landfill gas management system. "
    # Added after the live check on Manresa (18133): the agent found compaction, read it against the
    # Controlled Dumpsite definition, and downgraded an engineered landfill.
    "Compaction, soil cover and restricted access occur at BOTH controlled dumpsites and sanitary "
    "landfills, so none of them distinguishes the two. What decides it is engineered containment: "
    "a liner, leachate collection and groundwater monitoring. "
    "Classify from what sources say about the facility's engineering and operation, NEVER from "
    "words in its name. Names are often regulatory terms or literal translations: Catalan "
    "'dipòsit controlat' and Spanish 'vertedero controlado' can describe a fully engineered "
    "sanitary landfill, and 'controlled' in a name is not evidence of a Controlled Dumpsite."
)

ATTRIBUTE_GUIDANCE = {
    "facility_status": "Allowed values only: Active, Inactive. A closed or former site is Inactive.",
    "facility_type": (
        "Allowed values only: Sanitary Landfill, Controlled Dumpsite, Dumpsite, Incineration Facility."
        + FACILITY_TYPE_DEFINITIONS
    ),
    "cover_types": "Array. Allowed members only: clay cover, organic cover, sand cover, other soil mixture.",
    "has_landfill_gas_collection": "Yes, No, or Unknown.",
    "operator": (
        "The organisation that runs the facility day to day - a company, municipality, utility "
        "or public agency - as the source names it. If the source distinguishes them, give the "
        "operator, not the owner, landlord or regulator. Text only; if a concession changed hands, "
        "give the operator for the source's value_date."
    ),
    "opening_year": "Four-digit year the facility opened.",
    "closing_year": (
        "Four-digit year the facility closed or is expected to close. REQUIRED whenever the "
        "facility is closed or inactive - a closure date almost always exists in news coverage, "
        "a council resolution, or a closure order. Search for it specifically."
    ),
    "annual_incoming_waste_metric_tonnes": (
        "Waste accepted per year. Report the source's original unit; the pipeline converts. "
        "Distinguish metric tonnes from US short tons. If the source just says 'tons', keep the "
        "number in value and set unit to 'ambiguous tons'. Never put words in value."
    ),
    "waste_in_place_metric_tonnes": (
        "Total waste mass in place (a stock, not a rate). Report the source's original unit."
    ),
    "area_square_meters": (
        "Surface area of the landfill footprint containing waste. Report the source's original "
        "unit - hectares, acres, square feet, square kilometres or square metres - and the "
        "pipeline converts. This is the waste footprint, not the whole property or parcel."
    ),
    "waste_depth": (
        "Average depth of waste. Report the NUMBER and the source's original unit (feet or "
        "meters) - the pipeline converts to metres and bins the result into a depth category. "
        "Do not return a category yourself, and never put words or a band in value. Must be "
        "greater than zero: a reported zero is not a measurement."
    ),
    "has_cover": "Whether any cover material is present. Yes, No, or Unknown.",
    "has_biocover": (
        "Whether the facility has a biocover - an engineered organic or biologically active "
        "cover intended to oxidise methane. Yes, No, or Unknown. A plain soil, clay or sand "
        "cover is NOT a biocover; answer No for those. Do not infer one from the mere presence "
        "of cover material."
    ),
    "gccs_ch4_flared_metric_tonnes": (
        "Methane sent to flares. Report the source's original unit. If the source gives a gas "
        "VOLUME, say whether it is methane or raw landfill gas - raw landfill gas cannot be "
        "converted without a methane fraction, so give that fraction if the source states it."
    ),
    "gccs_ch4_generated_metric_tonnes": "Methane generated by the landfill. Same unit rules as flared.",
    "gccs_ch4_collected_metric_tonnes": "Methane captured by the collection system. Same unit rules as flared.",
    "gccs_ch4_flow_to_project_metric_tonnes": (
        "Methane routed to an energy recovery project. Same unit rules as flared."
    ),
    "gccs_energy_project_type": (
        "Array. Allowed members only: Electricity Generation, Direct Use, Renewable Natural Gas, "
        "Other. Flaring alone is NOT an energy project - leave empty if gas is only flared. "
        "(This 'Other' is a database enum value for project type, unrelated to source_type.)"
    ),
    "gccs_current_project_status": (
        "Array. Allowed members only: Operational, Under Construction, Planned, Closed."
    ),
    "gccs_collection_efficiency": (
        "Fraction of generated landfill gas that is collected, as a value between 0 and 1. "
        "If the source gives a percentage, report the number and set unit to '%'."
    ),
    "has_flare": "Whether the facility has a flare that burns collected landfill gas. Yes, No, or Unknown.",
    "flare_efficiency": (
        "Destruction efficiency of the flare: the fraction of the methane sent to it that is "
        "destroyed, as a value between 0 and 1. If the source gives a percentage, report the "
        "number and set unit to '%'."
    ),
    "bulk_waste_type": (
        "The type of waste that makes up MOST of what the facility receives. Allowed values only. "
        "municipal solid waste - waste from households, and similar waste from shops, offices, "
        "markets and institutions, collected by or for a municipality. It contains food, garden, "
        "paper and other material that decomposes. A site that mainly takes this but also some "
        "rubble or soil is still municipal solid waste. "
        "inert waste - waste that does not decompose or react: construction and demolition "
        "rubble, concrete, bricks, soil, stones and excavation material. A site permitted for "
        "inert waste only belongs here. "
        "others - mainly another kind of waste, such as industrial, hazardous or mining waste, or "
        "sewage sludge. "
        "If no source says what waste the facility receives, or no one type makes up most of it, "
        "do not return bulk_waste_type."
    ),
    "found_facility_name": "The source's exact official or canonical name for this facility.",
    "found_latitude": "Source-reported latitude only. Never infer from an address or nearby place.",
    "found_longitude": "Source-reported longitude only. Never infer from an address or nearby place.",
}


# Countries bound by the EU Landfill Directive (1999/31/EC): the EU27, plus Norway, Iceland and
# Liechtenstein through the EEA agreement. The UK is deliberately absent - it applies equivalent
# rules through retained domestic law, but is no longer bound by the Directive itself.
LANDFILL_DIRECTIVE_ISO3 = {
    "AUT", "BEL", "BGR", "HRV", "CYP", "CZE", "DNK", "EST", "FIN", "FRA", "DEU", "GRC", "HUN",
    "IRL", "ITA", "LVA", "LTU", "LUX", "MLT", "NLD", "POL", "PRT", "ROU", "SVK", "SVN", "ESP",
    "SWE", "ISL", "LIE", "NOR",
}

# Appended to facility_type's guidance for sites in those countries (WP-531). Existing landfills
# had until 16 July 2009 to meet the Directive or close, so the prior covers sites that accepted
# waste after that; an older closed site may never have been engineered.
LANDFILL_DIRECTIVE_GUIDANCE = (
    " This site is in a country bound by the EU Landfill Directive (1999/31/EC), which requires "
    "every landfill accepting non-hazardous waste to have a liner and leachate management. A "
    "landfill that accepted waste after July 2009 is therefore a Sanitary Landfill unless a source "
    "says it lacks that engineering; in this country that includes every landfill reporting to "
    "E-PRTR. A site that "
    "closed before July 2009 may predate these requirements; classify it from its described "
    "engineering instead."
)


# Appended to facility_type's guidance only for facilities nothing independently confirms (WP-525).
NOT_A_WASTE_FACILITY_GUIDANCE = (
    f" This site is known only from a map or satellite detection, so its type is unconfirmed. If "
    f"a source positively states it is, and always was, something other than a waste disposal "
    f"facility - a quarry, a mine, an aggregate or construction yard that never accepted waste, or "
    f"a feature mapped in the wrong place - return facility_type = '{NOT_A_WASTE_FACILITY}', "
    f"describe what the site actually is in evidence_summary, and quote the source. "
    f"A CLOSED, CAPPED, RECLAIMED, REVEGETATED OR REDEVELOPED LANDFILL IS STILL A WASTE DISPOSAL "
    f"FACILITY: buried waste keeps generating methane for decades, which is why the site is "
    f"tracked. A landfill that is now a park, a solar farm or a sports ground keeps its landfill "
    f"or dumpsite type, and its closure goes in facility_status = Inactive and closing_year. The "
    f"question is 'was this ever a waste disposal site?', not 'is it operating today?'. If no "
    f"source says the site was never a waste facility, never return '{NOT_A_WASTE_FACILITY}' - "
    f"finding nothing about a site is not evidence against it."
)


# Name fragments that signal a closed site, so the prompt can push on closing_year up front.
CLOSURE_NAME_HINTS = ("closed", "close)", "former", "abandoned", "decommission", "shut")


def looks_inactive(site: dict[str, Any]) -> bool:
    """Whether we already have reason to believe this facility is closed."""
    if normalize_scalar(site.get("facility_status")).lower() == "inactive":
        return True
    name = normalize_scalar(site.get("site_name")).lower()
    return any(hint in name for hint in CLOSURE_NAME_HINTS)


def has_gas_collection(site: dict[str, Any]) -> bool:
    """Whether the baseline states this facility has a gas collection system.

    GCCS attributes are requested only when this is true. Unknown counts as not-yet-known rather
    than absent, and is picked up on a later pass: `has_landfill_gas_collection` is itself
    gap-filled, so once a pass establishes it, `refresh-seed` promotes it into the baseline and
    the next search requests that facility's GCCS attributes. Nothing is lost, only deferred.
    """
    return parse_tristate_bool(site.get("has_landfill_gas_collection"))[0] is True


def coordinates_only(site: dict[str, Any]) -> bool:
    """Whether this facility's country is scoped to coordinates alone."""
    return normalize_scalar(site.get("country_iso3")).upper() in COORDINATES_ONLY_ISO3


def location_is_inexact(site: dict[str, Any]) -> bool:
    """Whether the recorded location is positively flagged as not exact.

    Unknown is not inexact: only an explicit FALSE asks for a coordinate search.
    """
    return parse_tristate_bool(site.get("is_location_exact"))[0] is False


def needs_contradiction_check(site: dict[str, Any]) -> bool:
    """Whether nothing independently confirms this is a disposal site (WP-525).

    True when every contributing source is in CONTRADICTION_CHECK_SOURCES and none of the
    corroborating attributes has a value. Once a pass fills one of those, the site has
    corroboration and is not asked again. Corroborated facilities never see the question, so
    their prompts carry none of its guidance.
    """
    sources = [
        token.strip().lower()
        for token in normalize_scalar(site.get("contributing_data_sources")).split("+")
        if token.strip()
    ]
    if not sources or not all(source in CONTRADICTION_CHECK_SOURCES for source in sources):
        return False
    return all(is_blank(site.get(field)) for field in CORROBORATING_ATTRIBUTES)


def requested_attributes(site: dict[str, Any]) -> list[str]:
    """Identity always; metadata only where the facility's baseline is empty (gap-fill, Q29).

    GCCS attributes are requested only for facilities known to have a gas collection system.
    Some countries are scoped to coordinates alone (COORDINATES_ONLY_ISO3).
    """
    if coordinates_only(site):
        # Coordinates are worth confirming only where the recorded location is flagged inexact;
        # everything else for these facilities is government-sourced and left alone.
        return list(COORDINATE_ATTRIBUTES) if location_is_inexact(site) else []

    # Identity is always requested, except what a Tier 1-2 source already supplied: the name
    # (WP-531) and the coordinates (F29). Coordinates flagged inexact are still searched.
    known: set[str] = set()
    if trusted_baseline_name(site):
        known.add("found_facility_name")
    if trusted_baseline_coordinates(site) and not location_is_inexact(site):
        known.update(COORDINATE_ATTRIBUTES)
    requested = [name for name in IDENTITY_ATTRIBUTES if name not in known]
    gas_present = has_gas_collection(site)
    for field in GAP_FILL_ATTRIBUTES:
        if field in UNSEARCHED_ATTRIBUTES or not is_blank(site.get(field)):
            continue
        if field in GCCS_ATTRIBUTES and not gas_present:
            continue
        requested.append(field)
    return requested


def search_budget(max_web_searches: int) -> str:
    """Prompt text capping the agent's web searches, which Bing bills one by one (WP-545)."""
    if max_web_searches <= 0:
        return ""
    return f"""
Search budget:
- Use at most {max_web_searches} web searches in total for this site. Plan them before you start.
- One good source often answers several attributes at once (a permit, licence, inventory or
  operator record usually states status, type, dates and capacity together), so search for such
  records first, in the local language first.
- Once an attribute has an authoritative source (regulator, government or operator), stop
  searching for it.
- When the budget is spent, stop and return what you have. Leave unfound attributes out.
"""


def build_site_prompt(
    site: dict[str, Any], attributes: list[str] | None = None, max_web_searches: int = 0
) -> str:
    attributes = attributes if attributes is not None else requested_attributes(site)

    site_context = {
        key: site.get(key)
        for key in (
            "site_id",
            "site_name",
            "original_site_name",
            "source_language",
            "country_iso3",
            "latitude",
            "longitude",
            "input_area_square_meters",
            "municipality",
            "admin1",
            "admin2",
            "formatted_address",
        )
        if site.get(key)
    }

    # Only a facility nothing confirms is offered "Not a Waste Facility". Every other prompt carries
    # the four database values and none of the addendum.
    unconfirmed_type = needs_contradiction_check(site)

    under_landfill_directive = (
        normalize_scalar(site.get("country_iso3")).upper() in LANDFILL_DIRECTIVE_ISO3
    )

    def guidance_for(name: str) -> str:
        text = ATTRIBUTE_GUIDANCE.get(name, "")
        if name == "facility_type" and unconfirmed_type:
            text = (
                f"Allowed values only: {', '.join(FACILITY_TYPE_ALLOWED)}."
                + FACILITY_TYPE_DEFINITIONS
                + NOT_A_WASTE_FACILITY_GUIDANCE
            )
        if name == "facility_type" and under_landfill_directive:
            text += LANDFILL_DIRECTIVE_GUIDANCE
        return text

    targets = [
        {
            "attribute_name": name,
            "unit": ATTRIBUTE_UNITS.get(name, ""),
            "guidance": guidance_for(name),
        }
        for name in attributes
    ]

    # Three cases, because "came through translation" and "has a second spelling" are separate
    # facts. A name from a Portuguese source that translated to itself still needs to be searched
    # in Portuguese, and calling it untranslated throws that away.
    original_name = normalize_scalar(site.get("original_site_name"))
    language = normalize_scalar(site.get("source_language"))
    translated_source = bool(language) and language.lower() not in {"en", "eng", "english"}

    if original_name:
        names_guidance = """
Names:
- site_name has been machine-translated into English. It is often NOT what local
  sources call this facility, and searching it alone is the most common reason a
  real site looks like it has no coverage.
- original_site_name is the name the source dataset actually recorded, in
  source_language. Search THIS name in the local language first - permits,
  municipal records and local news will use it, not the translation.
- Search both. Treat them as one facility with two names, never as two
  candidates: agreeing on the translated name alone is not identity confirmation.
- Return found_facility_name exactly as your source spells it, in whatever script or
  language that source uses. Do not translate it back.
"""
    elif translated_source:
        # The name survived translation unchanged - usually a proper noun - so there is no second
        # spelling to give. The source language is still known and still worth searching in.
        # The code is passed through rather than mapped to a language name: the corpus carries
        # 61 distinct ISO 639-1 codes and a hand-kept table of names would rot as sources arrive.
        names_guidance = f"""
Names:
- site_name came from a source written in '{language}' (ISO 639-1) and was
  unchanged by translation, so it is also the source's own spelling. There is no
  second name to search.
- Search it in that language as well as in English. Local permits, municipal
  records and news coverage are written in it, and are where this site is
  documented.
- Return found_facility_name exactly as your source spells it, in whatever script or
  language that source uses. Do not translate it back.
"""
    else:
        names_guidance = """
Names:
- site_name is the name the source dataset recorded for this facility; it has not
  been translated. Search it as given, and in the local language too.
- Return found_facility_name exactly as your source spells it, in whatever script or
  language that source uses. Do not translate it back.
"""

    asks_name = "found_facility_name" in attributes
    asks_coordinates = "found_latitude" in attributes
    # Not asked is not the same as trusted: the gas-capture follow-up asks for GCCS alone, so only
    # a Tier 1-2 provenance lets the prompt call the seed's identity confirmed.
    name_confirmed = not asks_name and trusted_baseline_name(site)
    coordinates_confirmed = not asks_coordinates and trusted_baseline_coordinates(site)
    if not asks_name:
        # Asking the agent to return a name anyway would contradict "only the attributes listed
        # below are wanted".
        names_guidance = names_guidance.replace(
            "- Return found_facility_name exactly as your source spells it, in whatever script or\n"
            "  language that source uses. Do not translate it back.\n",
            "- site_name is already confirmed by an authoritative source. Use it to search; do not\n"
            "  return a facility name.\n"
            if name_confirmed
            else "- Use site_name to search; do not return a facility name.\n",
        )
    if asks_name and asks_coordinates:
        identity_rule = (
            "- found_facility_name, found_latitude and found_longitude establish that you found the\n"
            "  RIGHT facility. Always return them when a source supports them."
        )
    elif asks_coordinates:
        identity_rule = (
            "- found_latitude and found_longitude establish that you found the RIGHT facility.\n"
            "  Always return them when a source supports them. "
            + ("The name is already confirmed." if name_confirmed else "Do not return a name.")
        )
    elif asks_name:
        identity_rule = (
            "- found_facility_name establishes that you found the RIGHT facility. Always return it\n"
            "  when a source supports it. "
            + ("The coordinates are already confirmed: use them" if coordinates_confirmed else "Use the coordinates")
            + " to check\n  that each source describes this facility, and do not return coordinates."
        )
    elif name_confirmed and coordinates_confirmed:
        identity_rule = (
            "- The name and coordinates are already confirmed by an authoritative source. Use them\n"
            "  to check that each source describes THIS facility, and do not return a name or\n"
            "  coordinates."
        )
    else:
        identity_rule = (
            "- The name and coordinates are not asked for here. Use them to check that each source\n"
            "  describes THIS facility, and do not return a name or coordinates."
        )

    closure_focus = ""
    if "closing_year" in attributes and looks_inactive(site):
        closure_focus = (
            "\nThis facility appears to be closed or inactive already, so a closure date should\n"
            "exist. Search specifically for it - closure orders, council resolutions, news\n"
            "coverage of the shutdown - and return closing_year. Say in search_notes what you\n"
            "tried if you genuinely cannot find one.\n"
        )

    output_contract = {
        "site_id": site.get("site_id"),
        "attributes": [
            {
                "attribute_name": "One attribute_name from the Target attributes list",
                "value": "Candidate value",
                "unit": "Source's original unit, or blank",
                "value_basis": "Direct / Inferred / Conflicting / Not found",
                "confidence_score": "High / Medium / Low / Excluded candidate",
                "value_date": "YYYY-MM-DD, YYYY-MM, or YYYY",
                "evidence_summary": "Short source-backed explanation",
                "search_terms_used": "Search terms and languages used",
                "sources": [
                    {
                        "source_title": "Title",
                        "publisher": "Publisher, operator, or regulator",
                        "source_type": "One value from Definitions.source_type",
                        "publication_date": "YYYY-MM-DD or blank",
                        "url": "https://...",
                        "language": "English / local language",
                        "paywall_flag": False,
                        "quoted_evidence_short": "Short quote from the source",
                    }
                ],
            }
        ],
        "unverified_leads": [],
        "search_notes": "Languages, search terms, and gaps.",
    }

    definitions = {
        "attribute_name": attributes,
        "source_type": SOURCE_TYPES,
        "source_tier_rubric": TIER_DEFINITIONS,
        "facility_status": DEFINITION_VALUES["facility_status"],
        "facility_type": DEFINITION_VALUES["facility_type"],
        "cover_type": DEFINITION_VALUES["cover_type"],
        "gccs_energy_project_type": DEFINITION_VALUES["gccs_energy_project_type"],
        "gccs_current_project_status": DEFINITION_VALUES["gccs_current_project_status"],
        "bulk_waste_type": DEFINITION_VALUES["bulk_waste_type"],
        "boolean_unknown": DEFINITION_VALUES["boolean_unknown"],
        "value_basis": DEFINITION_VALUES["value_basis"],
        "confidence_score": DEFINITION_VALUES["confidence_score"],
    }
    if not unconfirmed_type:
        # DEFINITION_VALUES carries the pending value for the workbook; a prompt that is not
        # offering it must not list it either.
        definitions["facility_type"] = [
            value for value in definitions["facility_type"] if value != NOT_A_WASTE_FACILITY
        ]

    return f"""You are a waste-sector data discovery agent for WasteMAP.

Task:
Find source-backed data for this site, recorded as a waste disposal facility. Search
in English and in the local language, using the site name, coordinates,
municipality, and admin names.
{search_budget(max_web_searches)}{names_guidance}
Only the attributes listed below are wanted. Everything else about this facility is
already known and must not be researched or returned.

Target attributes:
{json.dumps(targets, indent=2, ensure_ascii=False)}

Site context:
{json.dumps(site_context, indent=2, ensure_ascii=False)}

Definitions:
{json.dumps(definitions, indent=2, ensure_ascii=False)}

{closure_focus}
Status and closure:
- If your sources show the facility is closed, inactive, or no longer accepting waste, return
  facility_status = Inactive AND search for closing_year in the same pass. A closed site
  without a closure year is an incomplete answer.

Identity first:
{identity_rule}
- If you cannot confirm you found this specific facility, return an empty attributes
  list and explain why in search_notes. Do not return data for a different site.

Source requirements:
- Every returned attribute needs at least one clickable source URL.
- Classify each source with source_type from Definitions.source_type. Do not invent
  new source types and do not combine two with a slash. There is no catch-all: if no
  listed type genuinely fits the source, put the finding in unverified_leads rather
  than guessing a type.
- Always give publication_date when the source shows one. An undated source is
  treated as less credible, so a date materially changes how the value is used.
- Do not assign a source tier. The pipeline assigns credibility from source_type
  and publisher.
- Attach every supporting source to the attribute. Agreement between independent
  sources is recorded, so list them all rather than only the best one.

Value requirements:
- Use only the allowed categorical values in Definitions. If the true value is not
  among them, put it in unverified_leads instead of forcing a wrong category.
- Report the source's original unit in the unit field. The pipeline converts.
- value must hold only the value itself. For numeric attributes that means digits, never
  words, ranges in prose, or unit text.
- Gas capture attributes only apply if the facility actually has a gas collection system. If it
  does not, omit them rather than returning zeros.
- Label anything not directly stated by the source as value_basis = Inferred.
- Put uncertain or unsourced findings in unverified_leads, not attributes.
- If nothing is found, return an empty attributes list and explain in search_notes.

Output requirements:
- Return strict JSON only. No markdown, no prose outside JSON.

JSON output contract:
{json.dumps(output_contract, indent=2, ensure_ascii=False)}
"""
