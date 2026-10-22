"""The lists arbitration writes for ingestion beside found_values.csv (lists.py).

Since the matcher went (step 2 of the approve-sources plan), possible_duplicates.csv holds only our
sites cited under the same page and facility name, and datasets_found.csv counts the names and
places a page gives itself, and the searches citing it, instead of the sites matching put it under.
The matcher's own tests went with it (test_match.py).
"""
from __future__ import annotations

from waste_ai_search.lists import Site, SiteIndex, candidate_id, datasets_found, plain, possible_duplicates


def cite(site_id, url, name="", point=None, basis="coordinates", index=1):
    """One search's citation of a source, as extract_evidence's source entries give it."""
    return {
        "site_id": site_id, "source_index": index, "url": url, "facility_name_in_source": name,
        "facility_name_english": "", "location_basis": basis if point else "",
        "located_latitude": str(point[0]) if point else "", "located_longitude": str(point[1]) if point else "",
    }


def seed(site_id, name, lat, lon, **extra):
    return {"site_id": site_id, "site_name": name, "latitude": str(lat), "longitude": str(lon), **extra}


# --- pages that look like datasets ---------------------------------------------------------------
REGISTRY = "https://council.example.nz/closed-landfills.csv"
REGISTRY_SOURCE = {
    "url_normalized": REGISTRY, "url": REGISTRY, "source_title": "Closed landfills", "publisher": "Council",
    "source_type": "Regulator", "source_tier": "Tier 1", "publication_date": "2024-01-01",
}


def test_a_page_three_searches_found_is_listed_as_a_dataset():
    cited = [cite(sid, REGISTRY, name, point) for sid, name, point in (
        ("101", "Smith Road Landfill", (-41.0, 175.0)), ("102", "Jones Road Landfill", (-41.09, 175.0)),
        ("103", "Brown Road Landfill", (-41.3, 175.0)),
    )]
    [row] = datasets_found(cited, [REGISTRY_SOURCE])
    assert (row["source_url"], row["publisher"], row["searches"], row["facilities_named"], row["facilities_placed"]) == (
        REGISTRY, "Council", 3, 3, 3,
    )
    assert row["searched_site_ids"] == "101; 102; 103"
    assert row["why"] == "found by 3 searches; names 3 facilities; places 3 facilities"


def test_one_search_naming_two_of_a_pages_facilities_lists_it():
    cited = [
        cite("101", REGISTRY, "Smith Road Landfill", (-41.0, 175.0), index=1),
        cite("101", REGISTRY, "Hilltop Landfill", (-41.6, 175.0), index=2),
    ]
    [row] = datasets_found(cited, [])
    assert (row["searches"], row["facilities_named"], row["facilities_placed"]) == (1, 2, 2)
    assert row["facility_names_in_source"] == "Smith Road Landfill; Hilltop Landfill"


def test_points_a_page_gives_within_2_km_are_one_facility_and_a_street_counts():
    """Two searches placing one landfill a kilometre apart place one facility, not two. An address
    placed on its street places it; a hint does not."""
    cited = [
        cite("101", REGISTRY, "Smith Road Landfill", (-41.0, 175.0)),
        cite("102", REGISTRY, "Smith Road Landfill", (-41.009, 175.0), basis="street"),
        cite("103", REGISTRY, "Smith Road Landfill", (-41.5, 175.0), basis="hint"),
    ]
    [row] = datasets_found(cited, [REGISTRY_SOURCE])
    assert (row["facilities_named"], row["facilities_placed"], row["why"]) == (1, 1, "found by 3 searches")


def test_a_town_page_many_searches_cite_is_no_dataset():
    """Batch 1's Brandon page was found by fourteen searches: a town's landfill page, not a dataset,
    since a municipality doesn't publish them."""
    page = "https://www.brandon.ca/landfill"
    cited = [cite(sid, page, "Eastview Landfill") for sid in ("13338", "13339", "13340")]
    source = {**REGISTRY_SOURCE, "url_normalized": page, "url": page, "source_type": "Municipal document"}
    assert datasets_found(cited, [source]) == []


# --- possible duplicates -------------------------------------------------------------------------
def test_two_sites_cited_under_the_same_page_and_name_are_listed_once_with_every_page():
    sites = {
        "13338": seed("13338", "Eastview Landfill", 49.83866, -99.88844),
        "18499": seed("18499", "Eastview Landfill", 43.5742, -80.2323),
    }
    index = "https://www.gov.mb.ca/sd/eal/registries/5597brandoneastview/index.html"
    appendix = "https://www.gov.mb.ca/sd/eal/registries/5597brandoneastview/eap/appendix_f_j.pdf"
    shared = {
        (index, "city of brandon eastview landfill"): [
            cite("13338", index, "City of Brandon - Eastview Landfill"), cite("18499", index, "City of Brandon - Eastview Landfill"),
        ],
        (appendix, "eastview landfill"): [cite("18499", appendix, "Eastview Landfill"), cite("13338", appendix, "Eastview Landfill")],
    }
    [row] = possible_duplicates(shared, sites)
    assert (row["site_id_a"], row["site_id_b"], row["searched_site_ids"]) == ("13338", "18499", "13338; 18499")
    assert row["source_urls"] == f"{appendix}; {index}"
    assert row["facility_names_in_source"] == "City of Brandon - Eastview Landfill; Eastview Landfill"
    assert float(row["distance_km"]) > 1600
    assert row["reason"] == (
        "Both sites' searches cited the same page under the same facility name, but the sites are 1,646 km "
        "apart: more likely one search claimed the other's source than one facility recorded twice"
    )


def test_two_sites_close_together_on_one_page_and_name_may_be_one_facility():
    """Cowpen Bewley's E-PRTR re-registration: two records of ours at one place."""
    sites = {"18572": seed("18572", "Cowpen Bewley", 54.6136, -1.24064), "19041": seed("19041", "Cowpen Bewley", 54.6136, -1.24064)}
    page = "https://environment.data.gov.uk/public-register/view/EPR-RP3531DV"
    [row] = possible_duplicates({(page, "cowpen bewley"): [cite("18572", page, "Cowpen Bewley"), cite("19041", page, "Cowpen Bewley")]}, sites)
    assert (row["distance_km"], row["reason"]) == ("0.00", "Both sites' searches cited the same page under the same facility name")


def test_a_page_cited_with_no_name_for_two_sites_is_no_duplicate():
    """Both pairs still go to an SME, but a blank name says nothing of two sites being one: in the
    gpt-5.4 test, Ontario's province-wide landfill gas dataset paired sites 332 km apart."""
    sites = {sid: seed(sid, f"Site {sid}", -41.0, 175.0) for sid in ("9", "10")}
    assert possible_duplicates({("https://p.example", ""): [cite(sid, "https://p.example") for sid in ("10", "9")]}, sites) == []


def test_three_sites_on_one_page_and_name_are_three_pairs():
    sites = {sid: seed(sid, f"Site {sid}", -41.0, 175.0) for sid in ("9", "10", "11")}
    shared = {("https://p.example", "site"): [cite(sid, "https://p.example", "Site") for sid in ("10", "9", "11")]}
    pairs = [(row["site_id_a"], row["site_id_b"]) for row in possible_duplicates(shared, sites)]
    assert pairs == [("9", "10"), ("9", "11"), ("10", "11")]
    assert {row["distance_km"] for row in possible_duplicates(shared, sites)} == {"0.00"}


# --- the rest ------------------------------------------------------------------------------------
def test_names_are_compared_without_case_accents_or_punctuation():
    assert plain("City of Brandon – Eastview Landfill") == plain("city of brandon eastview LANDFILL")
    assert plain("Décharge de l'Île") == "decharge de l ile"


def test_the_nearest_site_is_measured_to_its_edge():
    near = Site.from_seed(seed("1", "Big Landfill", 49.80, -99.90, area_square_meters="3141592.65"))
    far = Site.from_seed(seed("2", "Small Landfill", 49.82, -99.90))
    km, site = SiteIndex([near, far]).nearest((49.83, -99.90))
    # The first is 3.3 km away to its point, less its 1 km radius; the second 1.1 km away.
    assert site.site_id == "2" and round(km, 1) == 1.1
    km, site = SiteIndex([near]).nearest((49.83, -99.90))
    assert round(km, 1) == 2.3


def test_a_candidate_id_is_the_same_each_time():
    assert candidate_id("SME", "13338", "https://a.example") == candidate_id("SME", "13338", "https://a.example")
    assert candidate_id("SME", "13338", "https://a.example").startswith("WAINEW-")
    assert candidate_id("SME", "13338", "https://a.example") != candidate_id("SME", "13339", "https://a.example")
