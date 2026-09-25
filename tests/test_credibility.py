from waste_ai_search.credibility import (
    TIER_1,
    TIER_2,
    TIER_3,
    TIER_4,
    TIER_5,
    apply_staleness_penalty,
    assign_source_tier,
    backfilled_years,
    baseline_tier,
    best_source,
    is_promotable,
    may_auto_validate,
    may_override_baseline,
    normalize_source_type,
)


def source(source_type, publication_date="2024-01-01", url="https://e.org/a", **extra):
    return {"source_type": source_type, "publication_date": publication_date, "url": url, **extra}


def test_tier_comes_from_source_type():
    assert assign_source_tier(source("Regulator"))[0] == TIER_1
    assert assign_source_tier(source("Operator disclosure"))[0] == TIER_1
    assert assign_source_tier(source("Peer-reviewed article"))[0] == TIER_2
    assert assign_source_tier(source("News report"))[0] == TIER_3
    assert assign_source_tier(source("Wiki or aggregator"))[0] == TIER_4


def test_every_news_source_gets_the_same_tier():
    """The pilot put news at Tier 2, 3 and 4. One class must mean one tier."""
    tiers = {assign_source_tier(source("News report", publication_date=d))[0] for d in ("2024-01-01", "2019-06-06")}
    assert tiers == {TIER_3}


def test_agent_proposed_tier_is_ignored():
    high = assign_source_tier(source("Wiki or aggregator", source_tier="Tier 1"))
    low = assign_source_tier(source("Regulator", source_tier="Tier 4"))
    assert high[0] == TIER_4
    assert low[0] == TIER_1


def test_undated_source_is_capped_at_tier_3():
    tier, rule = assign_source_tier(source("Regulator", publication_date=""))
    assert tier == TIER_3
    assert "capped" in rule


def test_undated_tier_4_is_not_promoted_by_the_cap():
    assert assign_source_tier(source("Wiki or aggregator", publication_date=""))[0] == TIER_4


def test_unclassifiable_source_type_is_tier_5():
    tier, _rule = assign_source_tier(source("Other"))
    assert tier == TIER_5
    assert not is_promotable(tier)


def test_missing_source_type_is_tier_5_not_defaulted_to_tier_3():
    tier, rule = assign_source_tier(source(""))
    assert tier == TIER_5
    assert "unclassifiable" in rule


def test_tier_5_sits_below_tier_4_on_one_ordered_scale():
    """Tier 5 replaced an out-of-band sentinel, so margins stay meaningful."""
    assert TIER_5 == 5
    assert TIER_4 < TIER_5
    assert not is_promotable(TIER_5)
    assert not may_override_baseline(TIER_5)
    assert not may_auto_validate(TIER_5)


def test_legacy_free_text_source_types_are_recognized():
    assert normalize_source_type("Operator website") == "operator disclosure"
    assert normalize_source_type("Regulator / Court decision") == "court decision"
    assert normalize_source_type("EIA") == "environmental impact assessment"
    assert normalize_source_type("Journal article") == "peer-reviewed article"
    assert normalize_source_type("Operator website / company profile") == "company directory listing"


def test_company_directory_is_not_treated_as_operator_disclosure():
    assert assign_source_tier(source("Company directory listing"))[0] == TIER_4


def test_baseline_tier_takes_the_best_contributing_dataset():
    assert baseline_tier("usa_ghgrp_2026 + osm_2022")[0] == TIER_1
    assert baseline_tier("osm_2022")[0] == TIER_4
    assert baseline_tier("gpw_2021 + osm_2022")[0] == TIER_4
    assert baseline_tier("waste_atlas_landfills_2013")[0] == TIER_3
    assert baseline_tier("mexico_inegi_2016")[0] == TIER_1
    assert baseline_tier("canada_ghgrp_2021")[0] == TIER_1
    assert baseline_tier("sinir_2024")[0] == TIER_1
    assert baseline_tier("manual_entry")[0] == TIER_3


def test_unknown_baseline_dataset_is_tier_5():
    tier, note = baseline_tier("some_new_source_2030")
    assert tier == TIER_5
    assert "no tiered dataset" in note


def test_gating_thresholds():
    assert is_promotable(TIER_3) and not is_promotable(TIER_4)
    assert may_override_baseline(TIER_2) and not may_override_baseline(TIER_3)
    assert may_auto_validate(TIER_2) and not may_auto_validate(TIER_3)


def test_staleness_penalty_only_past_the_three_year_gap():
    # 1- and 2-year gaps are reporting lag, not staleness.
    assert apply_staleness_penalty(TIER_1, "waste_in_place_metric_tonnes", 2016, 2015)[0] == TIER_1
    assert apply_staleness_penalty(TIER_1, "waste_in_place_metric_tonnes", 2016, 2014)[0] == TIER_1
    assert apply_staleness_penalty(TIER_1, "waste_in_place_metric_tonnes", 2016, 2013)[0] == TIER_2


def test_staleness_penalty_is_floored_and_scoped():
    assert apply_staleness_penalty(TIER_3, "waste_in_place_metric_tonnes", 2020, 2000)[0] == TIER_3
    assert apply_staleness_penalty(TIER_1, "facility_type", 2020, 1990)[0] == TIER_1


def test_backfilled_years_parses_seed_provenance():
    parsed = backfilled_years("facility_status@2013; waste_in_place_metric_tonnes@2022")
    assert parsed == {"facility_status": 2013, "waste_in_place_metric_tonnes": 2022}


def test_best_source_prefers_credibility_over_listing_order():
    sources = [source("News report", url="https://n.example/a"), source("Regulator", url="https://r.gov/b")]
    chosen, tier, _rule = best_source(sources)
    assert chosen["url"] == "https://r.gov/b"
    assert tier == TIER_1


def test_best_source_skips_entries_without_a_url():
    chosen, tier, rule = best_source([source("Regulator", url="")])
    assert chosen == {}
    assert tier == TIER_5
    assert "clickable URL" in rule


def test_gazetteer_is_credible_for_coordinates_but_not_for_metadata():
    """A coordinate directory is purpose-built for location and useless for operations."""
    gazetteer = source("Map or coordinate directory", publication_date="")

    assert assign_source_tier(gazetteer, "found_latitude")[0] == TIER_3
    assert assign_source_tier(gazetteer, "found_longitude")[0] == TIER_3
    assert assign_source_tier(gazetteer, "annual_incoming_waste_metric_tonnes")[0] == TIER_4
    assert assign_source_tier(gazetteer, "facility_status")[0] == TIER_4
    assert assign_source_tier(gazetteer)[0] == TIER_4


def test_wiki_is_not_a_gazetteer_and_stays_tier_4_for_coordinates():
    assert assign_source_tier(source("Wiki or aggregator"), "found_latitude")[0] == TIER_4


def test_coordinate_exception_cannot_override_a_baseline():
    """Tier 3 promotes and can confirm, but never silently overrides."""
    tier = assign_source_tier(source("Map or coordinate directory"), "found_latitude")[0]
    assert is_promotable(tier)
    assert not may_override_baseline(tier)
    assert not may_auto_validate(tier)


def test_there_is_no_catch_all_source_type():
    """Removing 'Other' means a source must fit a real type or fall to Tier 5."""
    from waste_ai_search.credibility import SOURCE_TYPES

    assert "Other" not in SOURCE_TYPES
    assert all(assign_source_tier(source(t))[0] <= TIER_4 for t in SOURCE_TYPES)


def test_unrecognized_type_falls_to_tier_5_and_says_why():
    tier, rule = assign_source_tier(source("Other"))
    assert tier == TIER_5
    assert "not a recognized type" in rule

    tier, rule = assign_source_tier(source(""))
    assert tier == TIER_5
    assert "no source_type given" in rule


def test_per_attribute_source_is_tiered_from_one_dataset_token():
    from waste_ai_search.credibility import attribute_baseline_tier, attribute_sources

    parsed = attribute_sources("facility_status@osm_2022; found_site_name@lmop_2024")
    assert parsed == {"facility_status": "osm_2022", "found_site_name": "lmop_2024"}
    assert attribute_baseline_tier("osm_2022")[0] == TIER_4
    assert attribute_baseline_tier("lmop_2024")[0] == TIER_1


def test_untiered_or_missing_per_attribute_source_is_tier_5():
    from waste_ai_search.credibility import attribute_baseline_tier

    assert attribute_baseline_tier("")[0] == TIER_5
    tier, note = attribute_baseline_tier("some_new_dataset_2030")
    assert tier == TIER_5
    assert "carries no tier" in note
