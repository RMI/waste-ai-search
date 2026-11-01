from __future__ import annotations

import json
from typing import Any

from .credibility import (
    SOURCE_TYPES,
    TIER_DEFINITIONS,
    trusted_baseline_coordinates,
    trusted_baseline_name,
)
from .geocoder import NEAR_KM, OTHER_SITES_FIELD, SAME_POINT_FIELD
from .locate import prompt_point
from .schema import (
    CONTRADICTION_CHECK_SOURCES,
    CORROBORATING_ATTRIBUTES,
    FACILITY_TYPE_ALLOWED,
    FACILITY_TYPE_OFFERED,
    NOT_A_WASTE_FACILITY,
    COORDINATES_ONLY_ISO3,
    DEFINITION_VALUES,
    GCCS_ATTRIBUTES,
    normalize_scalar,
    parse_tristate_bool,
    GAP_FILL_ATTRIBUTES,
    UNSEARCHED_ATTRIBUTES,
    is_blank,
)


# Only these units are ever requested, so the agent is never asked to invent one.
ATTRIBUTE_UNITS = {
    "annual_incoming_waste_metric_tonnes": "metric tonnes/year",
    "waste_in_place_metric_tonnes": "metric tonnes",
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
    # The standardized spec's type for a site a model of municipal waste leaves out (Hugh, 30 Oct).
    "Non-Municipal Waste Landfill - a disposal site taking mainly waste other than municipal "
    "solid waste: construction and demolition rubble, soil, or industrial, hazardous or mining "
    "waste. What it takes decides this, whatever its engineering. "
    # Added after the live check on Manresa (18133): the agent found compaction, read it against the
    # Controlled Dumpsite definition, and downgraded an engineered landfill.
    "Compaction, soil cover and restricted access occur at BOTH controlled dumpsites and sanitary "
    "landfills, so none of them distinguishes the two. What decides it is engineered containment: "
    "a liner, leachate collection and groundwater monitoring. "
    # In the 31 October Mexico tests, Sol gave Controlled Dumpsite 7 times from a fence, compaction or
    # criticism, and 6 were wrong. The two types are named so a 'tiradero a cielo abierto' page can
    # still give Dumpsite.
    "If no source says whether it has them, choose neither Controlled Dumpsite nor Sanitary "
    "Landfill: leave facility_type out. "
    "Classify from what sources say about the facility's engineering and operation, NEVER from "
    "words in its name. Names are often regulatory terms or literal translations: Catalan "
    "'dipòsit controlat' and Spanish 'vertedero controlado' can describe a fully engineered "
    "sanitary landfill, and 'controlled' in a name is not evidence of a Controlled Dumpsite. "
    "Transfer Station - a site where collected waste is unloaded, consolidated and reloaded for "
    "transport to a disposal site; no waste is disposed of there. "
    "Recycling Center - a site where recyclable or separated waste is dropped off, sorted or "
    "processed for recovery, including drop-off centres and materials recovery facilities; no "
    "waste is disposed of there. "
    # Site 2529, "Wonthaggi Waste Transfer & Recycling Centre": many sites are both.
    "A site that is both a transfer station and a recycling centre is a Transfer Station if it "
    "takes mixed (unsorted) waste, and a Recycling Center only if it takes nothing but separated "
    "recyclables. "
    "If waste was ever landfilled or dumped on the site - for example a transfer station or "
    "recycling centre built on a closed landfill - classify the disposal site, not the facility "
    "now on it: the buried waste still emits."
)

ATTRIBUTE_GUIDANCE = {
    # In the 31 October Mexico tests, 12 statuses came from pages that never show whether waste
    # still arrives: fire reports, a gas-chimney inauguration, a law's date line, a plan. 'Shows',
    # not 'says': Active from works pages that show the site in use was judged supported.
    "facility_status": (
        "Allowed values only: Active, Inactive. A closed or former site is Inactive. Status changes "
        "over time, so base it on the most recent source that shows whether the site still takes "
        "waste - a closure notice, council resolution, current opening hours or fees page, or news "
        "- not an older permit, inventory or directory listing. A fire there does not show it: "
        "closed dumps burn too. Give that source's date in value_date."
    ),
    "facility_type": (
        f"Allowed values only: {', '.join(FACILITY_TYPE_OFFERED)}."
        + FACILITY_TYPE_DEFINITIONS
    ),
    # In the 31 October Mexico tests, a cell's figure or cover was taken for the whole site's: 6,000 m2
    # of a new cell's geomembrane as the area, and tepetate on a COVID clinical-waste cell as the
    # cover. Not the type: Sanitary Landfill read from the cell in use was judged supported 4 times.
    "cover_types": (
        "Array. Allowed members only: clay cover, organic cover, sand cover, other soil mixture. "
        "The cover used across the site, not one cell's."
    ),
    # In test10-mex-sample2-run2 an order to "implement ... biogas management" at Naucalpan was
    # read as Yes, which set off the gas follow-up search (Hugh, 30 October).
    "has_landfill_gas_collection": (
        "Whether the landfill has a system collecting its gas through wells and pipes, to a flare "
        "or an engine. Yes only where the source says one exists or existed; not one that is "
        "ordered, required, planned or proposed, and not vents that only let gas escape. Yes, No, "
        "or Unknown."
    ),
    # In the 31 October Mexico tests, 17 operators named a body the page mentions in another role,
    # such as leading a supervision tour or lending machinery for a clean-up, or gave a description:
    # 'empresa que opera el relleno sanitario', 'Autoridad Municipal'.
    "operator": (
        "The organisation the source says runs the facility day to day - a company, municipality, "
        "utility or public agency - by its full name as the source writes it, never a description "
        "('la empresa', 'autoridad municipal'). Not a body the source names only for owning, "
        "regulating, inspecting or visiting it, hauling waste to it, building, cleaning up or "
        "closing it, or giving a figure. Text only; if a concession changed hands, give the "
        "operator for the source's value_date."
    ),
    "opening_year": (
        "Four-digit year the facility first accepted waste. Not the date of a permit or licence, "
        "or of a later stage, cell or extension opening."
    ),
    "closing_year": (
        "Four-digit year the facility closed, meaning it stopped accepting waste. REQUIRED whenever "
        "the facility is closed or inactive - a closure date almost always exists in news coverage, "
        "a council resolution, or a closure order that was carried out. Search for it specifically. "
        "If the facility is still open and a source gives the year it is planned or expected to "
        "close, return that year with planned = true; an actual closure is planned = false."
    ),
    "annual_incoming_waste_metric_tonnes": (
        "Waste actually accepted per year - not a permitted or licensed limit or a design "
        "capacity. Report the source's original unit; the pipeline converts. "
        "Distinguish metric tonnes from US short tons. If the source just says 'tons', keep the "
        "number in value and set unit to 'ambiguous tons'. Never put words in value. "
        # The standardized spec's rule (Hugh, 30 October): municipal waste only, as far as the
        # source allows; where it doesn't say, the amount is taken as municipal.
        "Municipal solid waste only. Where the source splits the amount by kind of waste, return "
        "the municipal part. Where it says the amount is mainly other waste - construction and "
        "demolition rubble, soil, quarry or mining waste, industrial or hazardous waste, or "
        "sludge - return it with not_municipal = true. Where it doesn't say, return it as written."
    ),
    "waste_in_place_metric_tonnes": (
        "Total waste mass actually in place (a stock, not a rate) - not a permitted or design "
        "capacity. Report the source's original unit."
    ),
    "area_square_meters": (
        "Surface area of the landfill footprint containing waste. Report the source's original "
        "unit - hectares, acres, square feet, square kilometres or square metres - and the "
        "pipeline converts. This is the waste footprint, not the whole property or parcel, nor one "
        "cell or extension."
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
    # The follow-up search then read Naucalpan's own status as its gas project's: Operational
    # because waste was arriving, Closed because the site had been sealed (Hugh, 30 October).
    "gccs_current_project_status": (
        "Array. Allowed members only: Operational, Under Construction, Planned, Closed. The status "
        "of each landfill gas energy project, never of the landfill itself: a landfill that is "
        "operating, closed or sealed says nothing of its gas project."
    ),
    "gccs_collection_efficiency": (
        "Fraction of generated landfill gas that is collected, as measured or reported for the "
        "facility - not a design or target value - as a value between 0 and 1. "
        "If the source gives a percentage, report the number and set unit to '%'."
    ),
    "has_flare": "Whether the facility has a flare that burns collected landfill gas. Yes, No, or Unknown.",
    "flare_efficiency": (
        "Destruction efficiency of the flare: the fraction of the methane sent to it that is "
        "destroyed, as measured or reported - not a design or required minimum - as a value "
        "between 0 and 1. If the source gives a percentage, report the "
        "number and set unit to '%'."
    ),
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
# waste after that; an older closed site may never have been engineered. The prior holds where no
# source describes the engineering, which would otherwise leave facility_type out.
LANDFILL_DIRECTIVE_GUIDANCE = (
    " This site is in a country bound by the EU Landfill Directive (1999/31/EC), which requires "
    "every landfill accepting non-hazardous waste to have a liner and leachate management. A "
    "landfill that accepted waste after July 2009 is therefore a Sanitary Landfill unless a source "
    "says it lacks that engineering, even where no source describes it; in this country that "
    "includes every landfill reporting to E-PRTR. A site that "
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
    """Metadata only where the facility's baseline is empty (gap-fill, Q29).

    The facility's name and location are not asked for (step 1 of the approve-sources plan). The
    agent returns only sources about this facility, as batch 1 did, and each states the name,
    address and coordinates it gives; those are used to check the match and never replace ours.
    GCCS attributes are requested only for facilities known to have a gas collection system. A
    country scoped to coordinates alone (COORDINATES_ONLY_ISO3) has nothing left to ask, so its
    sites are not searched.
    """
    if coordinates_only(site):
        # Everything about these facilities is government-sourced and left alone. Their coordinates
        # were the one thing worth confirming, and a location is no longer asked for.
        return []
    requested: list[str] = []
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
            "input_area_square_meters",
            "municipality",
            "admin1",
            "admin2",
            SAME_POINT_FIELD,
            OTHER_SITES_FIELD,
        )
        if site.get(key)
    }
    # Our coordinates, rounded to about 1 km: enough to search with, and nothing worth copying. In
    # batch 1, with the full point in the prompt, the agent credited our own OSM centroid to a
    # registry. The street address a reverse geocode gives would leak the point just as well, so
    # formatted_address is left out too.
    point = prompt_point(site)
    if point is not None:
        site_context["latitude"], site_context["longitude"] = point

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
    # In the 31 October Mexico tests none of the 213 sources cited was in English and none printed
    # decimal coordinates, yet the agents spent searches of their six on both: Terra searched
    # English names at 43 of 50 sites, and Sol ran out of budget "before a separate English-name
    # query". So a Spanish-language site is searched in Spanish, and our coordinates only check a
    # source's place. Other languages keep searching in English until their batches are measured.
    spanish = language.lower() == "es"
    coordinates_for = "checking places" if spanish else "searching"
    if spanish:
        search_how = (
            "Search\nin the local language, using the site's names, municipality and admin names. Use the\n"
            "approximate coordinates only to check a source's place: pages almost never print them, so\n"
            "never put them in a search."
        )
        if GCCS_ATTRIBUTES & set(attributes):
            search_how += (
                "\nGas-project records (UNFCCC CDM, Global Methane Initiative) are often in English:\n"
                "search those in English."
            )
    else:
        search_how = (
            "Search\nin English and in the local language, using the site name, approximate coordinates,\n"
            "municipality, and admin names."
        )

    if original_name:
        search_both = (
            "- Don't spend searches on site_name: it is our own translation and no source uses\n"
            "  it. Treat them as one facility with two names, never as two candidates: agreeing\n"
            "  on the translated name alone is not identity confirmation."
            if spanish else
            "- Search both. Treat them as one facility with two names, never as two\n"
            "  candidates: agreeing on the translated name alone is not identity confirmation."
        )
        names_guidance = f"""
Names:
- site_name has been machine-translated into English. It is often NOT what local
  sources call this facility, and searching it alone is the most common reason a
  real site looks like it has no coverage.
- original_site_name is the name the source dataset actually recorded, in
  source_language. Search THIS name in the local language first - permits,
  municipal records and local news will use it, not the translation.
{search_both}
- Give each source's own name for the facility as facility_name_in_source, exactly as
  that source spells it, in whatever script or language it uses. Do not translate it back.
"""
    elif translated_source:
        # The name survived translation unchanged - usually a proper noun - so there is no second
        # spelling to give. The source language is still known and still worth searching in.
        # The code is passed through rather than mapped to a language name: the corpus carries
        # 61 distinct ISO 639-1 codes and a hand-kept table of names would rot as sources arrive.
        in_that_language = (
            "- Search it in that language. Local permits, municipal records and news coverage\n"
            "  are written in it, and are where this site is documented."
            if spanish else
            "- Search it in that language as well as in English. Local permits, municipal\n"
            "  records and news coverage are written in it, and are where this site is\n"
            "  documented."
        )
        names_guidance = f"""
Names:
- site_name came from a source written in '{language}' (ISO 639-1) and was
  unchanged by translation, so it is also the source's own spelling. There is no
  second name to search.
{in_that_language}
- Give each source's own name for the facility as facility_name_in_source, exactly as
  that source spells it, in whatever script or language it uses. Do not translate it back.
"""
    else:
        names_guidance = """
Names:
- site_name is the name the source dataset recorded for this facility; it has not
  been translated. Search it as given, and in the local language too.
- Give each source's own name for the facility as facility_name_in_source, exactly as
  that source spells it, in whatever script or language it uses. Do not translate it back.
"""

    # Batch 1's identity rule. Not asked is not the same as trusted: nothing about identity is asked
    # now, so only a Tier 1-2 provenance lets the prompt call our name or coordinates confirmed.
    confirmed = {
        (True, True): "The name and coordinates are",
        (True, False): "The name is",
        (False, True): "The coordinates are",
    }.get((
        trusted_baseline_name(site),
        trusted_baseline_coordinates(site) and not location_is_inexact(site),
    ))
    confirmation = f"\n  {confirmed} already confirmed by an authoritative source." if confirmed else ""
    # A page about the town, or our other sites near this one where it has any
    # (geocoder.with_other_sites). In test10-mex-sample2, Las Varas was given pages about Madera's
    # new landfill (Hugh, 30 October).
    not_this_one = (
        f"the town's waste, another of its sites, or one of {OTHER_SITES_FIELD} (our other sites in this\n"
        f"  municipality or within {NEAR_KM:g} km)"
        if site.get(OTHER_SITES_FIELD) else "the town's waste or another of its sites"
    )
    same_point = (
        f"\n  {SAME_POINT_FIELD} may be this facility recorded twice: a page about one of them may be\n"
        "  about this one."
        if site.get(SAME_POINT_FIELD) else ""
    )

    # Only where status or the closing year is asked for: the gas follow-up wants neither, and
    # arbitration resolves whatever comes back, asked or not.
    status_and_closure = ""
    if {"facility_status", "closing_year"} & set(attributes):
        status_and_closure = (
            "Status and closure:\n"
            "- If your sources show the facility is closed, inactive, or no longer accepting waste, return\n"
            "  facility_status = Inactive AND search for closing_year in the same pass. A closed site's\n"
            "  answer is incomplete until you have looked for its closure year.\n"
            # In the 31 October Mexico tests, 15 statuses and closing years took a closure that had
            # not happened, or did not last, as done: a closure programme Profepa approved, an
            # agreement funding one, a replacement landfill's opening, a 'clausura temporal' with 15
            # days to comply, the article's own date. That a source expects a year is planned = true
            # is said in closing_year's guidance and the contract.
            "- A closure that is only ordered, recommended, planned, funded or expected, is temporary\n"
            "  (clausura temporal) or later reversed, or is a replacement site's opening does not make\n"
            "  the site Inactive or give an actual closing_year. closing_year is a year a source gives\n"
            "  for the closure, never the article's own date.\n\n"
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
        "sources": [
            {
                "url": "https://...",
                "source_title": "Title",
                "publisher": "Publisher, operator, or regulator",
                "source_type": "One value from Definitions.source_type",
                "publication_date": "YYYY-MM-DD or blank",
                "language": "English / local language",
                "paywall_flag": False,
                "facility_name_in_source": "The facility's name exactly as this source writes it, or blank",
                "facility_name_english": "That name in English (the same if already English), or blank",
                "stated_address": "The site's address exactly as this source writes it, or blank",
                "stated_coordinates": "The site's coordinates or grid reference exactly as this source writes them, or blank",
                "stated_municipality": "Town or municipality, or blank",
                "stated_state": "State or province, or blank",
                "values": [
                    {
                        "attribute_name": "One attribute_name from the Target attributes list",
                        "value": "Candidate value",
                        "unit": "Source's original unit, or blank",
                        "value_basis": "Direct / Inferred / Conflicting / Not found",
                        "confidence_score": "High / Medium / Low / Excluded candidate",
                        # A yearly value loads under the year value_date starts with: in the 31
                        # October Mexico tests, 55 t a day from a 2022-2024 plan was dated by the
                        # article, and a project's status by its target year.
                        "value_date": (
                            "The date the value is true for (a figure's own year, never a target year; "
                            "else the source's date): YYYY-MM-DD, YYYY-MM, or YYYY"
                        ),
                        "evidence_summary": "Short source-backed explanation",
                        "search_terms_used": "Search terms and languages used",
                        "quoted_evidence_short": "A short passage copied word for word from the source, in its own language",
                        "planned": "true only for a closing_year the source gives as planned or expected; otherwise false",
                        "not_municipal": "true only for an incoming waste amount the source says is mainly other waste; otherwise false",
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
Find source-backed data for this site, recorded as a waste disposal facility. {search_how}
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
{status_and_closure}Identity first:
- Return only sources about THIS facility. Use the names, approximate coordinates,
  municipality and admin names in the site context to check that each source describes it.{confirmation}
- A town can have waste sites we don't hold, such as a new landfill that replaced an old
  dump. A page about {not_this_one} is not about this facility unless it also describes this
  one: its name, place or history. From such a page use only what it says about this one. A
  generic phrase such as "municipal dump" or "basurero municipal" names no particular site.{same_point}
- If you cannot confirm you found this specific facility, return an empty sources list
  and explain why in search_notes. Do not return data for a different site.
- From a page listing many facilities, such as a registry, use only this facility's entry.

What each source says about the facility:
- Report each source you use once, with the facility as that source describes it:
  facility_name_in_source and facility_name_english, stated_address and
  stated_coordinates, stated_municipality and stated_state. They are used to check that
  the source is about this facility.
- stated_address is the site's own address, copied exactly as written: never the
  operator's office. A page often gives it in a contact box or footer, so look there too.
- stated_coordinates is the site's coordinates or grid reference, copied exactly as
  written. Give both fields where the source gives both.
- stated_municipality and stated_state: the town and the state or province the source puts
  the site in, in full ("Nuevo León", not "N.L."); for a town alone, the state it is in. A
  source in another state than admin1 is very likely about a different facility.
- Never work out an address or coordinates, never use the coordinates in the site context
  (rounded to about 1 km, for {coordinates_for} only), and leave a field blank if the source gives
  none.

Source requirements:
- Every source needs a clickable URL.
- quoted_evidence_short is copied word for word from the source, in the source's own language and
  with its own figures: never translated, reworded or summarised. It is checked against the page.
- Classify each source with source_type from Definitions.source_type. Do not invent
  new source types and do not combine two with a slash. There is no catch-all: if no
  listed type genuinely fits the source, put the finding in unverified_leads rather
  than guessing a type.
- Always give publication_date when the source shows one. An undated source is
  treated as less credible, so a date materially changes how the value is used.
- Do not assign a source tier. The pipeline assigns credibility from source_type
  and publisher.
- Report every source that supports a value, each with the values it gives. Agreement
  between independent sources is recorded, so list them all rather than only the best one.
- Never cite mapcarta.com or maptons.com: they copy OpenStreetMap or scrape business
  listings, and are discarded.
- Where a directory republishes government data, cite the government source instead.
  wastebook.co.uk copies the Environment Agency register: its coordinates are postcode
  centroids, and its "issued" date is a permit date, not a closure date. landfills.ca
  copies Statistics Canada's infrastructure database.
- Prefer the most recent source. Facilities open, close, change operator and add gas
  systems, so when sources disagree, or a newer source exists, use the newest one and
  give its publication_date and value_date. Mention an older conflicting source in
  evidence_summary rather than returning its value.

Value requirements:
- Use only the allowed categorical values in Definitions. If the true value is not
  among them, put it in unverified_leads instead of forcing a wrong category.
- Report the source's original unit in the unit field. The pipeline converts.
- value must hold only the value itself. For numeric attributes that means digits, never
  words, ranges in prose, or unit text.
- Gas capture attributes only apply if the facility actually has a gas collection system. If it
  does not, omit them rather than returning zeros.
- Return only actual figures, dates and features: what the facility is, has, did or does.
  Leave out planned, proposed, ordered or required, permitted or licensed, design and
  projected ones - a permitted capacity or annual limit is not waste received, and a design
  capacity is not waste in place. Never return a future year, except a planned closing_year
  (planned = true). If a source gives only such a figure, leave the attribute out and mention
  the figure in evidence_summary. (A gas project's status may still be Planned or Under
  Construction.)
- Label anything not directly stated by the source as value_basis = Inferred.
- Put uncertain or unsourced findings in unverified_leads, not in values.
- If nothing is found, return an empty sources list and explain in search_notes.

Output requirements:
- Return strict JSON only. No markdown, no prose outside JSON.

JSON output contract:
{json.dumps(output_contract, indent=2, ensure_ascii=False)}
"""
