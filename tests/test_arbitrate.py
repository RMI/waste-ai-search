from datetime import date

from waste_ai_search.arbitration import needs_review, resolve, routed_to_leads
from waste_ai_search.arbitrate import dedupe_sources, extract_evidence, normalize_url


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


GAZETTEER = {
    "source_title": "Site listing",
    "publisher": "Mapcarta",
    "source_type": "Map or coordinate directory",
    "publication_date": "",
    "url": "https://mapcarta.example/x",
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


def test_empty_baseline_filled_by_tier_1_auto_validates():
    by_attr, _rows, _sources, _warn = extract(site(), payload(attribute("facility_status", "active")))

    resolved = resolve(site(), "facility_status", by_attr["facility_status"])
    assert resolved["resolution"] == "Filled empty baseline"
    assert resolved["validation_status"] == "Auto-validated"


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


def test_equal_tier_disagreement_goes_to_review():
    s = site(facility_type="Dumpsite", contributing_data_sources="usa_ghgrp_2026")
    by_attr, _rows, _sources, _warn = extract(s, payload(attribute("facility_type", "sanitary landfill")))

    resolved = resolve(s, "facility_type", by_attr["facility_type"])
    assert resolved["resolution"] == "Conflict - needs review"
    assert "equal credibility" in resolved["resolution_rule"]
    assert resolved["validation_status"] == "Needs review"


def test_agreement_with_baseline_is_recorded_as_confirmation():
    s = site(facility_type="Sanitary Landfill", contributing_data_sources="osm_2022")
    by_attr, _rows, _sources, _warn = extract(s, payload(attribute("facility_type", "sanitary landfill")))

    resolved = resolve(s, "facility_type", by_attr["facility_type"])
    assert resolved["resolution"] == "Confirmed baseline"
    assert resolved["validation_status"] == "Auto-validated"


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


def test_normalize_url_strips_www_scheme_case_and_trailing_slash():
    assert normalize_url("HTTPS://WWW.Example.com/Path/") == normalize_url("https://example.com/path")


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


def test_gazetteer_coordinates_promote_and_confirm_identity():
    s = site(latitude="6.5", longitude="3.3")
    by_attr, _rows, _sources, _warn = extract(
        s,
        payload(
            attribute("found_latitude", "6.5", "decimal degrees", sources=[GAZETTEER]),
            attribute("found_longitude", "3.3", "decimal degrees", sources=[GAZETTEER]),
        ),
    )

    lat = resolve(s, "found_latitude", by_attr["found_latitude"])
    assert lat["resolution"] == "Confirmed baseline"
    assert lat["validation_status"] == "Auto-validated"


def test_gazetteer_coordinates_that_disagree_become_a_reviewable_conflict():
    s = site(latitude="6.5", longitude="3.3")
    by_attr, _rows, _sources, _warn = extract(
        s, payload(attribute("found_latitude", "7.9", "decimal degrees", sources=[GAZETTEER]))
    )

    lat = resolve(s, "found_latitude", by_attr["found_latitude"])
    assert lat["resolution"] == "Conflict - needs review"
    assert lat["resolved_value"] == "7.9"


def test_distance_is_computed_once_coordinates_resolve():
    from waste_ai_search.arbitrate import append_distance_row

    s = site(latitude="6.5", longitude="3.3")
    by_attr, _rows, _sources, _warn = extract(
        s,
        payload(
            attribute("found_latitude", "6.6", "decimal degrees", sources=[GAZETTEER]),
            attribute("found_longitude", "3.4", "decimal degrees", sources=[GAZETTEER]),
        ),
    )
    resolved_rows = [resolve(s, name, by_attr[name]) for name in ("found_latitude", "found_longitude")]
    append_distance_row(s, resolved_rows)

    distance = resolved_rows[-1]
    assert distance["attribute_name"] == "distance_to_original_coordinates_km"
    assert distance["resolution"] == "Calculated"
    assert distance["winning_source_tier"] == ""
    assert 14.0 < float(distance["resolved_value"]) < 16.0


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


def test_distant_coordinates_withdraw_auto_validation():
    """A credible source describing the wrong facility is what tier cannot catch."""
    from waste_ai_search.arbitrate import append_distance_row, flag_identity_mismatch

    s = site(latitude="6.5", longitude="3.3")
    by_attr, _rows, _sources, _warn = extract(
        s,
        payload(
            attribute("found_latitude", "14.5", "decimal degrees", sources=[GAZETTEER]),
            attribute("found_longitude", "121.0", "decimal degrees", sources=[GAZETTEER]),
            attribute("facility_status", "active", sources=[REGULATOR]),
        ),
    )
    resolved_rows = [
        resolve(s, name, by_attr.get(name, []))
        for name in ("found_latitude", "found_longitude", "facility_status")
    ]
    status = resolved_rows[-1]
    assert status["validation_status"] == "Auto-validated"  # tier alone would pass it

    km = append_distance_row(s, resolved_rows)
    assert flag_identity_mismatch(resolved_rows, km) is True

    assert status["validation_status"] == "Needs review"
    assert "Identity not confirmed" in status["researcher_notes"]
    assert resolved_rows[-1]["resolution"] == "Calculated"
    assert resolved_rows[-1]["validation_status"] == "Auto-validated"


def test_nearby_coordinates_keep_auto_validation():
    from waste_ai_search.arbitrate import append_distance_row, flag_identity_mismatch

    s = site(latitude="6.5", longitude="3.3")
    by_attr, _rows, _sources, _warn = extract(
        s,
        payload(
            attribute("found_latitude", "6.501", "decimal degrees", sources=[GAZETTEER]),
            attribute("found_longitude", "3.301", "decimal degrees", sources=[GAZETTEER]),
            attribute("facility_status", "active", sources=[REGULATOR]),
        ),
    )
    resolved_rows = [
        resolve(s, name, by_attr.get(name, []))
        for name in ("found_latitude", "found_longitude", "facility_status")
    ]
    km = append_distance_row(s, resolved_rows)
    assert flag_identity_mismatch(resolved_rows, km) is False
    assert resolved_rows[-1]["validation_status"] == "Auto-validated"


def test_sub_threshold_coordinate_difference_stops_going_to_review():
    """Four of five pilot coordinate conflicts were under 200 m apart (feedback 5)."""
    from waste_ai_search.arbitrate import settle_near_coordinates

    s = site(latitude="14.844807966", longitude="120.33153842")
    by_attr, _rows, _sources, _warn = extract(
        s,
        payload(
            attribute("found_latitude", "14.84566", "decimal degrees", sources=[GAZETTEER]),
            attribute("found_longitude", "120.32995", "decimal degrees", sources=[GAZETTEER]),
        ),
    )
    resolved_rows = [resolve(s, name, by_attr[name]) for name in ("found_latitude", "found_longitude")]
    assert all(needs_review(row) for row in resolved_rows)

    settled = settle_near_coordinates(s, resolved_rows)
    assert settled == 2
    assert not any(needs_review(row) for row in resolved_rows)
    assert "same place" in resolved_rows[0]["resolution_rule"]


def test_large_coordinate_difference_still_goes_to_review():
    from waste_ai_search.arbitrate import settle_near_coordinates

    s = site(latitude="15.140209253", longitude="120.54323141")
    by_attr, _rows, _sources, _warn = extract(
        s, payload(attribute("found_latitude", "16.89324", "decimal degrees", sources=[GAZETTEER]))
    )
    resolved_rows = [resolve(s, "found_latitude", by_attr["found_latitude"])]

    assert settle_near_coordinates(s, resolved_rows) == 0
    assert needs_review(resolved_rows[0])


def test_single_axis_coordinate_is_judged_without_its_counterpart():
    """Pilot site 2056 found a latitude but no longitude; it must still be judgeable."""
    from waste_ai_search.arbitrate import axis_offset_km

    s = site(latitude="6.6392530721", longitude="3.3021248334")
    km = axis_offset_km(s, "found_latitude", "6.638125")
    assert km is not None and km < 1.0


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


def test_auto_validated_rows_are_signed_by_the_agent():
    """Three blank columns on a row nobody will review is just a gap; record who decided it."""
    from datetime import date

    by_attr, _rows, _sources, _warn = extract(site(), payload(attribute("facility_status", "active")))
    row = resolve(site(), "facility_status", by_attr["facility_status"], reviewed_on=date(2026, 9, 15))

    assert row["validation_status"] == "Auto-validated"
    assert row["reviewer"] == "AI Agent"
    assert row["reviewed_date"] == "2026-09-15"
    assert row["researcher_notes"]  # carries the agent's own evidence summary


def test_rows_awaiting_a_human_are_left_unsigned():
    from datetime import date

    s = site(facility_status="Inactive", contributing_data_sources="usa_ghgrp_2026")
    by_attr, _rows, _sources, _warn = extract(s, payload(attribute("facility_status", "active")))
    row = resolve(s, "facility_status", by_attr["facility_status"], reviewed_on=date(2026, 9, 15))

    assert row["validation_status"] == "Needs review"
    assert row["reviewer"] == ""
    assert row["reviewed_date"] == ""
    assert row["researcher_notes"] == ""


def test_not_found_rows_are_signed_too():
    from datetime import date

    row = resolve(site(), "facility_status", [], reviewed_on=date(2026, 9, 15))
    assert row["resolution"] == "Not found"
    assert row["reviewer"] == "AI Agent"
    assert row["researcher_notes"] == "No source-backed evidence returned."


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
