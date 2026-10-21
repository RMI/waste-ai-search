from datetime import date

from waste_ai_search.arbitration import needs_review, resolve, routed_to_leads
from waste_ai_search.arbitrate import dedupe_sources, extract_evidence, normalize_url
from waste_ai_search.schema import NOTHING_TO_REVIEW


REGULATOR = {
    "source_title": "Annual waste statistics",
    "publisher": "Waste Authority",
    "source_tier": "Tier 4",  # agent's guess, must be ignored
    "publication_date": "2022-12-31",
    "url": "https://example.org/waste-statistics",
    "language": "English",
    "source_type": "Regulator",
}

NEWS = {
    "source_title": "Landfill reopens",
    "publisher": "Daily News",
    "source_tier": "Tier 2",  # agent over-rates news
    "publication_date": "2024-02-02",
    "url": "https://news.example.com/story/",
    "source_type": "News report",
}


# A map site that is kept (Wikimapia). Mapcarta and Maptons are dropped by website before
# arbitration sees them; see test_map_sites.py.
GAZETTEER = {
    "source_title": "Site listing",
    "publisher": "Wikimapia",
    "source_type": "Map or coordinate directory",
    "publication_date": "",
    "url": "https://wikimapia.example/x",
}


def site(**overrides):
    base = {
        "site_id": "1",
        "internal_facility_id": "1",
        "site_name": "Test Landfill",
        "country_iso3": "NGA",
        "latitude": "6.5",
        "longitude": "3.3",
        "contributing_data_sources": "osm_2022",
        "reference_year": "2022",
    }
    base.update(overrides)
    return base


def payload(*attributes):
    return {"site_id": "1", "attributes": list(attributes)}


def attribute(name, value, unit="", sources=None, **extra):
    row = {
        "attribute_name": name,
        "value": value,
        "unit": unit,
        "value_basis": "Direct",
        "confidence_score": "High",
        "value_date": "2022",
        "evidence_summary": "summary",
        "sources": sources if sources is not None else [REGULATOR],
    }
    row.update(extra)
    return row


def extract(s, p):
    return extract_evidence(s, p, "run", "v", date(2026, 1, 1))


def test_units_convert_to_the_spec_standard():
    by_attr, rows, _sources, _warn = extract(
        site(), payload(attribute("annual_incoming_waste_metric_tonnes", "365000", "kg/day"))
    )

    row = rows[0]
    assert row["claimed_value"] == "365000"
    assert row["claimed_unit"] == "kg/day"
    assert row["normalized_value"] == "133225"  # 365000 kg/day -> t/year
    assert row["normalized_unit"] == "metric tonnes/year"
    assert "kg to metric tonnes" in row["unit_conversion_note"]
    assert by_attr["annual_incoming_waste_metric_tonnes"][0]["value"] == "133225"


def test_agent_tier_is_ignored_and_reassigned_from_source_type():
    _by_attr, rows, _sources, _warn = extract(
        site(), payload(attribute("facility_status", "closed", sources=[REGULATOR, NEWS]))
    )

    regulator_row, news_row = rows[0], rows[1]
    assert regulator_row["agent_proposed_tier"] == "Tier 4"
    assert regulator_row["source_tier"] == "Tier 1"
    assert news_row["agent_proposed_tier"] == "Tier 2"
    assert news_row["source_tier"] == "Tier 3"


def test_categorical_values_map_onto_the_database_enums():
    _by_attr, rows, _sources, _warn = extract(
        site(),
        payload(
            attribute("facility_status", "closed"),
            attribute("facility_type", "landfill"),
            attribute("has_landfill_gas_collection", "Unknown"),
        ),
    )

    by_name = {row["attribute_name"]: row for row in rows}
    assert by_name["facility_status"]["mapped_value"] == "Inactive"
    assert by_name["facility_type"]["mapped_value"] == "Sanitary Landfill"
    assert by_name["has_landfill_gas_collection"]["mapped_value"] == ""


def test_undated_regulator_is_capped_at_tier_3():
    undated = dict(REGULATOR, publication_date="")
    _by_attr, rows, _sources, _warn = extract(site(), payload(attribute("facility_status", "active", sources=[undated])))

    assert rows[0]["source_tier"] == "Tier 3"
    assert "capped" in rows[0]["tier_rule_applied"]


def test_tier_4_fills_an_empty_baseline_but_needs_review():
    """A sourced value beats no value; tier decides whether a human must look (feedback 1)."""
    wiki = dict(REGULATOR, source_type="Wiki or aggregator")
    by_attr, rows, _sources, _warn = extract(site(), payload(attribute("facility_status", "active", sources=[wiki])))

    assert rows[0]["source_tier"] == "Tier 4"
    resolved = resolve(site(), "facility_status", by_attr["facility_status"])
    assert resolved["resolution"] == "Filled empty baseline"
    assert resolved["resolved_value"] == "Active"
    assert resolved["validation_status"] == "Needs review"


def test_tier_4_against_a_stronger_baseline_routes_to_leads():
    wiki = dict(REGULATOR, source_type="Wiki or aggregator")
    s = site(facility_status="Inactive", contributing_data_sources="usa_ghgrp_2026")
    by_attr, _rows, _sources, _warn = extract(s, payload(attribute("facility_status", "active", sources=[wiki])))

    resolved = resolve(s, "facility_status", by_attr["facility_status"])
    assert resolved["resolution"] == "Conflict - lower credibility"
    assert resolved["validation_status"] == "Routed to leads"
    assert not needs_review(resolved)


def test_empty_baseline_filled_by_tier_1_goes_to_review():
    """Reversed on purpose: Tier 1-2 fills once auto-validated (Q30). Nothing is accepted automatically."""
    by_attr, _rows, _sources, _warn = extract(site(), payload(attribute("facility_status", "active")))

    resolved = resolve(site(), "facility_status", by_attr["facility_status"])
    assert resolved["resolution"] == "Filled empty baseline"
    assert resolved["validation_status"] == "Needs review"


def test_tier_3_fill_goes_to_review():
    by_attr, _rows, _sources, _warn = extract(
        site(), payload(attribute("facility_status", "active", sources=[NEWS]))
    )

    resolved = resolve(site(), "facility_status", by_attr["facility_status"])
    assert resolved["resolution"] == "Filled empty baseline"
    assert resolved["validation_status"] == "Needs review"


def test_tier_1_source_overrides_a_tier_4_baseline():
    s = site(facility_type="Dumpsite", contributing_data_sources="osm_2022")
    by_attr, _rows, _sources, _warn = extract(s, payload(attribute("facility_type", "sanitary landfill")))

    resolved = resolve(s, "facility_type", by_attr["facility_type"])
    assert resolved["resolution"] == "Overrides baseline"
    assert resolved["credibility_margin"] == 3
    assert resolved["validation_status"] == "Needs review"


def test_equal_tier_disagreement_goes_to_review():
    s = site(facility_type="Dumpsite", contributing_data_sources="usa_ghgrp_2026")
    by_attr, _rows, _sources, _warn = extract(s, payload(attribute("facility_type", "sanitary landfill")))

    resolved = resolve(s, "facility_type", by_attr["facility_type"])
    assert resolved["resolution"] == "Conflict - needs review"
    assert "equal credibility" in resolved["resolution_rule"]
    assert resolved["validation_status"] == "Needs review"


def test_agreement_with_baseline_is_recorded_as_confirmation():
    """A confirmation changes nothing, so it is neither queued nor loaded."""
    s = site(facility_type="Sanitary Landfill", contributing_data_sources="osm_2022")
    by_attr, _rows, _sources, _warn = extract(s, payload(attribute("facility_type", "sanitary landfill")))

    resolved = resolve(s, "facility_type", by_attr["facility_type"])
    assert resolved["resolution"] == "Confirmed baseline"
    assert resolved["validation_status"] == NOTHING_TO_REVIEW
    assert not needs_review(resolved)


def test_corroboration_is_counted():
    trade = dict(NEWS, url="https://trade.example.com/a", source_type="Trade press")
    by_attr, _rows, _sources, _warn = extract(
        site(), payload(attribute("facility_status", "active", sources=[NEWS, trade]))
    )

    resolved = resolve(site(), "facility_status", by_attr["facility_status"])
    assert resolved["agreeing_source_count"] == 2
    assert resolved["dissenting_source_count"] == 0


def test_best_tier_wins_over_listing_order():
    by_attr, _rows, _sources, _warn = extract(
        site(),
        payload(
            attribute("facility_status", "active", sources=[NEWS]),
            attribute("facility_status", "inactive", sources=[REGULATOR]),
        ),
    )

    resolved = resolve(site(), "facility_status", by_attr["facility_status"])
    assert resolved["resolved_value"] == "Inactive"
    assert resolved["winning_source_tier"] == "Tier 1"
    assert resolved["dissenting_source_count"] == 1


def test_sources_dedupe_by_normalized_url():
    _by_attr, _rows, sources, _warn = extract(
        site(),
        payload(
            attribute("facility_status", "active"),
            attribute("facility_type", "dumpsite", sources=[dict(REGULATOR, url="https://WWW.example.org/waste-statistics/")]),
        ),
    )

    deduped = dedupe_sources(sources)
    assert len(deduped) == 1
    assert deduped[0]["times_cited"] == 2


def test_normalize_url_strips_www_scheme_and_host_case_and_trailing_slash():
    assert normalize_url("HTTPS://WWW.Example.com/Path/") == normalize_url("https://example.com/Path")


def test_normalize_url_keeps_the_path_case():
    """Reversed on purpose: the path was once lower-cased, but servers may treat case as meaning."""
    assert normalize_url("https://example.com/Path") != normalize_url("https://example.com/path")


def test_unsupported_attribute_is_dropped_with_a_warning():
    """`mcf` is a real spec column but deliberately not searched, so it must not slip through."""
    _by_attr, rows, _sources, warnings = extract(site(), payload(attribute("mcf", "1.0")))

    assert rows == []
    assert any("mcf" in warning for warning in warnings)


def test_non_numeric_value_for_a_numeric_attribute_is_not_promotable():
    """The agent can put unit text in the value field; it must never reach a numeric column.

    Caught here by the conversion guard rather than the numeric guard - either is fine, so long
    as it does not promote.
    """
    by_attr, rows, _sources, warnings = extract(
        site(),
        payload(attribute("annual_incoming_waste_metric_tonnes", "ambiguous tons", "tons/year")),
    )

    assert rows[0]["promotion_eligible"] == "FALSE"
    assert "Could not parse numeric value" in rows[0]["exclusion_reason"]
    assert "annual_incoming_waste_metric_tonnes" not in by_attr
    assert any("Could not parse numeric value" in warning for warning in warnings)
    resolved = resolve(site(), "annual_incoming_waste_metric_tonnes", by_attr.get("annual_incoming_waste_metric_tonnes", []))
    assert resolved["resolution"] == "Not found"


def test_numeric_attribute_with_a_real_number_still_promotes():
    by_attr, rows, _sources, _warn = extract(
        site(), payload(attribute("waste_in_place_metric_tonnes", "1200000", "metric tonnes"))
    )

    assert rows[0]["promotion_eligible"] == "TRUE"
    assert by_attr["waste_in_place_metric_tonnes"][0]["value"] == "1200000"


def located_by(stated, source=GAZETTEER, kind="coordinates"):
    """A source-first response with one source stating where the facility is, and no values."""
    return {"site_id": "1", "sources": [{**source, "stated_location": stated, "stated_location_kind": kind, "values": []}]}


def test_a_gazetteer_location_is_tier_3_and_a_reviewable_conflict():
    """Reversed on purpose for build step 4: the location is read from what the source states, as one
    found_location, not as two coordinates the agent returned. A map directory keeps its Tier 3."""
    s = site(latitude="6.5", longitude="3.3")
    by_attr, rows, _sources, _warn = extract(s, located_by("6.512345, 3.312345"))

    (item,) = by_attr["found_location"]
    assert (item["value"], item["tier"]) == ("6.512345, 3.312345", 3)
    assert rows[0]["location_basis"] == "coordinates"
    location = resolve(s, "found_location", by_attr["found_location"])
    assert location["baseline_value"] == "6.5, 3.3"
    assert location["resolution"] == "Conflict - needs review"
    assert location["resolved_value"] == "6.512345, 3.312345"


def test_a_location_equal_to_ours_at_comparison_precision_confirms_it():
    # Six significant digits: a longitude past 100 degrees compares to about 100 m.
    s = site(latitude="14.84481", longitude="120.331538")
    by_attr, _rows, _sources, _warn = extract(s, located_by("14.844812, 120.331912"))
    location = resolve(s, "found_location", by_attr["found_location"])
    assert (location["resolution"], location["validation_status"]) == ("Confirmed baseline", NOTHING_TO_REVIEW)


def test_a_location_equal_to_ours_is_a_location_and_confirms_it():
    """Reversed on purpose for build step 5: a point within 10 m of ours no longer counts as copied,
    since the prompt carries only our rounded point. One equal to the prompt's still does."""
    s = site(latitude="6.5123456", longitude="3.3123456")
    by_attr, rows, _sources, _warn = extract(s, located_by("6.5123456, 3.3123456"))
    assert rows[0]["location_basis"] == "coordinates"
    location = resolve(s, "found_location", by_attr["found_location"])
    assert location["resolution"] == "Confirmed baseline"
    _by, rows, _sources, _warn = extract(s, located_by("6.510, 3.310"))
    assert (rows[0]["location_basis"], rows[0]["exclusion_reason"]) == ("none", "No location: copied from the prompt.")


def test_gazetteer_metadata_cannot_beat_an_existing_value():
    """A gazetteer is Tier 4 for tonnage, so it loses to any real baseline."""
    s = site(annual_incoming_waste_metric_tonnes="120000", contributing_data_sources="usa_ghgrp_2026")
    by_attr, rows, _sources, _warn = extract(
        s, payload(attribute("annual_incoming_waste_metric_tonnes", "500000", "metric tonnes", sources=[GAZETTEER]))
    )

    assert rows[0]["source_tier"] == "Tier 4"
    resolved = resolve(s, "annual_incoming_waste_metric_tonnes", by_attr["annual_incoming_waste_metric_tonnes"])
    assert resolved["resolution"] == "Conflict - lower credibility"
    assert resolved["validation_status"] == "Routed to leads"


def test_distant_locations_flag_every_value_for_the_sme():
    """A credible source describing the wrong facility is what tier cannot catch. With nothing
    accepted automatically, the gate only adds its note; build step 5 replaces it."""
    from waste_ai_search.arbitrate import farthest_location_km, flag_identity_mismatch

    s = site(latitude="6.5", longitude="3.3")
    response = located_by("14.5000, 121.0000")
    status_value = {key: value for key, value in attribute("facility_status", "active").items() if key != "sources"}
    response["sources"].append({**REGULATOR, "values": [status_value]})
    by_attr, rows, _sources, _warn = extract(s, response)
    status = resolve(s, "facility_status", by_attr["facility_status"])
    assert status["validation_status"] == "Needs review"

    km = farthest_location_km(s, rows)
    assert flag_identity_mismatch([status], km) is True
    assert status["validation_status"] == "Needs review"
    assert status["researcher_notes"].startswith("Identity not confirmed: a source's stated location is")
    assert "Check the source is about this facility." in status["researcher_notes"]


def test_nearby_locations_add_no_identity_note():
    from waste_ai_search.arbitrate import farthest_location_km, flag_identity_mismatch

    s = site(latitude="6.5", longitude="3.3")
    _by, rows, _sources, _warn = extract(s, located_by("6.5010, 3.3010"))
    resolved_rows = [{"attribute_name": "operator", "resolution": "Filled empty baseline", "researcher_notes": ""}]
    assert flag_identity_mismatch(resolved_rows, farthest_location_km(s, rows)) is False
    assert resolved_rows[0]["researcher_notes"] == ""


def test_the_farthest_usable_location_is_measured_and_a_hint_is_not():
    from waste_ai_search.arbitrate import farthest_location_km

    s = site(latitude="6.5", longitude="3.3")
    response = located_by("6.5010, 3.3010")
    response["sources"] += located_by("7.6, 3.3")["sources"]  # one decimal: a hint, 122 km away
    _by, rows, _sources, _warn = extract(s, response)
    assert farthest_location_km(s, rows) < 1.0
    response["sources"] += located_by("7.6000, 3.3000")["sources"]
    _by, rows, _sources, _warn = extract(s, response)
    assert 120 < farthest_location_km(s, rows) < 125


def test_a_location_200_m_from_ours_goes_to_review():
    """Reversed on purpose. Four of five pilot coordinate conflicts were under 200 m apart, and the
    1 km rule (feedback 5) settled them as the same place. Every value that differs from ours is
    now reviewed; one equal to ours to comparison precision is still a confirmation."""
    s = site(latitude="14.844807966", longitude="120.33153842")
    by_attr, _rows, _sources, _warn = extract(s, located_by("14.84566, 120.32995"))
    assert needs_review(resolve(s, "found_location", by_attr["found_location"]))


def test_lead_routed_rows_are_not_in_the_review_queue():
    wiki = dict(REGULATOR, source_type="Wiki or aggregator")
    s = site(facility_type="Sanitary Landfill", contributing_data_sources="usa_ghgrp_2026")
    by_attr, _rows, _sources, _warn = extract(s, payload(attribute("facility_type", "dumpsite", sources=[wiki])))

    resolved = resolve(s, "facility_type", by_attr["facility_type"])
    assert routed_to_leads(resolved)
    assert not needs_review(resolved)


def test_ambiguous_tons_is_never_promoted_into_a_metric_tonnes_column():
    """Seen live on site 2114: "30000 tons" was filed as metric tonnes. Short tons are 10% off."""
    by_attr, rows, _sources, warnings = extract(
        site(), payload(attribute("waste_in_place_metric_tonnes", "30000", "tons"))
    )

    assert rows[0]["promotion_eligible"] == "FALSE"
    assert "ambiguous" in rows[0]["exclusion_reason"].lower()
    assert any("ambiguous" in w.lower() for w in warnings)
    resolved = resolve(site(), "waste_in_place_metric_tonnes", by_attr.get("waste_in_place_metric_tonnes", []))
    assert resolved["resolution"] == "Not found"


def test_explicit_tonne_variants_still_promote():
    for unit in ("metric tonnes", "tonnes", "short tons", "kg"):
        by_attr, rows, _sources, _warn = extract(
            site(), payload(attribute("waste_in_place_metric_tonnes", "30000", unit))
        )
        assert rows[0]["promotion_eligible"] == "TRUE", unit
        assert by_attr["waste_in_place_metric_tonnes"], unit


def test_unconvertible_depth_unit_is_not_promoted():
    _by_attr, rows, _sources, _warn = extract(site(), payload(attribute("waste_depth", "40", "fathoms")))
    assert rows[0]["promotion_eligible"] == "FALSE"


def test_a_finding_is_never_both_promoted_and_listed_as_a_lead():
    """Feedback 1 lets low-tier evidence win; feedback 3 says don't also list it as a lead.

    Mirrors the promoted-attribute filter in `arbitrate.run_arbitration`.
    """
    wiki = dict(REGULATOR, source_type="Wiki or aggregator")
    by_attr, site_evidence, _sources, _warn = extract(
        site(), payload(attribute("facility_status", "active", sources=[wiki]))
    )
    site_resolved = [resolve(site(), "facility_status", by_attr["facility_status"])]

    # The evidence was tier-ineligible but still filled the empty baseline.
    assert site_evidence[0]["promotion_eligible"] == "FALSE"
    assert site_resolved[0]["resolved_value"] == "Active"

    promoted = {r["attribute_name"] for r in site_resolved if r["resolved_value"]}
    leads = [r for r in site_evidence if r["exclusion_reason"] and r["attribute_name"] not in promoted]
    assert leads == []


def test_no_row_is_signed_off_by_the_agent():
    """Reversed on purpose: rows the pipeline once accepted itself carried reviewer = AI Agent.
    Nothing is accepted automatically now, so the reviewer columns are left for the SME."""
    s = site(facility_type="Sanitary Landfill", contributing_data_sources="osm_2022")
    by_attr, _rows, _sources, _warn = extract(
        s, payload(attribute("facility_status", "active"), attribute("facility_type", "sanitary landfill"))
    )
    rows = [
        resolve(s, "facility_status", by_attr["facility_status"]),  # a Tier 1 fill
        resolve(s, "facility_type", by_attr["facility_type"]),  # a confirmation
        resolve(s, "opening_year", []),  # not found
    ]

    assert [row["validation_status"] for row in rows] == ["Needs review", NOTHING_TO_REVIEW, NOTHING_TO_REVIEW]
    for row in rows:
        assert (row["reviewer"], row["reviewed_date"]) == ("", "")
    assert rows[0]["researcher_notes"] == ""
    assert rows[2]["resolution"] == "Not found"
    assert not needs_review(rows[2])


def test_dropped_columns_are_gone_from_resolved_output():
    from waste_ai_search.schema import RESOLVED_HEADERS

    for gone in (
        "baseline_carried_from_earlier_year",
        "baseline_staleness_note",
        "agreeing_source_count",
        "dissenting_source_count",
        "credibility_margin",
    ):
        assert gone not in RESOLVED_HEADERS


def test_per_attribute_provenance_beats_the_facility_composite():
    """The composite takes the BEST of every dataset a facility draws on, over-crediting a weak
    individual value. The ledger says which dataset supplied *this* attribute."""
    s = site(
        facility_status="Inactive",
        contributing_data_sources="usa_ghgrp_2026 + osm_2022",
        attribute_sources="facility_status@osm_2022",
    )
    by_attr, _rows, _sources, _warn = extract(s, payload(attribute("facility_status", "active")))
    row = resolve(s, "facility_status", by_attr["facility_status"])

    assert row["baseline_source"] == "osm_2022"
    assert row["baseline_tier"] == "Tier 4"
    # A Tier 1 regulator can now correct an OSM value instead of deadlocking on the composite.
    assert row["resolution"] == "Overrides baseline"


def test_without_the_ledger_it_falls_back_to_the_composite_and_says_so():
    s = site(facility_status="Inactive", contributing_data_sources="usa_ghgrp_2026 + osm_2022")
    by_attr, _rows, _sources, _warn = extract(s, payload(attribute("facility_status", "active")))
    row = resolve(s, "facility_status", by_attr["facility_status"])

    assert row["baseline_source"] == ""
    assert row["baseline_tier"] == "Tier 1"
    assert row["resolution"] == "Conflict - needs review"
    assert "fell back to the facility composite" in row["baseline_staleness_note"]


def test_a_previous_ai_pass_outranks_the_ledger():
    """An AI-filled value must be tiered by the source that filled it, not by the seed dataset."""
    s = site(
        facility_status="Active",
        contributing_data_sources="usa_ghgrp_2026",
        attribute_sources="facility_status@usa_ghgrp_2026",
        ai_filled_fields="facility_status@Tier 3",
    )
    by_attr, _rows, _sources, _warn = extract(s, payload(attribute("facility_status", "inactive")))
    row = resolve(s, "facility_status", by_attr["facility_status"])

    assert row["baseline_source"] == "ai_search_2026"
    assert row["baseline_tier"] == "Tier 3"
    assert row["resolution"] == "Overrides baseline"


def test_baseline_source_is_published_in_resolved_and_the_review_queue():
    from waste_ai_search.schema import RESOLVED_HEADERS, REVIEW_QUEUE_HEADERS

    assert "baseline_source" in RESOLVED_HEADERS
    assert "baseline_source" in REVIEW_QUEUE_HEADERS


def test_an_empty_baseline_reports_no_source_and_no_tier():
    """A tier on a field with no value is a claim about something that does not exist."""
    s = site(
        facility_status="",
        contributing_data_sources="usa_ghgrp_2026",
        attribute_sources="found_facility_name@usa_ghgrp_2026",
    )
    by_attr, _rows, _sources, _warn = extract(s, payload(attribute("facility_status", "active")))
    row = resolve(s, "facility_status", by_attr["facility_status"])

    assert row["resolution"] == "Filled empty baseline"
    assert row["baseline_value"] == ""
    assert row["baseline_source"] == ""
    assert row["baseline_tier"] == ""


def test_a_non_empty_baseline_still_reports_both():
    s = site(
        facility_status="Inactive",
        contributing_data_sources="usa_ghgrp_2026",
        attribute_sources="facility_status@usa_ghgrp_2026",
    )
    by_attr, _rows, _sources, _warn = extract(s, payload(attribute("facility_status", "active")))
    row = resolve(s, "facility_status", by_attr["facility_status"])

    assert row["baseline_source"] == "usa_ghgrp_2026"
    assert row["baseline_tier"] == "Tier 1"


def test_area_is_converted_to_square_metres_and_promoted():
    by_attr, rows, _sources, _warn = extract(site(), payload(attribute("area_square_meters", "25", "acres")))

    assert rows[0]["normalized_value"] == "101171.4"
    assert rows[0]["normalized_unit"] == "square meters"
    assert rows[0]["promotion_eligible"] == "TRUE"
    row = resolve(site(), "area_square_meters", by_attr["area_square_meters"])
    assert row["resolution"] == "Filled empty baseline"


def test_negative_area_is_not_promotable():
    _by_attr, rows, _sources, warnings = extract(site(), payload(attribute("area_square_meters", "-5", "square meters")))
    assert rows[0]["promotion_eligible"] == "FALSE"
    assert any("outside the spec range" in w for w in warnings)


def test_has_biocover_is_a_tristate_boolean():
    by_attr, rows, _sources, _warn = extract(
        site(),
        payload(
            attribute("has_biocover", "Yes"),
            attribute("has_cover", "No"),
        ),
    )
    by_name = {r["attribute_name"]: r for r in rows}
    assert by_name["has_biocover"]["mapped_value"] == "TRUE"
    assert by_name["has_cover"]["mapped_value"] == "FALSE"
    assert resolve(site(), "has_biocover", by_attr["has_biocover"])["resolved_value"] == "TRUE"


def test_unknown_biocover_becomes_null_not_false():
    _by_attr, rows, _sources, _warn = extract(site(), payload(attribute("has_biocover", "Unknown")))
    assert rows[0]["mapped_value"] == ""


def test_biocover_contradicting_cover_types_is_flagged():
    """The two are searched independently but describe the same physical cover."""
    from waste_ai_search.arbitrate import cover_consistency_warnings

    contradiction = [
        {"attribute_name": "has_biocover", "resolved_value": "TRUE"},
        {"attribute_name": "cover_types", "resolved_value": "clay cover; sand cover"},
    ]
    assert cover_consistency_warnings(contradiction)

    other_way = [
        {"attribute_name": "has_biocover", "resolved_value": "FALSE"},
        {"attribute_name": "cover_types", "resolved_value": "organic cover"},
    ]
    assert cover_consistency_warnings(other_way)


def test_consistent_or_partial_cover_answers_are_not_flagged():
    from waste_ai_search.arbitrate import cover_consistency_warnings

    for rows in (
        [{"attribute_name": "has_biocover", "resolved_value": "TRUE"},
         {"attribute_name": "cover_types", "resolved_value": "clay cover; organic cover"}],
        [{"attribute_name": "has_biocover", "resolved_value": "FALSE"},
         {"attribute_name": "cover_types", "resolved_value": "clay cover"}],
        [{"attribute_name": "has_biocover", "resolved_value": "TRUE"}],
        [{"attribute_name": "cover_types", "resolved_value": "organic cover"}],
    ):
        assert cover_consistency_warnings(rows) == []


def test_evidence_is_arbitrated_even_if_the_seed_would_not_ask_for_it():
    """A follow-up pass answers attributes a stale baseline would no longer request."""
    from waste_ai_search.prompt_builder import requested_attributes

    # gas collection already recorded, so the seed would not re-ask for it...
    s = site(has_landfill_gas_collection="TRUE", facility_status="Active")
    assert "has_landfill_gas_collection" not in requested_attributes(s)

    # ...but if a response carries it, the evidence must still be resolved, not dropped.
    by_attr, _rows, _sources, _warn = extract(
        s, payload(attribute("has_landfill_gas_collection", "Yes"))
    )
    assert "has_landfill_gas_collection" in by_attr
    row = resolve(s, "has_landfill_gas_collection", by_attr["has_landfill_gas_collection"])
    assert row["resolved_value"] == "TRUE"
    assert row["resolution"] == "Confirmed baseline"
