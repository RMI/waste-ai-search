"""Each prompt names our other sites near the site searched (Mexico test, 30 October).

In test10-mex-sample2 the first search gave Las Varas pages about Madera's new landfill, none of
which named Las Varas, and the second declined pages like them. Each prompt now says a page about
the town's waste, another of its sites, or one of our other sites nearby is not about the site
searched unless it describes it too. It names our other sites in the same country that are within
15 km, or within 60 km and in the same municipality and state, so the agent can tell them apart.
The geocoder names a site in a town by the town and one outside it by its municipio, so the names
alone miss some; hence the distance. Our records under 1 km away are named apart, as possibly the
same site recorded twice.
"""
from __future__ import annotations

import json
import math

from waste_ai_search.geocoder import (
    MAX_OTHER_SITES,
    OTHER_SITES_FIELD,
    SAME_POINT_FIELD,
    load_geocode_cache,
    with_other_sites,
)

LAT, LON = 17.38, -98.021
KM = 1 / (6371.0088 * math.pi / 180)  # a kilometre of latitude, in degrees, as haversine_km measures
KM_EAST = KM / math.cos(math.radians(LAT))  # a kilometre of longitude here


def site(site_id, km_north=0.0, km_east=0.0, town="San Sebastián Tecomaxtlahuaca", state="Oaxaca", iso3="MEX", **extra):
    """One of our sites, `km_north` and `km_east` of the site searched, with its place as the seed gives it."""
    return {
        "site_id": site_id, "site_name": f"Site {site_id}", "country_iso3": iso3,
        "latitude": str(LAT + km_north * KM), "longitude": str(LON + km_east * KM_EAST),
        "municipality": town, "admin1": state, **extra,
    }


def named(selected, sites, cache_path=None, geocoder=None, field=OTHER_SITES_FIELD):
    return {s["site_id"]: s.get(field, []) for s in with_other_sites(selected, sites, cache_path, geocoder)}


def test_our_other_sites_in_the_municipality_or_within_15_km_are_named_nearest_first():
    searched = site("6086", original_site_name="BASURERO DE TECOMAXTLAHUACA")
    sites = [
        searched,
        site("2", 40, original_site_name="RELLENO LEJANO"),           # same town, a big municipio
        site("1", 3, original_site_name="TIRADERO LA LOMA"),          # same town, 3 km
        site("3", 40, town="Tlapa de Comonfort"),                     # another town, 40 km
        site("4", -10, town="Santiago Juxtlahuaca"),                  # another town, but 10 km
        site("5", 40, state="Guerrero"),                               # a town of the same name elsewhere
        site("6", 2, iso3="GTM"),                                       # across a border
    ]
    assert named([searched], sites)["6086"] == [
        "TIRADERO LA LOMA (San Sebastián Tecomaxtlahuaca, 3 km away)",
        "Site 4 (Santiago Juxtlahuaca, 10 km away)",
        "RELLENO LEJANO (San Sebastián Tecomaxtlahuaca, 40 km away)",
    ]


def test_the_15_km_cut_off_holds_in_every_direction():
    searched = site("0")
    inside, outside = site("1", km_east=14.9, town="Otro"), site("2", km_east=-15.1, town="Otro")
    assert named([searched], [searched, inside, outside])["0"] == ["Site 1 (Otro, 15 km away)"]


def test_nearest_first_whatever_the_seed_order_and_the_nearest_ten_are_named():
    searched = site("0")
    offsets = {"a": (9, 0), "b": (0, -2), "c": (-6, 0), "d": (0, 4), "e": (1, 1), "f": (-3, -3),
               "g": (0, 11), "h": (-12, 0), "i": (5, 5), "j": (0, -8), "k": (13, 0), "l": (-2.5, 0)}
    sites = [searched, *(site(key, north, east) for key, (north, east) in offsets.items())]
    listed = named([searched], sites)["0"]
    nearest = sorted(offsets, key=lambda key: math.hypot(*offsets[key]))
    assert [entry.split(" (")[0] for entry in listed[:-1]] == [f"Site {key}" for key in nearest[:MAX_OTHER_SITES]]
    assert listed[-1] == "and 2 more"


def test_our_records_under_1_km_away_are_named_apart_as_possibly_this_site():
    """Mexico's INEGI rows hold some dumps twice: 8690 and 8689 are 3 m apart."""
    searched = site("8690")
    twin, neighbour = site("8689", 0.003), site("1", 5)
    [result] = with_other_sites([searched], [searched, twin, neighbour], None, None)
    assert result[SAME_POINT_FIELD] == ["Site 8689 (San Sebastián Tecomaxtlahuaca, under 1 km away)"]
    assert result[OTHER_SITES_FIELD] == ["Site 1 (San Sebastián Tecomaxtlahuaca, 5 km away)"]


def test_a_generic_label_is_named_as_one():
    searched = site("5957", original_site_name="BASURERO MUNICIPAL SAN MATEO DEL MAR")
    other = site("6037", 6, site_name="Municipal Dump", original_site_name="BASURERO MUNICIPAL")
    assert named([searched], [searched, other])["5957"] == [
        'unnamed site, recorded as "BASURERO MUNICIPAL" (San Sebastián Tecomaxtlahuaca, 6 km away)',
    ]


def test_town_names_compare_without_case_or_accents():
    searched = site("6086")
    other = site("1", 30, town="san sebastian  tecomaxtlahuaca", state="OAXACA")
    assert named([searched], [searched, other])["6086"] == ["Site 1 (san sebastian  tecomaxtlahuaca, 30 km away)"]


def test_a_site_with_none_near_it_names_none():
    searched = site("6086")
    [result] = with_other_sites([searched], [searched, site("1", 100)], None, None)
    assert OTHER_SITES_FIELD not in result and SAME_POINT_FIELD not in result


def test_other_sites_searched_in_the_same_run_name_each_other():
    one, two = site("1"), site("2", 2)
    assert named([one, two], [one, two]) == {"1": ["Site 2 (San Sebastián Tecomaxtlahuaca, 2 km away)"],
                                             "2": ["Site 1 (San Sebastián Tecomaxtlahuaca, 2 km away)"]}


def test_without_places_only_distance_counts():
    """Neither our site nor the others have a place: a blank town is no town, so it matches none."""
    searched = site("6086", town="", state="")
    near, far = site("1", 5, town="", state=""), site("2", 40, town="", state="")
    assert named([searched], [searched, near, far])["6086"] == ["Site 1 (5 km away)"]


class Places:
    """A geocoder that looks points up, counting each lookup."""

    name = "nominatim"

    def __init__(self):
        self.asked = []

    def reverse(self, latitude, longitude):
        self.asked.append((latitude, longitude))
        return {"municipality": "San Sebastián Tecomaxtlahuaca", "admin1": "Oaxaca", "admin2": ""}


def test_the_places_of_our_other_sites_are_looked_up_once_and_only_within_60_km(tmp_path):
    cache = tmp_path / "geocode_cache.jsonl"
    searched = site("6086")
    near = site("1", 40, town="", state="")          # within 60 km: looked up, then the same town
    east = site("2", km_east=100, town="", state="")  # level with it but 100 km east: never looked up
    north = site("3", 100, town="", state="")         # 100 km north: never looked up
    geocoder = Places()
    assert named([searched], [searched, near, east, north], cache, geocoder)["6086"] == [
        "Site 1 (San Sebastián Tecomaxtlahuaca, 40 km away)",
    ]
    assert len(geocoder.asked) == 1 and "site:1" in load_geocode_cache(cache)
    # The answer is cached, so another run asks nothing.
    named([searched], [searched, near, east, north], cache, geocoder)
    assert len(geocoder.asked) == 1


class _Client:
    def __init__(self):
        self.prompts = {}

    def search_site(self, site, prompt):
        self.prompts[site["site_id"]] = prompt
        return {"sources": [], "unverified_leads": [], "search_notes": "Nothing found."}


def _search(tmp_path, monkeypatch, cache_rows=(), **settings):
    from waste_ai_search import search as se

    seed = tmp_path / "seed.csv"
    seed.write_text(
        "site_id,site_name,original_site_name,country_iso3,latitude,longitude\n"
        f"6086,Tecomaxtlahuaca Dump,BASURERO DE TECOMAXTLAHUACA,MEX,{LAT},{LON}\n"
        f"7001,Municipal Dump,BASURERO MUNICIPAL,MEX,{LAT + 4 * KM},{LON}\n"
        f"7002,Far Dump,TIRADERO LEJANO,MEX,{LAT + 40 * KM},{LON}\n",
        encoding="utf-8",
    )
    cache = tmp_path / "geocode_cache.jsonl"
    cache.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in cache_rows), encoding="utf-8")
    client = _Client()
    monkeypatch.setattr(se, "get_client", lambda config: client)
    config = se.PipelineConfig(
        input_csv=seed, run_dir=tmp_path / "run", run_id="t", site_delay_seconds=0, site_ids=["6086"], locate=False,
        geocode_cache=cache, **settings,
    )
    se.run_search(config)
    return json.loads(client.prompts["6086"].split("\nSite context:\n", 1)[1].split("\n\n", 1)[0])


def test_each_prompt_names_our_other_sites_near_it_even_those_not_searched(tmp_path, monkeypatch):
    """A site skipped for its generic name is still one of ours. Without the cache, distance alone."""
    context = _search(tmp_path, monkeypatch)
    assert context[OTHER_SITES_FIELD] == ['unnamed site, recorded as "BASURERO MUNICIPAL" (4 km away)']


def test_with_the_cache_a_site_farther_off_in_the_same_municipality_is_named(tmp_path, monkeypatch):
    place = {"municipality": "San Sebastián Tecomaxtlahuaca", "admin1": "Oaxaca", "admin2": ""}
    rows = [{"site_id": site_id, "coord_key": "", **place} for site_id in ("6086", "7001", "7002")]
    context = _search(tmp_path, monkeypatch, cache_rows=rows, use_geocode_cache=True)
    assert context["municipality"] == "San Sebastián Tecomaxtlahuaca"
    assert context[OTHER_SITES_FIELD] == [
        'unnamed site, recorded as "BASURERO MUNICIPAL" (San Sebastián Tecomaxtlahuaca, 4 km away)',
        "TIRADERO LEJANO (San Sebastián Tecomaxtlahuaca, 40 km away)",
    ]
