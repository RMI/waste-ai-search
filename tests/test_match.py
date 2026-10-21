"""Each source is filed under the site of ours it is about (build step 5 of the source-matching plan).

Matching is by where the source places the facility, with its name as a check (match.py). A name
is a strong match at a score of 0.8 or more on at least two shared distinctive words, counting a
qualifier both names have. The fixtures are the plan's known cases, with hand-entered names and
locations per source, since batch 1's cached responses give a location for few sources: Brandon and
Guelph's two Eastview landfills, West Perth's page cited by many Mitchell-area searches, the SME's
Prairie Green, Moose Jaw and Merrick, the E-PRTR duplicates at Cowpen Bewley and Foxhall, Noosa's
Resource Recovery Centre, and namesakes near the site searched. Thunder Bay's copied point is end
to end, in test_pooled_review.py. Coordinates are the seed's.
"""
from __future__ import annotations

import pytest

from waste_ai_search.match import (
    LOCATED,
    MOVED_BY_NAME,
    NAME_NEARBY,
    NAMED_AMONG_SEVERAL,
    NEW_FACILITY,
    NO_LOCATION,
    NOT_A_FACILITY,
    OUTSIDE_RUN,
    TIE,
    Citation,
    Site,
    compare_names,
    datasets_found,
    judged_name,
    match_sources,
    run_selection,
)


# --- what a strong match is ---------------------------------------------------------------------
def test_two_shared_distinctive_words_at_0_8_are_strong():
    fit = compare_names("Thunder Bay Solid Waste & Recycling Facility", "City of Thunder Bay Solid Waste and Recycling Facility")
    assert (fit.score, fit.strong) == (1.0, True)
    assert compare_names("Moose Jaw Sanitary Landfill", "City of Moose Jaw (Landfill)").strong
    assert compare_names("Prairie Green Landfill", "Prairie Green IWMF").strong


def test_one_shared_word_is_never_strong():
    """Eastview Landfill is a Brandon and a Guelph site, 1,716 km apart."""
    fit = compare_names("Eastview Landfill", "Eastview Landfill")
    assert (fit.score, fit.strong, fit.fits) == (1.0, False, True)
    # Reads 0.8 alike as strings, but shares only "road".
    fit = compare_names("Hallam Road Landfill", "Ball Road Landfill")
    assert fit.score == pytest.approx(0.8) and not fit.strong


def test_a_qualifier_both_names_have_counts_as_a_shared_word():
    """In "North Bay" or "North Peace" the qualifier is part of the place's name."""
    fit = compare_names("Merrick Landfill - North Bay", "North Bay Landfill")
    assert (fit.score, fit.strong) == (1.0, True)
    assert compare_names("PRRD - North Peace Regional Landfill", "North Peace Regional Landfill").strong
    # Alone it is still one word, and qualifiers that conflict still score nothing.
    assert not compare_names("North Landfill", "North Landfill").strong
    assert compare_names("North Bay Landfill", "South Bay Landfill").score == 0.0


def test_a_generic_kind_of_site_shared_is_no_match():
    """187 pairs of our French sites matched each other strongly on "Installation de Stockage de
    Déchets Non Dangereux" alone, so a source naming one Hendaye's would have moved 97 km."""
    fit = compare_names("Installation de stockage de déchets non dangereux de Hendaye", "Installation de Stockage des Déchets Non Dangereux (ISDND)")
    assert not fit.fits
    assert compare_names("Installation de stockage de déchets non dangereux de Hendaye", "Hendaye landfill").score == 1.0


def test_a_name_with_no_distinctive_words_cant_be_judged():
    fit = compare_names("Landfill Site", "Brady Road Landfill")
    assert (fit.score, fit.strong, fit.fits) == (None, False, False)


@pytest.mark.parametrize("provenance", [
    {"attribute_sources": "found_facility_name@gpw_2021; found_latitude@gpw_2021"},
    # With no per-attribute provenance, a site drawn from Global Plastic Watch alone has its name.
    {"attribute_sources": "", "contributing_data_sources": "gpw_2021"},
])
def test_our_name_that_is_an_address_looked_up_from_our_point_cant_be_judged(provenance):
    """662 of batch 2's 856 sites are named so, and two in one district share every word."""
    site = Site.from_seed(seed("16433", "NH27, Jetpur Taluka, Rajkot, Gujarat, India", 21.75, 70.62, "IND", **provenance))
    assert (site.english, site.original, site.name) == ("", "", "NH27, Jetpur Taluka, Rajkot, Gujarat, India")
    named = Site.from_seed(seed("2880", "Nambour Landfill", -26.6, 152.98, "AUS", contributing_data_sources="osm_2022 + gpw_2021"))
    assert named.english == "Nambour Landfill"


@pytest.mark.parametrize("ours, judged", [
    ("OpenStreetMap Landfill", ""),
    ("domestic landfill", ""),
    ("Rubbish Tip", ""),
    ("", ""),
    ("Brady Road Resource Management Facility", "Brady Road Resource Management Facility"),
])
def test_our_placeholders_and_generic_labels_cant_be_judged(ours, judged):
    assert judged_name(ours) == judged


# --- fixtures --------------------------------------------------------------------------------------
def seed(site_id, name, lat, lon, iso3="CAN", **extra):
    return {"site_id": site_id, "site_name": name, "country_iso3": iso3, "latitude": str(lat), "longitude": str(lon), **extra}


def cite(searched, url, name="", point=None, index=1, basis="coordinates", **extra):
    """One search's citation of a source: the name it gives and the point its location was read as."""
    return Citation(
        searched_site_id=searched, source_index=index, url=url, name_in_source=name,
        stated_location=f"{point[0]}, {point[1]}" if point else "", basis=basis if point else "", point=point, **extra,
    )


def match(citations, sites, selection=None):
    return match_sources(citations, sites, {site["site_id"] for site in sites} if selection is None else selection)


# Brandon's Eastview Landfill, an unnamed site near it, and Guelph's Eastview, 1,716 km away.
BRANDON = seed("13338", "Eastview Landfill", 49.83866, -99.88844, area_square_meters="615489.82")
NEAR_BRANDON = seed("13339", "domestic landfill", 49.90, -99.50)
GUELPH = seed("18499", "Eastview Landfill", 43.5742, -80.2323)
BRANDON_PAGE = "https://www.brandon.ca/landfill"


# --- a source with a location ------------------------------------------------------------------
def test_a_located_source_is_filed_under_the_one_site_within_2_km_whether_or_not_it_was_searched():
    matching = match([cite("13339", BRANDON_PAGE, "Eastview Landfill", (49.8400, -99.8900))], [BRANDON, NEAR_BRANDON, GUELPH])
    filing = matching.filings[("13339", 1)]
    assert (filing.site_id, filing.outcome, filing.in_run) == ("13338", LOCATED, True)
    assert filing.match.startswith("Located: by its coordinates, the source places it 0.0 km from this site")
    assert "km from the site searched (13339)" in filing.match
    # The SME may re-file it under the site searched instead.
    assert filing.choices == ["13338", "13339"]
    assert matching.counts[LOCATED] == 1


def test_distance_is_to_a_sites_edge_by_its_area_capped_at_2_km():
    # 10 km2 is a circle of radius 1.78 km: a source 3.5 km from the point is 1.7 km from the edge.
    big = seed("1", "Big Landfill", 50.0, -100.0, area_square_meters="10000000")
    assert match([cite("1", "https://a.example", point=(50.0315, -100.0))], [big]).filings[("1", 1)].outcome == LOCATED
    # A radius beyond 2 km is not trusted: 100 km2 still reaches only 2 km past the point.
    huge = seed("1", "Huge Landfill", 50.0, -100.0, area_square_meters="100000000")
    assert match([cite("1", "https://a.example", point=(50.0405, -100.0))], [huge]).filings[("1", 1)].outcome == NEW_FACILITY
    # A site with no area is its point.
    bare = seed("1", "Bare Landfill", 50.0, -100.0)
    assert match([cite("1", "https://a.example", point=(50.0315, -100.0))], [bare]).filings[("1", 1)].outcome == NEW_FACILITY


@pytest.mark.parametrize("basis", ["hint", "none"])
def test_a_hint_or_an_unusable_location_counts_as_none(basis):
    """A hint, a location copied from the prompt and a doubtful one are read as no location."""
    matching = match([cite("13339", BRANDON_PAGE, "Eastview Landfill", (49.84, -99.89), basis=basis)], [BRANDON, NEAR_BRANDON])
    assert matching.filings[("13339", 1)].outcome == NO_LOCATION


def test_a_page_cited_by_several_searches_is_matched_once():
    """West Perth's landfill page was cited by searches of 'domestic landfill' sites hundreds of km
    apart. Where it states a location, every citation of it is filed under the site there."""
    mitchell = seed("12101", "domestic landfill", 43.4771, -81.2096)
    others = [seed("12200", "domestic landfill", 44.5, -80.0), seed("12300", "domestic landfill", 46.0, -81.0)]
    page = "https://www.westperth.com/news/landfill-hours"
    located = [cite(sid, page, "West Perth Landfill", (43.4775, -81.2100)) for sid in ("12101", "12200", "12300")]
    matching = match(located, [mitchell, *others])
    assert {key: filing.site_id for key, filing in matching.filings.items()} == {
        ("12101", 1): "12101", ("12200", 1): "12101", ("12300", 1): "12101",
    }
    assert matching.counts[LOCATED] == 1  # one source, however many searches cited it
    assert "from the site searched (12200)" in matching.filings[("12200", 1)].match
    assert "site searched" not in matching.filings[("12101", 1)].match


def test_the_name_picks_among_several_sites_within_2_km():
    ours = [
        seed("1", "domestic landfill", 50.000, -100.000),
        seed("2", "Brady Road Resource Management Facility", 50.010, -100.000),
        seed("3", "Gimli Landfill", 50.005, -100.010),
    ]
    matching = match([cite("1", "https://permit.example", "Brady Road Landfill", (50.004, -100.003))], ours)
    filing = matching.filings[("1", 1)]
    assert (filing.site_id, filing.outcome) == ("2", NAMED_AMONG_SEVERAL)
    assert "its name matches only this one" in filing.match
    assert set(filing.choices) == {"1", "2", "3"}
    assert matching.duplicates == []


def test_a_tie_is_filed_under_the_site_searched_and_every_pair_is_listed():
    ours = [seed(sid, "domestic landfill", 50.0 + offset, -100.0) for sid, offset in (("1", 0.0), ("2", 0.005), ("3", 0.010))]
    matching = match([cite("2", "https://registry.example/x", point=(50.004, -100.0))], ours)
    filing = matching.filings[("2", 1)]
    assert (filing.site_id, filing.outcome) == ("2", TIE)
    assert "its name picks none of them; filed under the site searched" in filing.match
    assert {(row["site_id_a"], row["site_id_b"]) for row in matching.duplicates} == {("1", "2"), ("1", "3"), ("2", "3")}
    assert all("picks neither" in row["reason"] for row in matching.duplicates)


def test_a_tie_not_among_the_sites_searched_goes_to_the_nearest():
    ours = [seed("1", "domestic landfill", 50.0, -100.0), seed("2", "domestic landfill", 50.01, -100.0),
            seed("9", "domestic landfill", 51.0, -100.0)]
    filing = match([cite("9", "https://registry.example/x", point=(50.008, -100.0))], ours).filings[("9", 1)]
    assert (filing.site_id, filing.outcome) == ("2", TIE)
    assert "filed under the nearest" in filing.match


# --- E-PRTR duplicates ---------------------------------------------------------------------------
COWPEN = [seed("18572", "Cowpen Bewley Landfill Site", 54.6136, -1.24064, "GBR"),
          seed("19041", "Cowpen Bewley Landfill Site", 54.613634, -1.240635, "GBR")]
FOXHALL = [seed("18578", "Viridor Waste Management Limited, Foxhall Landfill Site   Ea/Epr/Bw2943Ig/V002", 52.0482, 1.26388, "GBR"),
           seed("19056", "FOXHALL LANDFILL SITE", 52.048239, 1.2638765, "GBR")]


def test_one_page_cited_by_both_searches_of_a_duplicate_pair_lists_the_pair():
    """Cowpen Bewley and Foxhall are each two E-PRTR records of one landfill, and both searches cited
    the same register page, with no location."""
    page = "https://environment.data.gov.uk/public-register/industrial-installations/registration/EPR-RP3531DV?__pageState=result-industrial-installations"
    foxhall = "https://environment.data.gov.uk/public-register/industrial-installations/registration/EPR-BW2943IG?__pageState=result-all"
    citations = [cite("18572", page, "Cowpen Bewley Landfill Site"), cite("19041", page, "Cowpen Bewley Landfill Site"),
                 cite("18578", foxhall, "Foxhall Landfill Site"), cite("19056", foxhall, "Foxhall Landfill Site")]
    matching = match(citations, [*COWPEN, *FOXHALL])
    assert [(row["site_id_a"], row["site_id_b"], row["distance_km"], row["reason"]) for row in matching.duplicates] == [
        ("18572", "19041", "0.00", "Both searches cited the same page"),
        ("18578", "19056", "0.00", "Both searches cited the same page"),
    ]
    assert matching.duplicates[0]["source_urls"] == page
    # Each stays with its own search: neither moves to the other by name.
    assert {key: filing.site_id for key, filing in matching.filings.items()} == {
        ("18572", 1): "18572", ("19041", 1): "19041", ("18578", 1): "18578", ("19056", 1): "19056",
    }


def test_a_located_source_at_a_duplicate_pair_is_a_tie_its_name_cant_settle():
    page = "https://environment.data.gov.uk/public-register/EPR-RP3531DV"
    matching = match([cite("19041", page, "Cowpen Bewley Landfill Site", (54.6137, -1.2405))], COWPEN)
    filing = matching.filings[("19041", 1)]
    assert (filing.site_id, filing.outcome) == ("19041", TIE)
    assert "its name fits 2 of them" in filing.match
    [pair] = matching.duplicates
    assert (pair["site_id_a"], pair["site_id_b"]) == ("18572", "19041")
    assert "its name fits both" in pair["reason"]


# --- a strongly matching name and a location just beyond 2 km ---------------------------------------
def test_a_strong_name_within_5_km_keeps_the_source_as_a_coordinate_correction():
    ours = [seed("1", "Seaton Meadows Landfill", 54.60, -1.20, "GBR")]
    filing = match([cite("1", "https://permit.example", "Seaton Meadows Landfil", (54.63, -1.20))], ours).filings[("1", 1)]
    assert (filing.site_id, filing.outcome) == ("1", NAME_NEARBY)
    assert "3.3 km from this site" in filing.match and "coordinate correction" in filing.match
    # Beyond 5 km it is another landfill.
    filing = match([cite("1", "https://permit.example", "Seaton Meadows Landfil", (54.66, -1.20))], ours).filings[("1", 1)]
    assert (filing.site_id, filing.outcome) == ("", NEW_FACILITY)


def test_an_inexact_location_of_ours_stretches_the_name_rescue_to_25_km():
    ours = [seed("9", "Lixão de Porto Nacional", -10.18, -48.33, "BRA", is_location_exact="FALSE")]
    filing = match([cite("9", "https://licenca.example.br/1", "Lixão Municipal de Porto Nacional", (-10.35, -48.33))], ours).filings[("9", 1)]
    assert (filing.site_id, filing.outcome) == ("9", NAME_NEARBY)


def test_one_shared_word_never_stretches_a_match():
    filing = match([cite("13338", BRANDON_PAGE, "Eastview Landfill", (49.86, -99.86))], [BRANDON]).filings[("13338", 1)]
    assert filing.outcome == NEW_FACILITY


# --- a point only on its street ------------------------------------------------------------------
LICENCE = "https://www.gov.mb.ca/sd/eal/registries/5597brandoneastview/licence3149.pdf"
# Where Nominatim put the licence's "3610 Victoria Avenue East, Brandon, Manitoba": somewhere on
# Victoria Avenue East, since it has no house numbers for the road, 2.4 km from Brandon's Eastview.
VICTORIA_AVENUE = (49.842103, -99.927913)


def test_a_street_level_point_more_than_2_km_from_every_site_counts_as_no_location():
    licence = cite("13338", LICENCE, "Eastview Landfill", VICTORIA_AVENUE, basis="street")
    filing = match([licence], [BRANDON]).filings[("13338", 1)]
    assert (filing.site_id, filing.outcome) == ("13338", NO_LOCATION)
    assert filing.match.startswith(
        "No location: the source's location can't be used (its address is placed only on its street, 2.4 km "
        "from the site searched: too rough to match beyond 2 km), so it stays with the site searched."
    )
    # A point on the building, or stated coordinates, that far out is still a new facility.
    for basis in ("rooftop", "coordinates"):
        placed = cite("13338", LICENCE, "Eastview Landfill", VICTORIA_AVENUE, basis=basis)
        assert match([placed], [BRANDON]).filings[("13338", 1)].outcome == NEW_FACILITY


def test_a_street_level_point_within_2_km_still_locates_the_source():
    on_street = cite("13339", BRANDON_PAGE, "Eastview Landfill", (49.8426, -99.8644), basis="street")
    filing = match([on_street], [BRANDON, NEAR_BRANDON]).filings[("13339", 1)]
    assert (filing.site_id, filing.outcome) == ("13338", LOCATED)
    assert filing.match.startswith("Located: by its address, placed on its street, the source places it 1.3 km")


# --- new facilities ----------------------------------------------------------------------------
def test_located_sources_far_from_every_site_are_grouped_into_candidates():
    ours = [seed("1", "domestic landfill", 50.0, -100.0)]
    citations = [
        cite("1", "https://a.example", "Mapleward Landfill", (50.100, -100.0), index=1),
        cite("1", "https://b.example", "Mapleward Road Site", (50.110, -100.0), index=2),  # 1.1 km from the first
        cite("1", "https://c.example", "Other Dump", (50.300, -100.0), index=3),
    ]
    matching = match(citations, ours)
    assert {filing.outcome for filing in matching.filings.values()} == {NEW_FACILITY}
    assert all(filing.site_id == "" and not filing.in_run for filing in matching.filings.values())
    first, second = sorted(matching.new_facilities, key=lambda row: row["source_urls"])
    assert first["source_urls"] == "https://a.example; https://b.example"
    assert first["facility_names_in_source"] == "Mapleward Landfill; Mapleward Road Site"
    assert (first["latitude"], first["longitude"], first["location_basis"]) == ("50.105000", "-100.000000", "coordinates")
    assert (first["nearest_site_id"], first["nearest_site_km"], first["searched_site_ids"]) == ("1", "11.7", "1")
    assert first["candidate_id"].startswith("WAINEW-") and first["found_by"] == "arbitration"
    assert second["source_urls"] == "https://c.example"
    assert matching.counts[NEW_FACILITY] == 3


def test_a_source_saying_not_a_waste_facility_is_never_a_candidate():
    ours = [seed("1", "domestic landfill", 50.0, -100.0)]
    quarry = cite("1", "https://quarry.example", "Smith Quarry", (50.2, -100.0), not_a_waste_facility=True)
    matching = match([quarry], ours)
    filing = matching.filings[("1", 1)]
    assert (filing.site_id, filing.outcome) == ("", NOT_A_FACILITY)
    assert filing.match.endswith("says it is not a waste facility, so it is not listed.")
    assert matching.new_facilities == []
    assert (matching.counts[NEW_FACILITY], matching.counts[NOT_A_FACILITY]) == (0, 1)


def test_a_registry_page_naming_several_facilities_is_several_sources():
    ours = [seed("1", "Alpha Brook Landfill", 50.0, -100.0), seed("2", "Beta Creek Landfill", 50.5, -100.0)]
    registry = "https://registry.example/landfills.csv"
    matching = match([
        cite("1", registry, "Alpha Brook Landfill", (50.001, -100.0), index=1),
        cite("1", registry, "Beta Creek Landfill", (50.501, -100.0), index=2),
    ], ours)
    assert (matching.filings[("1", 1)].site_id, matching.filings[("1", 2)].site_id) == ("1", "2")


def test_a_name_a_page_gives_to_two_places_is_two_sources():
    """A council's list of closed landfills can call every row "Closed Landfill". Two searches citing
    two of its rows, 10 km apart, each go to their own site, not both to the first one's."""
    ours = [seed("101", "Smith Road Landfill", -41.0, 175.0, "NZL"), seed("102", "Jones Road Landfill", -41.09, 175.0, "NZL")]
    page = "https://council.example.nz/closed-landfills"
    matching = match([
        cite("101", page, "Closed Landfill", (-41.0, 175.0)),
        cite("102", page, "Closed Landfill", (-41.09, 175.0)),
    ], ours)
    assert {key: filing.site_id for key, filing in matching.filings.items()} == {("101", 1): "101", ("102", 1): "102"}
    assert matching.counts[LOCATED] == 2


def test_a_citation_with_no_location_joins_its_entry_only_where_the_name_is_one_place():
    ours = [seed("101", "Smith Road Landfill", -41.0, 175.0, "NZL"), seed("102", "Jones Road Landfill", -41.09, 175.0, "NZL"),
            seed("103", "Brown Road Landfill", -41.3, 175.0, "NZL")]
    page = "https://council.example.nz/closed-landfills"
    # The page's own name for a row, matching none of ours, so only joining the located row can file
    # a citation of it elsewhere than its own search.
    # A name that says which facility it is, placed once: a citation that gave no location joins it.
    one = match([cite("101", page, "Hilltop Landfill", (-41.0, 175.0)), cite("103", page, "Hilltop Landfill")], ours)
    assert (one.filings[("103", 1)].site_id, one.filings[("103", 1)].outcome) == ("101", LOCATED)
    # A generic name may stand for rows no search located, so the citation stays with its own site.
    generic = match([cite("101", page, "Closed Landfill", (-41.0, 175.0)), cite("103", page, "Closed Landfill")], ours)
    assert (generic.filings[("103", 1)].site_id, generic.filings[("103", 1)].outcome) == ("103", NO_LOCATION)
    # A name placed in two spots leaves no telling which the third search meant.
    two = match([
        cite("101", page, "Hilltop Landfill", (-41.0, 175.0)),
        cite("102", page, "Hilltop Landfill", (-41.09, 175.0)),
        cite("103", page, "Hilltop Landfill"),
    ], ours)
    assert (two.filings[("103", 1)].site_id, two.filings[("103", 1)].outcome) == ("103", NO_LOCATION)


# --- a source with no location ---------------------------------------------------------------------
def test_eastview_one_shared_word_stays_with_the_site_searched_and_names_the_namesake():
    """Brandon's page went under 14 sites in batch 1. Without a location, one shared word can't move
    it, but the flag names Brandon's Eastview, 29 km away, and not Guelph's, 1,716 km away."""
    matching = match([cite("13339", BRANDON_PAGE, "Eastview Landfill")], [BRANDON, NEAR_BRANDON, GUELPH])
    filing = matching.filings[("13339", 1)]
    assert (filing.site_id, filing.outcome) == ("13339", NO_LOCATION)
    assert filing.match.startswith("No location: the source gives no location, so it stays with the site searched.")
    assert "Its name also fits Eastview Landfill (13338, 29 km), within 200 km." in filing.match
    assert "18499" not in filing.match
    assert filing.choices == ["13339", "13338"]


@pytest.mark.parametrize("ours, searched_at, name", [
    # The SME's examples: no location given, and the name scored 1.0 against the right site.
    (seed("18529", "Prairie Green IWMF", 50.0047, -97.1307), (50.30, -97.00), "Prairie Green Landfill"),
    # Batch 1's 13354, 133 km away: "Integrated Waste Management Facility" is what IWMF stands for.
    (seed("18529", "Prairie Green IWMF", 50.0047, -97.1307), (49.25037, -98.57193),
     "Prairie Green Integrated Waste Management Facility (Landfill)"),
    (seed("13449", "City of Moose Jaw (Landfill)", 50.39872, -105.48078), (50.90, -106.30), "Moose Jaw Sanitary Landfill"),
])
def test_a_name_strongly_matching_one_other_site_within_200_km_moves_the_source(ours, searched_at, name):
    searched = seed("100", "domestic landfill", *searched_at)
    matching = match([cite("100", "https://city.example/landfill", name)], [ours, searched])
    filing = matching.filings[("100", 1)]
    assert (filing.site_id, filing.outcome) == (ours["site_id"], MOVED_BY_NAME)
    assert "and not the site searched" in filing.match and "from the site searched (100)" in filing.match
    assert filing.choices == [ours["site_id"], "100"]


# Batch 1's searches of unnamed sites near North Bay that cited Ontario's landfill list for the
# Merrick Landfill, and our North Bay Landfill, which it is: the City of North Bay's landfill.
NORTH_BAY = seed("14312", "North Bay Landfill", 46.51482, -79.49169, area_square_meters="418700.56")
MERRICK_SEARCHES = [
    seed("14272", "domestic landfill", 45.49261, -79.44276), seed("14283", "domestic landfill", 46.23946, -80.44142),
    seed("14292", "Municipal Dump", 46.40463, -80.31042), seed("14300", "domestic landfill", 45.77757, -79.39066),
]


def test_merrick_its_towns_name_moves_the_source_to_the_landfill_named_after_the_town():
    """The SME's Merrick example. "Merrick Landfill - North Bay" scores 1.0 against our North Bay
    Landfill, 64-114 km from the sites searched, and shares two words with it, "North" among them."""
    page = "https://data.ontario.ca/dataset/ontario-landfills"
    matching = match([cite(site["site_id"], page, "Merrick Landfill - North Bay") for site in MERRICK_SEARCHES], [NORTH_BAY, *MERRICK_SEARCHES])
    assert {key[0]: (filing.site_id, filing.outcome) for key, filing in matching.filings.items()} == {
        site["site_id"]: ("14312", MOVED_BY_NAME) for site in MERRICK_SEARCHES
    }
    assert "114 km from the site searched (14272)" in matching.filings[("14272", 1)].match
    assert matching.counts[MOVED_BY_NAME] == 4


def test_merrick_alone_shares_no_word_with_north_bay_and_stays_with_the_site_searched():
    searched = seed("14281", "domestic landfill", 46.11473, -80.40934)
    filing = match([cite("14281", "https://northbay.ca/landfill-operations", "Merrick Landfill")], [NORTH_BAY, searched]).filings[("14281", 1)]
    assert (filing.site_id, filing.outcome) == ("14281", NO_LOCATION)
    assert "North Bay" not in filing.match
    # With a location it is matched by where it is.
    filing = match([cite("14281", "https://northbay.ca/landfill-operations", "Merrick Landfill", (46.5150, -79.4920))],
                   [NORTH_BAY, searched]).filings[("14281", 1)]
    assert (filing.site_id, filing.outcome) == ("14312", LOCATED)


def test_noosa_a_generic_kind_of_site_never_moves_a_source_to_another_of_that_kind():
    """Batch 1: Noosa's page for its Resource Recovery Centre, the site searched, gave no location.
    "Resource Recovery Centre" is generic, so the name doesn't fit Nambour's Landfill & Resource
    Recovery Centre, 20 km away. Our name for the site searched is Global Plastic Watch's address
    for its point, which can't be judged either."""
    noosa = seed("2881", "Emu Mountain Road, Doonan, Noosa Shire, Queensland, 4566, Australia", -26.43825, 153.02924, "AUS",
                 attribute_sources="found_facility_name@gpw_2021; found_latitude@gpw_2021")
    nambour = seed("2880", "Nambour Landfill & Resource Recovery Centre", -26.61253, 152.98284, "AUS",
                   area_square_meters="638846.2", attribute_sources="found_facility_name@osm_2022")
    page = "https://www.noosa.qld.gov.au/Environment-and-Waste/Waste-and-Recycling/Waste-and-resource-recovery-centres"
    filing = match([cite("2881", page, "Noosa Resource Recovery Centre")], [noosa, nambour]).filings[("2881", 1)]
    assert (filing.site_id, filing.outcome) == ("2881", NO_LOCATION)
    assert "Nambour" not in filing.match


def test_two_namesakes_within_200_km_leave_the_source_with_the_site_searched():
    namesakes = [seed("2", "Green Lane Landfill", 43.0, -81.0), seed("3", "Green Lane Landfill", 43.5, -80.5)]
    searched = seed("1", "domestic landfill", 43.2, -80.8)
    filing = match([cite("1", "https://news.example/green-lane", "Green Lane Landfill")], [searched, *namesakes]).filings[("1", 1)]
    assert (filing.site_id, filing.outcome) == ("1", NO_LOCATION)
    assert "Its name also fits Green Lane Landfill (2," in filing.match and "Green Lane Landfill (3," in filing.match
    assert filing.choices == ["1", "2", "3"]


def test_a_name_that_also_fits_the_site_searched_is_not_moved():
    searched = seed("1", "Green Lane Landfill", 43.2, -80.8)
    other = seed("2", "Green Lane Landfill", 43.0, -81.0)
    filing = match([cite("1", "https://news.example/green-lane", "Green Lane Landfill")], [searched, other]).filings[("1", 1)]
    assert (filing.site_id, filing.outcome) == ("1", NO_LOCATION)
    assert "Green Lane Landfill (2," in filing.match  # a tie, named


def test_the_flag_says_the_page_was_cited_for_other_searches():
    sites = [seed(sid, "domestic landfill", 43.0 + n, -81.0) for n, sid in enumerate(("12101", "12200", "12300"))]
    page = "https://www.westperth.com/news/landfill-hours"
    matching = match([cite(sid, page, "West Perth Landfill") for sid in ("12101", "12200", "12300")], sites)
    assert {filing.site_id for filing in matching.filings.values()} == {"12101", "12200", "12300"}
    assert "The same page was cited for 2 other search(es): 12200, 12300." in matching.filings[("12101", 1)].match
    assert matching.counts[NO_LOCATION] == 3  # once per search, never pooled
    # The SME may re-file it under any of them.
    assert matching.filings[("12200", 1)].choices == ["12200", "12101", "12300"]


def test_prairie_green_beyond_200_km_stays_but_the_sme_is_offered_prairie_greens_own_search():
    """Batch 1: Prairie Green's page was cited by the searches of 13490, 311 km away, and of Prairie
    Green itself. Too far to move by name, it stays with 13490, and corrected_site_id offers 18529.
    Brandon's page at Guelph's Eastview, 1,716 km away, is offered back to Brandon the same way."""
    prairie_green = seed("18529", "Prairie Green IWMF", 50.0047, -97.1307)
    searched = seed("13490", "domestic landfill", 50.94687, -101.26409)
    page = "https://www.wasteconnections.ca/prairie-green"
    matching = match([cite("13490", page, "Prairie Green Integrated Waste Management Facility"), cite("18529", page, "Prairie Green Landfill")],
                     [prairie_green, searched])
    filing = matching.filings[("13490", 1)]
    assert (filing.site_id, filing.outcome, filing.choices) == ("13490", NO_LOCATION, ["13490", "18529"])
    filing = match([cite("18499", BRANDON_PAGE, "Eastview Landfill"), cite("13338", BRANDON_PAGE, "Eastview Landfill")],
                   [BRANDON, GUELPH]).filings[("18499", 1)]
    assert (filing.site_id, filing.choices) == ("18499", ["18499", "13338"])


def test_an_unusable_location_says_why():
    filing = match([cite("13339", BRANDON_PAGE, "Eastview Landfill", (49.9, -99.5), basis="none", location_reason="copied from the prompt")],
                   [NEAR_BRANDON]).filings[("13339", 1)]
    assert filing.match.startswith("No location: the source's location can't be used (copied from the prompt)")


def test_names_are_compared_in_both_languages():
    """Our English name is a generic label, so only our original name can be judged."""
    ours = seed("1", "Sanitary Landfill", 19.40, -99.00, "MEX", original_site_name="Relleno Sanitario Bordo Poniente")
    searched = seed("2", "domestic landfill", 19.50, -99.20, "MEX")
    source = cite("2", "https://cdmx.example/bordo", "Relleno Sanitario Bordo Poniente", name_english="")
    assert match([source], [ours, searched]).filings[("2", 1)].site_id == "1"


# --- the run's selection -------------------------------------------------------------------------
def test_the_selection_is_the_countries_searched_and_only_the_searched_sites_of_an_excluded_country():
    sites = [
        seed("1", "a", 0, 0, "CAN"), seed("2", "b", 0, 0, "CAN"), seed("3", "c", 0, 0, "GBR"),
        seed("4", "d", 0, 0, "USA"), seed("5", "e", 0, 0, "USA"), seed("6", "f", 0, 0, "BRA"),
    ]
    assert run_selection(sites, {"1"}) == {"1", "2"}
    assert run_selection(sites, {"1", "4"}) == {"1", "2", "4"}


def test_a_match_outside_the_selection_is_listed_not_filed():
    """Another batch's country: ingestion stops on a facility with values from two runs."""
    ours = [seed("1", "domestic landfill", 49.2, -97.2), seed("2", "Pembina Valley Landfill", 48.99, -97.21, "USA")]
    matching = match([cite("1", "https://nd.example/pembina", "Pembina Valley Landfill", (48.991, -97.21))], ours, selection={"1"})
    filing = matching.filings[("1", 1)]
    assert (filing.site_id, filing.outcome, filing.in_run) == ("2", LOCATED, False)
    assert filing.choices == ["1"]
    assert matching.counts[OUTSIDE_RUN] == 1
    [row] = matching.outside
    assert (row["matched_site_id"], row["country_iso3"], row["searched_site_id"], row["source_url"]) == (
        "2", "USA", "1", "https://nd.example/pembina",
    )


# --- pages that look like datasets ---------------------------------------------------------------
REGISTRY = "https://council.example.nz/closed-landfills.csv"
COUNCIL = [seed("101", "Smith Road Landfill", -41.0, 175.0, "NZL"), seed("102", "Jones Road Landfill", -41.09, 175.0, "NZL"),
           seed("103", "Brown Road Landfill", -41.3, 175.0, "NZL")]


def test_a_page_three_searches_found_is_listed_as_a_dataset():
    cited = [cite(sid, REGISTRY, name, point) for sid, name, point in (
        ("101", "Smith Road Landfill", (-41.0, 175.0)), ("102", "Jones Road Landfill", (-41.09, 175.0)),
        ("103", "Brown Road Landfill", (-41.3, 175.0)),
    )]
    sources = [{"url_normalized": REGISTRY, "url": REGISTRY, "source_title": "Closed landfills", "publisher": "Council",
                "source_type": "Regulator", "source_tier": "Tier 1", "publication_date": "2024-01-01"}]
    [row] = datasets_found(cited, match(cited, COUNCIL), sources)
    assert (row["source_url"], row["publisher"], row["searches"], row["facilities_named"], row["facilities_placed"]) == (
        REGISTRY, "Council", 3, 3, 3,
    )
    assert row["placed_site_ids"] == "101; 102; 103"
    assert row["why"] == "found by 3 searches; names 3 facilities; places 3 facilities"


def test_one_search_naming_two_of_a_pages_facilities_lists_it_and_its_new_entries_count():
    cited = [
        cite("101", REGISTRY, "Smith Road Landfill", (-41.0, 175.0), index=1),
        # A row none of our sites is: a new-landfill entry of the page.
        cite("101", REGISTRY, "Hilltop Landfill", (-41.6, 175.0), index=2),
    ]
    [row] = datasets_found(cited, match(cited, COUNCIL), [])
    assert (row["searches"], row["facilities_named"], row["facilities_placed"], row["placed_site_ids"]) == (1, 2, 2, "101")
    assert row["facility_names_in_source"] == "Smith Road Landfill; Hilltop Landfill"


def test_a_page_staying_with_searches_only_for_want_of_a_location_places_nothing():
    """Brandon's landfill page stayed with twelve unnamed searches: that says nothing about what it lists."""
    cited = [cite(sid, BRANDON_PAGE, "Eastview Landfill") for sid in ("13339", "13340")]
    sites = [BRANDON, NEAR_BRANDON, seed("13340", "domestic landfill", 49.95, -99.6)]
    assert datasets_found(cited, match(cited, sites), []) == []
