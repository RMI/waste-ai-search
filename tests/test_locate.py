"""Locate: where each source puts the facility becomes a point (build step 4 of the source-matching
plan).

Each source states its coordinates or grid reference (stated_coordinates) and its address
(stated_address), as written (step 1 of the approve-sources plan). Coordinates and grid references
are read locally. Addresses go to a geocoder, which only the locate step calls, and every answer is
cached by text and country. Every geocoder here is a fake: tests/conftest.py stops any real request.
"""
from __future__ import annotations

import pytest

from waste_ai_search.locate import (
    ALONG_A_ROAD_REASON,
    AZURE_MAPS_API_VERSION,
    COORDINATES,
    DEFAULT_NOMINATIM_USER_AGENT,
    GRID_REFERENCE,
    HINT,
    MAJOR_ROAD,
    MAJOR_ROAD_REASON,
    NO_GEOCODER,
    NO_PLACE_REASON,
    NONE,
    NOT_LOCATED,
    ROOFTOP,
    STREET,
    AzureMapsGeocoder,
    GeocodeMatch,
    GeocoderError,
    Located,
    NominatimGeocoder,
    address_to_send,
    azure_match,
    cache_key,
    coordinates_from,
    describe_bases,
    haversine_km,
    iso2,
    load_locations,
    locate_source,
    make_geocoder,
    nominatim_match,
    parse_coordinates,
    parse_grid_reference,
    parse_point,
    plainer_address,
    save_locations,
    stated_address,
    stated_coordinates,
    stated_location_text,
    states_location,
    with_place,
)


def near(located, lat, lon, metres=60.0):
    assert located is not None and located.point is not None, located
    distance = haversine_km(located.latitude, located.longitude, lat, lon) * 1000
    assert distance <= metres, f"{located} is {distance:.0f} m from ({lat}, {lon})"


# --- coordinates -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "48.843368, -87.429626",
        "Lat: 48.8434, Long: -87.4296",
        "Longitude -87.4296, Latitude 48.8434",
        "48.8434°N 87.4296°W",
        "87.4296 W, 48.8434 N",
        "N 48.8434 W 87.4296",
    ],
)
def test_decimal_coordinates_are_read_in_either_order_when_the_text_says_which(text):
    located = parse_coordinates(text, strict=True)
    assert located.basis == COORDINATES
    near(located, 48.8434, -87.4296, metres=20)


def test_lon_lat_is_read_only_when_unambiguous():
    swapped = parse_coordinates("151.2093, -33.8688")
    assert (swapped.latitude, swapped.longitude) == (-33.8688, 151.2093)
    # Both fit a latitude, so the order written is kept.
    kept = parse_coordinates("-87.4296, 48.8434")
    assert (kept.latitude, kept.longitude) == (-87.4296, 48.8434)


def test_degrees_minutes_and_seconds():
    # Batch 1's one DMS location, cached as the agent wrote it.
    located = parse_coordinates("63° 43' 58.15\" N, 68° 32' 08.54\" W")
    assert located.basis == COORDINATES
    near(located, 63.732819, -68.535706, metres=1)
    near(parse_coordinates("51°30'26\"N 0°7'39\"W"), 51.507222, -0.1275, metres=1)
    near(parse_coordinates("51°30.25'N 0°7.65'W"), 51.504167, -0.1275, metres=1)


def test_coarse_coordinates_are_only_a_hint():
    assert parse_coordinates("48.84, -87.43").basis == HINT
    assert parse_coordinates("51°30'N 0°7'W").basis == HINT
    assert parse_coordinates("48.8, -87.4296").basis == HINT  # the coarser axis decides


def test_decimal_commas_are_read():
    near(parse_coordinates("48,8434; 2,3488"), 48.8434, 2.3488, metres=1)


def test_out_of_range_coordinates_are_no_location():
    assert parse_coordinates("95.123, 200.456").basis == NONE
    assert parse_coordinates("95.123, 200.456", strict=True) is None
    assert parse_coordinates("0.000, 0.000") is None or parse_coordinates("0.000, 0.000").basis == NONE


@pytest.mark.parametrize("text", ["1777 Brady Road, Winnipeg, MB R3V 1A5", "Km 12.5 Carretera 45.2", "Lot 4, Concession 6"])
def test_an_address_is_not_read_as_coordinates(text):
    assert parse_point(text, "CAN", strict=True) is None


# --- grid references --------------------------------------------------------------------------
def test_os_grid_references_convert_to_known_points():
    # Ben Nevis summit and Nelson's Column, at the published 1 m references.
    near(parse_grid_reference("NN 16667 71283", "GBR"), 56.796891, -5.003675, metres=10)
    near(parse_grid_reference("TQ 30015 80430"), 51.507791, -0.127970, metres=15)
    for spelling in ("NN1666771283", "NGR NN 16667 71283", "nn 16667 71283".upper()):
        near(parse_grid_reference(spelling, "GBR", strict=True), 56.796891, -5.003675, metres=10)


def test_a_grid_reference_coarser_than_100_m_is_a_hint():
    assert parse_grid_reference("SU 123 456").basis == GRID_REFERENCE
    assert parse_grid_reference("SU 1234").basis == HINT
    assert parse_grid_reference("SU1234").basis == HINT


def test_irish_grid_and_itm_convert_to_the_same_point():
    # The Spire, O'Connell Street, Dublin.
    irish = parse_grid_reference("O 15904 34671", "IRL")
    itm = parse_grid_reference("ITM 715830, 734697", "IRL")
    labelled = parse_grid_reference("ITM N 734697 E 715830", "IRL")
    for located in (irish, itm, labelled):
        assert located.basis == GRID_REFERENCE
        near(located, 53.349798, -6.260254, metres=5)


def test_irish_grid_letters_are_read_only_in_ireland_and_the_uk():
    assert parse_grid_reference("O 15904 34671", "FRA") is None
    # ITM is read only where the text names it.
    assert parse_grid_reference("715830, 734697", "IRL") is None


@pytest.mark.parametrize("text", ["SW1A 1AA", "Unit A 12 34, Dublin Road", "M50 Junction 9"])
def test_postcodes_and_addresses_are_not_grid_references(text):
    assert parse_point(text, "IRL", strict=True) is None


# --- one source ---------------------------------------------------------------------------------
SITE = {"site_id": "13755", "country_iso3": "CAN", "latitude": "48.843368376131856", "longitude": "-87.42962607259884"}


def source(coordinates="", address="", url="https://data.ontario.ca/landfills.csv", **extra):
    """A source as the agent reports one: the coordinates and the address it states."""
    return {"url": url, "stated_coordinates": coordinates, "stated_address": address, **extra}


class FakeGeocoder:
    name = "fake"

    def __init__(self, answers=None, error=None):
        self.answers = answers or {}
        self.error = error
        self.calls = []

    def geocode(self, address, iso2_code):
        self.calls.append((address, iso2_code))
        if self.error:
            raise self.error
        return self.answers.get(address)


def test_stated_coordinates_become_the_sources_location():
    located = locate_source(source("48.4410, -89.3690"), SITE)
    assert (located.basis, located.value()) == (COORDINATES, "48.441, -89.369")


def test_no_location_stated():
    for stated in ("", "None", "n/a", "Not stated"):
        assert locate_source(source(stated, stated), SITE).reason == "no location stated"
    assert locate_source({"url": "https://a.example"}, SITE).reason == "no location stated"


def test_coordinates_equal_to_the_prompts_count_as_copied():
    located = locate_source(source("48.84, -87.43"), SITE)
    assert (located.basis, located.reason) == (NONE, "copied from the prompt")


def test_coordinates_equal_to_our_own_point_are_a_location():
    """Reversed on purpose for build step 5. Batch 1's prompts carried our full point, and the agent
    copied it to every digit and credited it to Ontario's registry, so a point within 10 m of ours
    counted as copied. The prompt now carries only the rounded point, so a source stating our exact
    point agrees with us - the regulator our seed came from, say - and is matched and confirmed."""
    located = locate_source(source("48.843368376131856, -87.42962607259884"), SITE)
    assert (located.basis, located.reason) == (COORDINATES, "")
    assert locate_source(source("48.8438, -87.4296"), SITE).basis == COORDINATES


@pytest.mark.parametrize("stated, copied", [
    (("48.843368376131856", "-87.42962607259884"), True),  # Thunder Bay's, to every digit
    (("48.84336849", "-87.4296291"), True),                 # with fewer digits, as Sudbury's was
    (("48.8438", "-87.4296"), False),                       # 50 m away
])
def test_in_a_response_from_before_the_rounding_our_own_point_is_the_prompts(stated, copied):
    """Batch 1's prompts carried our full point, and the agent copied it six times, each within a
    few metres, and credited it to Ontario's registry. An attribute-first response came from such
    a prompt, so there a point within 10 m of ours is copied. A source-first one, above, is not."""
    from waste_ai_search.payload import read_payload

    registry = {"url": "https://files.ontario.ca/opendata/large_landfills.csv", "source_type": "Permit or registry"}
    old = read_payload({"attributes": [
        {"attribute_name": "found_latitude", "value": stated[0], "value_basis": "Direct", "confidence_score": "High", "sources": [registry]},
        {"attribute_name": "found_longitude", "value": stated[1], "value_basis": "Direct", "confidence_score": "High", "sources": [registry]},
    ]})
    [converted] = old["sources"]
    located = locate_source(converted, SITE)
    assert (located.basis, located.reason) == ((NONE, "copied from the prompt") if copied else (COORDINATES, ""))


def test_wastebooks_coordinates_are_never_a_location():
    located = locate_source(source("52.1234, -1.2345", url="https://www.wastebook.co.uk/site/123"), SITE)
    assert (located.basis, located.reason) == (NONE, "wastebook.co.uk's coordinates are postcode centroids")


def test_coordinates_given_as_the_address_still_count():
    assert locate_source(source(address="48.4410, -89.3690"), SITE).basis == COORDINATES
    assert locate_source(source(address="Lat: 48.4410, Long: -89.3690"), SITE).basis == COORDINATES


def test_stated_coordinates_that_cannot_be_read_leave_the_address():
    unread = locate_source(source("SW-02-17-26-W2M"), SITE)
    assert (unread.basis, unread.reason) == (NONE, "could not read the stated coordinates")
    geocoder = FakeGeocoder({"1802 Caribou Street East, Swift Current": GeocodeMatch(50.28, -107.76, ROOFTOP, "")})
    located = locate_source(source("SW-02-17-26-W2M", "1802 Caribou Street East, Swift Current"), SITE, {}, geocoder, live=True)
    assert located.basis == ROOFTOP


def test_an_older_responses_location_is_read_by_its_kinds():
    """Before step 1 of the approve-sources plan a source gave one stated_location, labelled part by
    part. The reader splits it, so an older run locates as it did, except that a place description
    is now an address, and sent to the geocoder like one."""
    from waste_ai_search.payload import read_payload

    def old(stated, kind):
        [read] = read_payload({"sources": [{"url": "https://a.example", "stated_location": stated, "stated_location_kind": kind}]})["sources"]
        return read

    geocoder = FakeGeocoder({
        "Gate Road": GeocodeMatch(48.4500, -89.3700, ROOFTOP, ""),
        "Mapleward Road east of Thunder Bay, Ontario": GeocodeMatch(48.40, -89.20, STREET, ""),
    })
    both = old("48.4410, -89.3690 | Gate Road", "coordinates | street address")
    assert (both["stated_coordinates"], both["stated_address"]) == ("48.4410, -89.3690", "Gate Road")
    assert locate_source(both, SITE, {}, geocoder, live=True).value() == "48.441, -89.369"
    # Labels were never reliable: coordinates labelled an address, or not at all, still count.
    assert locate_source(old("48.4410, -89.3690", "street address"), SITE).basis == COORDINATES
    assert locate_source(old("48.4410, -89.3690", ""), SITE).basis == COORDINATES
    place = locate_source(old("Mapleward Road east of Thunder Bay, Ontario", "place description"), SITE, {}, geocoder, live=True)
    assert place.basis == STREET
    assert locate_source(old("Not stated", "none"), SITE).reason == "no location stated"


def test_without_a_geocoder_an_address_stays_unlocated_and_says_so():
    cache = {}
    stated = source(address="1777 Brady Road, Winnipeg, MB")
    located = locate_source(stated, SITE, cache=cache, live=True)
    assert (located.basis, located.reason) == (NONE, NO_GEOCODER)
    assert cache[cache_key("1777 Brady Road, Winnipeg, MB", "CAN")]["reason"] == NO_GEOCODER
    # Arbitration reads the same answer back, and without the cache says to run locate.
    assert locate_source(stated, SITE, cache=cache).reason == NO_GEOCODER
    assert locate_source(stated, SITE).reason == NOT_LOCATED


def test_a_rooftop_address_is_a_location_and_a_street_one_matches_only():
    geocoder = FakeGeocoder({
        "1777 Brady Road, Winnipeg, MB": GeocodeMatch(49.7958, -97.1601, ROOFTOP, "Address, Rooftop"),
        "Brady Road, Winnipeg, MB": GeocodeMatch(49.80, -97.16, STREET, "RoadBlock"),
        "Winnipeg, MB": GeocodeMatch(49.89, -97.13, "", "PopulatedPlace"),
    })
    rooftop = locate_source(source(address="1777 Brady Road, Winnipeg, MB"), SITE, {}, geocoder, live=True)
    assert (rooftop.basis, rooftop.value(), rooftop.geocoder) == (ROOFTOP, "49.7958, -97.1601", "fake")
    street = locate_source(source(address="Brady Road, Winnipeg, MB"), SITE, {}, geocoder, live=True)
    assert street.basis == STREET
    town = locate_source(source(address="Winnipeg, MB"), SITE, {}, geocoder, live=True)
    assert (town.basis, town.reason) == (NONE, "geocoded only coarser than a street, so not used")
    missing = locate_source(source(address="Nowhere Lane, Winnipeg, MB"), SITE, {}, geocoder, live=True)
    assert (missing.basis, missing.reason) == (NONE, "the geocoder found no match")
    assert {country for _address, country in geocoder.calls} == {"CA"}


def test_an_address_is_geocoded_once_per_country():
    geocoder = FakeGeocoder({"1 Tip Road, Hamilton": GeocodeMatch(-37.78, 175.28, ROOFTOP, "")})
    cache = {}
    nzl = {**SITE, "country_iso3": "NZL"}
    for _ in range(2):
        locate_source(source(address="1 Tip Road, Hamilton"), nzl, cache, geocoder, live=True)
    assert geocoder.calls == [("1 Tip Road, Hamilton", "NZ")]
    locate_source(source(address="1 Tip Road, Hamilton"), {**nzl, "country_iso3": "CAN"}, cache, geocoder, live=True)
    assert len(geocoder.calls) == 2


def test_an_address_cached_without_a_geocoder_is_tried_again_once_there_is_one():
    cache = {}
    stated = source(address="1 Tip Road, Thunder Bay")
    locate_source(stated, SITE, cache, None, live=True)
    geocoder = FakeGeocoder({"1 Tip Road, Thunder Bay": GeocodeMatch(48.44, -89.37, ROOFTOP, "")})
    assert locate_source(stated, SITE, cache, geocoder, live=True).basis == ROOFTOP


def test_a_failed_lookup_is_not_cached():
    cache = {}
    with pytest.raises(GeocoderError):
        locate_source(source(address="1 Tip Road, Thunder Bay"), SITE, cache, FakeGeocoder(error=TimeoutError("slow")), live=True)
    assert cache_key("1 Tip Road, Thunder Bay", "CAN") not in cache


def test_po_boxes_and_facility_names_are_never_sent():
    geocoder = FakeGeocoder()
    po_box = locate_source(source(address="PO Box 1234, Thunder Bay, ON"), SITE, {}, geocoder, live=True)
    assert po_box.reason == "a PO box is not where the site is"
    named = source(
        address="Thunder Bay Solid Waste and Recycling Facility",
        facility_name_in_source="Thunder Bay Solid Waste and Recycling Facility",
    )
    assert "facility's name" in locate_source(named, SITE, {}, geocoder, live=True).reason
    assert geocoder.calls == []


HASARS = {"site_id": "3560", "country_iso3": "MEX", "latitude": "20.922", "longitude": "-103.453"}


@pytest.mark.parametrize(
    "stated",
    [
        # As Zapopan's page gave it, in test10-mex-sample2: Nominatim matched the road near Colotlán,
        # 129 km from the site.
        "CARRETERA COLOTLAN KM 15.5",
        "Km. 3.5 Carretera Federal 200, Bahía de Banderas, Nayarit",
        "Kilómetro 12 de la carretera México-Puebla",
        "Carretera a Tlapa, a 3 km del centro",
        "5 km east of Thunder Bay on Highway 61",
    ],
)
def test_a_kilometre_marker_or_a_distance_is_never_sent(stated):
    """A geocoder finds the road or the town by name, and any stretch of the road will do."""
    geocoder = FakeGeocoder({stated: GeocodeMatch(22.0682, -103.2724, STREET, "highway=primary")})
    # As test10-mex-sample2's file held it from before: the answer is replaced, never read.
    cache = {cache_key(stated, "MEX"): {"text": stated, "country": "MEX", "latitude": 22.0682, "longitude": -103.2724,
                                         "basis": STREET, "reason": "", "geocoder": "nominatim", "match": "highway=primary"}}
    located = locate_source(source(address=stated), HASARS, cache, geocoder, live=True)
    assert (located.basis, located.reason) == (NONE, ALONG_A_ROAD_REASON)
    assert geocoder.calls == []
    assert (cache[cache_key(stated, "MEX")]["basis"], cache[cache_key(stated, "MEX")]["reason"]) == (NONE, ALONG_A_ROAD_REASON)


def test_an_address_with_a_distance_is_sent_plainer_without_it():
    stated = (
        "4 Sand Dam Road, North Bay, approximately 21 kilometers from downtown North Bay, off Highway 11 at the "
        "end of Sand Dam Road"
    )
    geocoder = FakeGeocoder({"4 Sand Dam Road, North Bay": GeocodeMatch(46.40, -79.40, ROOFTOP, "")})
    located = locate_source(source(address=stated), SITE, {}, geocoder, live=True)
    assert located.basis == ROOFTOP
    assert geocoder.calls == [("4 Sand Dam Road, North Bay", "CA")]


@pytest.mark.parametrize("stated", ["Calle Kmita 12, Monterrey", "1777 Brady Road, Winnipeg"])
def test_an_address_with_no_kilometre_marker_is_sent(stated):
    geocoder = FakeGeocoder()
    locate_source(source(address=stated), HASARS, {}, geocoder, live=True)
    assert geocoder.calls[0] == (stated, "MX")


def test_a_match_on_a_major_road_itself_is_not_used():
    """With its house number not found, a road between towns, or a city's arterial, could be met
    anywhere along it: each such match in the October 2026 test runs was more than 5 km off."""
    road = "carretera Guadalajara- El Salto, Tlaquepaque"
    geocoder = FakeGeocoder({road: GeocodeMatch(20.55, -103.31, MAJOR_ROAD, "highway=secondary on way, rank 26")})
    cache = {}
    located = locate_source(source(address=road), HASARS, cache, geocoder, live=True)
    assert (located.basis, located.reason) == (NONE, MAJOR_ROAD_REASON)
    assert cache[cache_key(road, "MEX")]["match"] == "highway=secondary on way, rank 26"


def test_a_leading_facility_name_is_taken_off_the_address():
    geocoder = FakeGeocoder()
    stated = source(
        address="Brady Road Landfill, 1777 Brady Road, Winnipeg",
        facility_name_in_source="Brady Road Landfill",
    )
    locate_source(stated, SITE, {}, geocoder, live=True)
    assert geocoder.calls == [("1777 Brady Road, Winnipeg", "CA")]


@pytest.mark.parametrize(("stated", "plainer"), [
    ("765 33rd Street East in Brandon, Manitoba", "765 33rd Street East, Brandon, Manitoba"),
    ("3610 Victoria Avenue East on NW 1\u20444 17-10-18 WPM in Brandon, Manitoba", "3610 Victoria Avenue East, Brandon, Manitoba"),
    ("110 Dunlop Drive in the southeast part of Guelph", "110 Dunlop Drive, Guelph"),
    ("Lots 1 & 2, 16 Ball Road in Heatherton", "16 Ball Road, Heatherton"),
    ("Reynolds Bach Drive, accessed from Eastern Hutt Road in Silverstream.", "Reynolds Bach Drive, Silverstream"),
    (
        "4 Sand Dam Road, North Bay, approximately 21 kilometers from downtown North Bay, off Highway 11 at the "
        "end of Sand Dam Road",
        "4 Sand Dam Road, North Bay",
    ),
    ("5494 Mapleward Road, City of Thunder Bay, Thunder Bay", "5494 Mapleward Road, Thunder Bay"),
    ("Eastview Landfill at 3000 Victoria Avenue East in Brandon", "3000 Victoria Avenue East, Brandon"),
])
def test_an_address_written_as_a_sentence_has_a_plainer_form(stated, plainer):
    # Each is as the agent copied it from a source in the 10-site test runs.
    assert plainer_address(stated, ["Eastview Landfill"]) == plainer


@pytest.mark.parametrize("stated", [
    "1777 Brady Road, Winnipeg, MB",
    # Without its land description the street has no place, and one in another town could match.
    "3610 Victoria Avenue East, on NW1/4 Sec. 17, Twp. 10, and Rge. 18 W.",
    "SW-02-17-26-W2M",
    "Burnfort, Mallow, Co. Cork",
])
def test_an_address_with_nothing_to_take_out_or_no_street_and_place_left_has_no_plainer_form(stated):
    assert plainer_address(stated, []) == ""


def test_an_address_the_geocoder_cannot_find_as_written_is_sent_again_plainer():
    geocoder = FakeGeocoder({
        "765 33rd Street East, Brandon, Manitoba": GeocodeMatch(49.82644, -99.89425, STREET, "highway=residential"),
    })
    cache = {}
    stated = source(address="765 33rd Street East in Brandon, Manitoba")
    located = locate_source(stated, SITE, cache, geocoder, live=True)
    assert (located.basis, located.value()) == (STREET, "49.82644, -99.89425")
    assert [address for address, _country in geocoder.calls] == [
        "765 33rd Street East in Brandon, Manitoba", "765 33rd Street East, Brandon, Manitoba",
    ]
    # Arbitration reads both answers back from the cache, and asks nothing.
    assert locate_source(stated, SITE, cache).value() == "49.82644, -99.89425"
    assert len(geocoder.calls) == 2


def test_an_address_found_as_written_is_not_sent_again():
    geocoder = FakeGeocoder({"765 33rd Street East in Brandon, Manitoba": GeocodeMatch(49.8, -99.9, ROOFTOP, "")})
    locate_source(source(address="765 33rd Street East in Brandon, Manitoba"), SITE, {}, geocoder, live=True)
    assert len(geocoder.calls) == 1


def test_coordinates_win_over_an_address_unless_it_lands_more_than_2_km_away():
    geocoder = FakeGeocoder({
        "Gate Road, Thunder Bay": GeocodeMatch(48.4500, -89.3700, ROOFTOP, ""),   # about 1 km away
        "Head Office, Thunder Bay": GeocodeMatch(48.3800, -89.2500, ROOFTOP, ""),  # about 11 km away
    })
    close = locate_source(source("48.4410, -89.3690", "Gate Road, Thunder Bay"), SITE, {}, geocoder, live=True)
    assert (close.basis, close.value()) == (COORDINATES, "48.441, -89.369")
    far = locate_source(source("48.4410, -89.3690", "Head Office, Thunder Bay"), SITE, {}, geocoder, live=True)
    assert far.basis == NONE and far.reason.startswith("doubtful: its address lands")


def test_a_located_address_beats_coarse_coordinates():
    geocoder = FakeGeocoder({"Gate Road, Thunder Bay": GeocodeMatch(48.4500, -89.3700, ROOFTOP, "")})
    located = locate_source(source("48.4, -89.4", "Gate Road, Thunder Bay"), SITE, {}, geocoder, live=True)
    assert located.basis == ROOFTOP


def test_copied_coordinates_leave_the_address():
    geocoder = FakeGeocoder({"Gate Road, Thunder Bay": GeocodeMatch(48.4500, -89.3700, ROOFTOP, "")})
    stated = source("48.84, -87.43", "Gate Road, Thunder Bay")  # the prompt's point
    assert locate_source(stated, SITE, {}, geocoder, live=True).basis == ROOFTOP


def test_the_locate_step_records_every_part_it_reads():
    cache = {}
    locate_source(source("SU 123 456"), {**SITE, "country_iso3": "GBR"}, cache, live=True)
    entry = cache[cache_key("SU 123 456", "GBR")]
    assert (entry["basis"], entry["geocoder"]) == (GRID_REFERENCE, "")


def test_the_cache_file_round_trips_and_an_unchanged_answer_keeps_its_date(tmp_path):
    path = tmp_path / "source_locations.json"
    cache = {}
    locate_source(source("48.4410, -89.3690"), SITE, cache, live=True)
    entry = next(iter(cache.values()))
    entry["located_at"] = "2026-01-01"
    save_locations(path, cache)
    again = load_locations(path)
    locate_source(source("48.4410, -89.3690"), SITE, again, live=True)
    assert next(iter(again.values()))["located_at"] == "2026-01-01"
    (tmp_path / "broken.json").write_text("{not json")
    assert load_locations(tmp_path / "broken.json") == {}
    assert load_locations(tmp_path / "missing.json") == {}


def test_what_a_source_states_is_read_from_its_two_fields():
    both = source("48.4410, -89.3690", "1 Gate Road, Thunder Bay")
    assert (stated_coordinates(both), stated_address(both)) == ("48.4410, -89.3690", "1 Gate Road, Thunder Bay")
    assert states_location(both) and states_location(source(address="1 Gate Road")) and states_location(source("SU 123 456"))
    # Shown together where a note or the review queue shows one location: coordinates first.
    assert stated_location_text(both) == "48.4410, -89.3690 | 1 Gate Road, Thunder Bay"
    assert stated_location_text(source(address="1 Gate Road")) == "1 Gate Road"
    # Words for no location are no location.
    nothing = source("n/a", "Not stated")
    assert (stated_coordinates(nothing), stated_address(nothing), stated_location_text(nothing)) == ("", "", "")
    assert not states_location(nothing)


@pytest.mark.parametrize(("basis", "said"), [
    (COORDINATES, "stated"),
    (GRID_REFERENCE, "grid reference"),
    (ROOFTOP, "address (building)"),
    (STREET, "address (street)"),
    (HINT, ""),
    (NONE, ""),
    ("", ""),
])
def test_coordinates_from_says_where_a_point_came_from(basis, said):
    assert coordinates_from(basis) == said


def test_a_sources_point_and_where_it_came_from():
    geocoder = FakeGeocoder({
        "1777 Brady Road, Winnipeg, MB": GeocodeMatch(49.7958, -97.1601, ROOFTOP, ""),
        "Brady Road, Winnipeg, MB": GeocodeMatch(49.80, -97.16, STREET, ""),
    })
    cases = [
        (source("49.7958, -97.1601", "1777 Brady Road, Winnipeg, MB"), "49.7958, -97.1601", "stated"),
        (source(address="1777 Brady Road, Winnipeg, MB"), "49.7958, -97.1601", "address (building)"),
        (source(address="Brady Road, Winnipeg, MB"), "49.8, -97.16", "address (street)"),
        (source("SU 123 456"), None, "grid reference"),
        (source("49.8, -97.2"), "49.8, -97.2", ""),  # a hint: approximate, with no point to use
    ]
    for stated, point, said in cases:
        located = locate_source(stated, {**SITE, "country_iso3": "GBR" if "SU" in stated["stated_coordinates"] else "CAN"}, {}, geocoder, live=True)
        assert coordinates_from(located.basis) == said, stated
        if point is not None:
            assert located.value() == point, stated


def test_describe_bases():
    located = [Located(1, 1, COORDINATES), Located(1, 1, COORDINATES), Located(basis=NONE), Located(1, 1, STREET)]
    assert describe_bases(located) == "coordinates 2, street 1, none 1"
    assert describe_bases([]) == "0"


# --- geocoders ----------------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return FakeResponse(self.payload)


# The first sample response in the Get Geocoding reference (api-version 2026-01-01).
AZURE_SAMPLE = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature",
        "properties": {
            "type": "Address",
            "confidence": "High",
            "matchCodes": ["Good"],
            "geocodePoints": [
                {"geometry": {"type": "Point", "coordinates": [-122.138681, 47.630358]},
                 "calculationMethod": "Rooftop", "usageTypes": ["Display"]},
                {"geometry": {"type": "Point", "coordinates": [-122.1386787, 47.6302179]},
                 "calculationMethod": "Rooftop", "usageTypes": ["Route"]},
            ],
        },
        "geometry": {"type": "Point", "coordinates": [-122.138681, 47.630358]},
    }],
}


def test_azure_maps_sends_the_address_line_with_its_country_and_key():
    session = FakeSession(AZURE_SAMPLE)
    found = AzureMapsGeocoder(subscription_key="key", session=session).geocode("15127 NE 24th Street, Redmond, WA", "US")
    assert (found.latitude, found.longitude, found.precision) == (47.630358, -122.138681, ROOFTOP)
    (call,) = session.calls
    assert call["url"] == "https://atlas.microsoft.com/geocode"
    assert call["params"] == {
        "api-version": AZURE_MAPS_API_VERSION, "addressLine": "15127 NE 24th Street, Redmond, WA",
        "top": 1, "countryRegion": "US",
    }
    assert call["headers"] == {"subscription-key": "key"}


def test_azure_maps_signs_in_with_entra_id_when_there_is_no_key():
    class Credential:
        def get_token(self, scope):
            assert scope == "https://atlas.microsoft.com/.default"
            return type("Token", (), {"token": "t0ken"})()

    session = FakeSession({"features": []})
    geocoder = AzureMapsGeocoder(client_id="maps-client", credential=Credential(), session=session)
    assert geocoder.geocode("1 Tip Road", "") is None
    assert session.calls[0]["headers"] == {"Authorization": "Bearer t0ken", "x-ms-client-id": "maps-client"}
    assert "countryRegion" not in session.calls[0]["params"]
    with pytest.raises(ValueError):
        AzureMapsGeocoder(session=session)


def feature(kind, method="Rooftop", confidence="High", codes=("Good",)):
    return {"properties": {
        "type": kind, "confidence": confidence, "matchCodes": list(codes),
        "geocodePoints": [{"geometry": {"coordinates": [1.0, 2.0]}, "calculationMethod": method, "usageTypes": ["Display"]}],
    }}


@pytest.mark.parametrize(
    ("feature_", "precision"),
    [
        (feature("Address", "Rooftop"), ROOFTOP),
        (feature("Address", "Parcel", confidence="Medium"), ROOFTOP),
        (feature("Address", "Interpolation"), STREET),
        (feature("RoadBlock", "Interpolation", confidence="Medium"), STREET),
        (feature("RoadIntersection", "Interpolation"), STREET),
        (feature("Postcode1"), ""),
        (feature("PopulatedPlace"), ""),
        (feature("Address", codes=("Good", "UpHierarchy")), ""),
        (feature("Address", codes=("Ambiguous",)), ""),
        (feature("Address", confidence="Low"), ""),
    ],
)
def test_azure_maps_precision(feature_, precision):
    found = azure_match(feature_)
    assert (found.latitude, found.longitude, found.precision) == (2.0, 1.0, precision)


def test_nominatim_identifies_the_application_and_waits_a_second_between_requests():
    session = FakeSession([{"lat": "48.44", "lon": "-89.37", "category": "place", "type": "house"}])
    now = [100.0]
    slept = []
    geocoder = NominatimGeocoder(
        session=session, clock=lambda: now[0], sleep=lambda seconds: slept.append(seconds),
    )
    assert geocoder.geocode("1 Tip Road, Thunder Bay", "CA").precision == ROOFTOP
    now[0] += 0.25
    geocoder.geocode("2 Tip Road, Thunder Bay", "CA")
    assert slept == [pytest.approx(0.75)]
    call = session.calls[0]
    assert call["url"] == "https://nominatim.openstreetmap.org/search"
    assert call["params"] == {"q": "1 Tip Road, Thunder Bay", "format": "jsonv2", "addressdetails": 1, "limit": 1, "countrycodes": "ca"}
    assert call["headers"] == {"User-Agent": DEFAULT_NOMINATIM_USER_AGENT} == {"User-Agent": "waste-ai-search (RMI)"}


def test_nominatims_url_and_user_agent_come_from_configuration(monkeypatch):
    monkeypatch.setenv("WASTE_AI_SEARCH_GEOCODER", "nominatim")
    monkeypatch.setenv("NOMINATIM_URL", "https://nominatim.internal.example/")
    monkeypatch.setenv("NOMINATIM_USER_AGENT", "waste-ai-search live test (RMI)")
    geocoder = make_geocoder()
    assert isinstance(geocoder, NominatimGeocoder)
    assert (geocoder.url, geocoder.user_agent) == ("https://nominatim.internal.example", "waste-ai-search live test (RMI)")


@pytest.mark.parametrize(
    ("result", "precision"),
    [
        ({"category": "place", "type": "house"}, ROOFTOP),
        ({"category": "place", "type": "house", "osm_type": "node"}, ROOFTOP),
        # A house number interpolated along its street sits on the interpolation line, a way.
        ({"category": "place", "type": "house", "osm_type": "way"}, STREET),
        ({"category": "building", "type": "yes"}, ROOFTOP),
        ({"category": "building", "type": "yes", "osm_type": "way"}, ROOFTOP),
        ({"category": "highway", "type": "residential"}, STREET),
        ({"category": "highway", "type": "tertiary"}, STREET),
        ({"category": "highway", "type": "unclassified"}, STREET),
        # A major road runs for kilometres, and any stretch of it could be the one met.
        ({"category": "highway", "type": "primary"}, MAJOR_ROAD),
        ({"category": "highway", "type": "secondary"}, MAJOR_ROAD),
        ({"category": "highway", "type": "trunk_link"}, MAJOR_ROAD),
        ({"category": "highway", "type": "motorway"}, MAJOR_ROAD),
        # A mapped landfill that carries the address would be OSM's own point for the site.
        ({"category": "landuse", "type": "landfill"}, ""),
        ({"category": "boundary", "type": "administrative"}, ""),
        ({"category": "place", "type": "postcode"}, ""),
    ],
)
def test_nominatim_precision(result, precision):
    assert nominatim_match({"lat": "1", "lon": "2", **result}).precision == precision


def test_no_geocoder_is_the_default(monkeypatch):
    monkeypatch.setenv("WASTE_AI_SEARCH_GEOCODER", "")
    assert make_geocoder() is None
    assert make_geocoder("none") is None
    with pytest.raises(ValueError):
        make_geocoder("bing")


def test_country_codes():
    assert (iso2("GBR"), iso2("irl"), iso2("NZL"), iso2("XXX")) == ("GB", "IE", "NZ", "")


def test_in_prose_the_first_precise_pair_is_the_location():
    located = parse_coordinates("The site is 1.5 km north of town, at 48.8434, -87.4296.")
    assert (located.latitude, located.longitude, located.basis) == (48.8434, -87.4296, COORDINATES)


def test_a_leading_name_is_taken_off_whatever_its_case():
    geocoder = FakeGeocoder()
    stated = source(address="BRADY ROAD LANDFILL - 1777 Brady Road, Winnipeg", facility_name_english="Brady Road Landfill")
    locate_source(stated, SITE, {}, geocoder, live=True)
    assert geocoder.calls == [("1777 Brady Road, Winnipeg", "CA")]


def test_saving_leaves_no_temporary_file(tmp_path):
    path = tmp_path / "source_locations.json"
    save_locations(path, {"CAN|x": {"basis": "none"}})
    assert [p.name for p in tmp_path.iterdir()] == ["source_locations.json"]


# --- what reviewers found the parsers misread --------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "Lat/Long: 45.4215, -75.6972",
        "Lat/Lon: 45.4215, -75.6972",
        "Latitude/Longitude: 45.4215, -75.6972",
        "Lat, Long: 45.4215, -75.6972",
        "Coordinates (lat, long): 45.4215, -75.6972",
        "GPS (latitude, longitude): 45.4215, -75.6972",
        "Long/Lat: -75.6972, 45.4215",
        "POINT(-75.6972 45.4215)",  # well-known text, longitude first
        # A label after its number belongs to that number, not the next one.
        "45.4215 lat, -75.6972 long",
    ],
)
def test_a_header_naming_both_axes_reads_them_in_its_order(text):
    located = parse_coordinates(text, strict=True)
    assert (located.latitude, located.longitude, located.basis) == (45.4215, -75.6972, COORDINATES)


def test_a_header_naming_both_axes_keeps_a_southern_point():
    located = parse_coordinates("Lat/Long: -37.8136, 144.9631")
    assert (located.latitude, located.longitude) == (-37.8136, 144.9631)


def test_hemispheres_written_as_words():
    located = parse_coordinates("Latitude 49.1234 North, Longitude 97.1234 West", strict=True)
    assert (located.latitude, located.longitude, located.basis) == (49.1234, -97.1234, COORDINATES)
    near(parse_coordinates("51°30'26\" North, 0°7'39\" West"), 51.507222, -0.1275, metres=1)
    assert parse_coordinates("33.8688 south, 151.2093 east").point == (-33.8688, 151.2093)


CHICOLOAPAN = {"site_id": "3933", "country_iso3": "MEX", "latitude": "19.40162383", "longitude": "-98.84103484"}


@pytest.mark.parametrize(
    "text",
    [
        # As a Mexican news page wrote it, in test10-mex-sample2: read as east, 15,289 km away.
        "19° 24’ 14.7” N de latitud y 98° 50’ 42.8” O de longitud",
        "N 19° 24’ 14.7” O 98° 50’ 42.8”",
        "19.404083 N, 98.845222 O",
        "19.404083 norte, 98.845222 oeste",
    ],
)
def test_west_written_o_is_west(text):
    """Spanish, Portuguese, French and Italian write west as O (oeste, ouest, ovest)."""
    near(parse_point(text, "MEX"), 19.404083, -98.845222, metres=1)
    located = locate_source(source(text), CHICOLOAPAN)
    assert located.basis == COORDINATES
    assert haversine_km(*located.point, 19.40162383, -98.84103484) < 1


def test_o_is_east_where_german_or_dutch_is_written():
    """In German and Dutch O is east (Ost, oost), as German Wikipedia writes Bochum."""
    assert parse_point("51° 28′ 54″ N, 7° 12′ 57″ O", "DEU").longitude > 0
    assert parse_point("51.9244 N, 4.4777 O", "NLD").longitude > 0
    # A page read for itself, with no country, is read as Spanish and the rest.
    assert parse_coordinates("51° 28′ 54″ N, 7° 12′ 57″ O").longitude < 0


@pytest.mark.parametrize(
    ("text", "iso3", "point"),
    [
        ("19°24'14.7\" latitud norte y 98°50'42.8\" longitud oeste", "MEX", (19.404083, -98.845222)),
        ("Latitud Norte: 19° 24' 14.7''; Longitud Oeste: 98° 50' 42.8''", "MEX", (19.404083, -98.845222)),
        ("Longitude West: 97.1234, Latitude 49.1234", "CAN", (49.1234, -97.1234)),
        ("33.4514 latitud sur, 70.6542 longitud oeste", "CHL", (-33.4514, -70.6542)),
        ("Latitude sul: 23.5505, Longitude oeste: 46.6333", "BRA", (-23.5505, -46.6333)),
        # A number's own sign is kept.
        ("Longitud oeste: -98.8452, latitud norte: 19.4041", "MEX", (19.4041, -98.8452)),
    ],
)
def test_a_hemisphere_written_with_its_axis_counts(text, iso3, point):
    near(parse_point(text, iso3), *point, metres=1)


@pytest.mark.parametrize(
    ("text", "iso3", "point"),
    [
        ("Le site est situé sur la route, 45.7640, 4.8357", "FRA", (45.764, 4.8357)),
        ("El relleno está al sur de la ciudad, 19.4041, -98.8452", "MEX", (19.4041, -98.8452)),
    ],
)
def test_a_word_that_only_sometimes_means_south_is_not_read_as_south(text, iso3, point):
    """"sur" is south in Spanish but "on" in French, so it counts only after a latitude's name."""
    assert parse_point(text, iso3).point == point


@pytest.mark.parametrize(
    "text",
    [
        "Site area 12.345 ha at 48.8434, -87.4296",
        "located 2.345 km from 48.8434, -87.4296",
        "Elevation 312.456 m; 48.8434, -87.4296",
        # No unit, but a pair out of range is passed over for the next one.
        "Elevation 312.456; 48.8434, -87.4296",
    ],
)
def test_a_distance_area_or_height_is_not_part_of_the_coordinates(text):
    located = parse_coordinates(text)
    assert (located.latitude, located.longitude, located.basis) == (48.8434, -87.4296, COORDINATES)


@pytest.mark.parametrize(
    "text",
    ["E452300 N345600", "E 452300, N 345600", "Easting 452300, Northing 345600", "X 452300 Y 345600", "NGR E 452300 N 345600"],
)
def test_a_labelled_easting_and_northing_in_the_uk_is_the_national_grid(text):
    # The same point as SK 52300 45600; read as the Irish Grid's square E it landed in Scotland.
    expected = parse_grid_reference("SK 52300 45600", "GBR")
    located = parse_grid_reference(text, "GBR")
    assert located.basis == GRID_REFERENCE
    near(located, *expected.point, metres=1)
    assert parse_point(text, "GBR", strict=True).basis == GRID_REFERENCE


def test_a_labelled_easting_and_northing_in_ireland_is_the_irish_grid_or_itm():
    # The Spire, Dublin: Irish Grid metres, and ITM metres, which never overlap.
    near(parse_grid_reference("E 315904 N 234671", "IRL"), 53.349798, -6.260254, metres=5)
    near(parse_grid_reference("E 715830, N 734697", "IRL"), 53.349798, -6.260254, metres=5)
    near(parse_grid_reference("E 278500 N 184300", "IRL"), 52.9042, -6.8341, metres=50)
    # Northern Ireland's sources give the Irish Grid, and say so.
    near(parse_grid_reference("Irish Grid E 315904 N 234671", "GBR"), 53.349798, -6.260254, metres=5)


def test_labelled_metres_with_no_known_grid_are_not_read():
    for iso3 in ("", "FRA"):
        assert parse_grid_reference("E 452300 N 345600", iso3) is None


def test_a_licence_number_before_a_grid_reference_does_not_hide_it():
    located = parse_grid_reference("Licence W0123, grid ref T 285 935", "IRL")
    assert located.basis == GRID_REFERENCE
    near(located, *parse_grid_reference("T 285 935", "IRL").point, metres=1)


def test_coarse_coordinates_give_way_only_to_an_address_that_agrees_with_them():
    site = {"site_id": "1", "country_iso3": "CAN", "latitude": "45.0", "longitude": "-75.0"}
    geocoder = FakeGeocoder({
        "12 Main St, Town": GeocodeMatch(45.3, -75.0, ROOFTOP, ""),      # an office, 32 km away
        "1 Gate Road, Town": GeocodeMatch(45.0325, -75.01, ROOFTOP, ""),  # 2.5 km: within 2 km and the hint's spread
    })
    far = locate_source(source("45.01, -75.01", "12 Main St, Town"), site, {}, geocoder, live=True)
    assert (far.basis, far.reason) == (NONE, "doubtful: its address lands 32.3 km from its coordinates")
    close = locate_source(source("45.01, -75.01", "1 Gate Road, Town"), site, {}, geocoder, live=True)
    assert (close.basis, close.value()) == (ROOFTOP, "45.0325, -75.01")


def test_a_po_box_is_taken_out_and_the_street_address_left_is_sent():
    geocoder = FakeGeocoder()
    for stated in (
        "29786 Rte. 2 PO Box 253 Wellington PE C0B 1T0 Canada",  # batch 1, East Prince
        "Box 219, 1350 Aster Street, Pemberton BC",               # batch 1, Pemberton
    ):
        locate_source(source(address=stated), SITE, {}, geocoder, live=True)
    assert [address for address, _country in geocoder.calls] == [
        "29786 Rte. 2, Wellington PE C0B 1T0 Canada",
        "1350 Aster Street, Pemberton BC",
    ]


@pytest.mark.parametrize(
    "stated",
    [
        "C.P. 123, Sainte-Sophie, QC",
        "Case postale 123, Sainte-Sophie",
        "Box 10, Hearst, ON P0L 1N0",
        "Private Mail Bag 12, Darwin",
        "PMB 12, Darwin",
        "BP 45, Abidjan",
        "GPO Box 12, Sydney NSW 2001",
        "Postbox 12, Oslo",
    ],
)
def test_a_po_box_with_only_a_town_left_is_not_sent(stated):
    geocoder = FakeGeocoder()
    located = locate_source(source(address=stated), SITE, {}, geocoder, live=True)
    assert (located.reason, geocoder.calls) == ("a PO box is not where the site is", [])


def test_a_street_address_after_a_mailing_address_is_still_located():
    geocoder = FakeGeocoder({"5405 Mapleward Road, Thunder Bay": GeocodeMatch(48.44, -89.37, ROOFTOP, "")})
    # Two addresses, as an older response's two street-address parts are read.
    stated = source(address="Mail: PO Box 100 | 5405 Mapleward Road, Thunder Bay")
    assert locate_source(stated, SITE, {}, geocoder, live=True).basis == ROOFTOP
    assert geocoder.calls == [("5405 Mapleward Road, Thunder Bay", "CA")]


def test_our_own_names_are_never_geocoded_either():
    geocoder = FakeGeocoder()
    named = {**SITE, "site_name": "Thunder Bay Solid Waste and Recycling Facility", "original_site_name": "Décharge de Thunder Bay"}
    for stated in ("Thunder Bay Solid Waste and Recycling Facility", "Décharge de Thunder Bay", "Thunder Bay Solid Waste & Recycling Facility"):
        located = locate_source(source(address=stated), named, {}, geocoder, live=True)
        assert "facility's name" in located.reason
    # A name the source gives as a found_facility_name value counts too.
    valued = source(
        address="Mapleward Landfill, 5405 Mapleward Road, Thunder Bay",
        values=[{"attribute_name": "found_facility_name", "value": "Mapleward Landfill"}],
    )
    locate_source(valued, SITE, {}, geocoder, live=True)
    assert geocoder.calls == [("5405 Mapleward Road, Thunder Bay", "CA")]


def test_a_leading_name_spelled_with_an_ampersand_is_taken_off():
    assert address_to_send(
        "Thunder Bay Solid Waste & Recycling Facility, 5405 Mapleward Road",
        ["Thunder Bay Solid Waste and Recycling Facility"],
    ) == "5405 Mapleward Road"


def test_a_name_is_taken_off_only_where_it_ends_at_a_separator():
    assert address_to_send("Hartland Avenue 1, Victoria", ["Hart"]) == "Hartland Avenue 1, Victoria"
    assert address_to_send("Eastview Road 18499, Guelph", ["Eastview"]) == "Eastview Road 18499, Guelph"
    assert address_to_send("Brady Road Landfill 1777 Brady Road", ["Brady Road Landfill"]) == "1777 Brady Road"


# --- a facility's name written as an address (build step 5) --------------------------------------
@pytest.mark.parametrize(
    "stated",
    [
        "Mapleward Road Landfill",
        "Highway 16 Transfer Station",
        "Croydon Rubbish Tip",
        "Main Street Recycling Centre",
        "Lot 5, Concession 3, Smith Waste Disposal Site",
        "Old Mill Road Dump.",
        # As sources usually write it: the name, then the town and province.
        "Mapleward Road Landfill, Thunder Bay, ON",
        "Main Street Recycling Centre, Brandon MB R7A 1A1",
        "Highway 16 Transfer Station; Vermilion, Alberta",
    ],
)
def test_an_address_with_no_civic_number_ending_in_a_facility_word_is_a_name(stated):
    """Agreed for step 5: a geocoder would find such text by name in OpenStreetMap, or place it on
    the road it names, so it is never sent."""
    geocoder = FakeGeocoder()
    located = locate_source(source(address=stated), SITE, {}, geocoder, live=True)
    assert (located.reason, geocoder.calls) == ("the stated location is the facility's name, which is never geocoded", [])


@pytest.mark.parametrize(
    "stated",
    [
        "5405 Mapleward Road Landfill, Thunder Bay",  # a civic number: an address after all
        "Unit 3, 12 Tip Lane",                   # a facility word that is not at the end
        "1777 Brady Road, Winnipeg MB",
        "Mapleward Road, Thunder Bay",
        "Brady Road Landfill, 1777 Brady Road, Winnipeg MB",  # a civic number after the name
    ],
)
def test_an_address_with_a_civic_number_or_no_facility_word_at_its_end_is_sent(stated):
    geocoder = FakeGeocoder()
    locate_source(source(address=stated), SITE, {}, geocoder, live=True)
    assert len(geocoder.calls) == 1


# --- an address goes with the source's own town and state (#21) ------------------------------------
OJO_SECO = {"site_id": "4853", "site_name": "Ojo Seco Municipal Open-Air Dump", "country_iso3": "MEX",
            "latitude": "18.91608333", "longitude": "-99.06905556"}
# The run's state names (state_names.json), as Nominatim writes them.
STATES = {
    cache_key(text, "MEX"): {"text": text, "country": "MEX", "name": name, "geocoder": "nominatim"}
    for text, name in (("Morelos", "Morelos"), ("Mor.", "Morelos"), ("Edo. Méx.", "Estado de México"))
}


def test_an_address_is_sent_with_the_town_and_state_the_source_gives():
    """Site 4853 (Ojo Seco, Yautepec, Morelos): Terra and Sol gave "colonia El Zarco", in Yautepec,
    Morelos. Sent bare, Nominatim matched a street of that name in Estado de México, 66.55 km away,
    and four pages the judges called ours were flagged more than 5 km from our site."""
    zarco = source(address="colonia El Zarco", stated_municipality="Yautepec", stated_state="Mor.")
    geocoder = FakeGeocoder({
        "colonia El Zarco": GeocodeMatch(19.5128796, -99.0219246, STREET, "highway=residential on way, rank 26", "Estado de México"),
        "colonia El Zarco, Yautepec, Morelos": GeocodeMatch(18.9046, -99.0612, STREET, "highway=residential on way, rank 26", "Morelos"),
    })
    cache = {}
    located = locate_source(zarco, OJO_SECO, cache, geocoder, live=True, states=STATES)
    # The state as the run's state names write it, not as the source abbreviates it.
    assert geocoder.calls == [("colonia El Zarco, Yautepec, Morelos", "MX")]
    assert located.basis == STREET and haversine_km(*located.point, 18.91608333, -99.06905556) < 5
    assert cache[cache_key("colonia El Zarco, Yautepec, Morelos", "MEX")]["state"] == "Morelos"


@pytest.mark.parametrize(("stated", "town", "state", "sent"), [
    ("Av. Juárez 100, CUERNAVACA, Morelos", "Cuernavaca", "Morelos", "Av. Juárez 100, CUERNAVACA, Morelos"),
    ("Camino Real 5, Querétaro", "Queretaro", "Querétaro", "Camino Real 5, Querétaro"),
    ("Libramiento Norte 3, Yautepec de Zaragoza", "Yautepec", "Morelos", "Libramiento Norte 3, Yautepec de Zaragoza, Morelos"),
    # A street named after the state doesn't name the state.
    ("Calle Morelos 3, Cuautla", "Cuautla", "Morelos", "Calle Morelos 3, Cuautla, Morelos"),
    ("Yautepec, Morelos", "Yautepec", "Morelos", "Yautepec, Morelos"),
])
def test_a_town_or_state_the_address_already_names_isnt_added_again(stated, town, state, sent):
    assert with_place(stated, town, state) == sent
    assert with_place("", town, state) == ""


def test_an_address_of_one_part_with_no_town_or_state_isnt_sent():
    """Round 2's other bare texts: "San Sebastián" matched a mountain, and "carretera Las Lumbreras"
    landed 0.63 km from the site only by luck."""
    geocoder = FakeGeocoder()
    for stated in ("San Sebastián", "carretera Las Lumbreras"):
        located = locate_source(source(address=stated), OJO_SECO, {}, geocoder, live=True, states=STATES)
        assert (located.basis, located.reason) == (NONE, NO_PLACE_REASON)
    assert geocoder.calls == []
    # Its plainer form, where it has a place after all, still is.
    locate_source(source(address="765 33rd Street East in Brandon"), SITE, {}, geocoder, live=True)
    assert geocoder.calls == [("765 33rd Street East, Brandon", "CA")]


def test_a_match_in_another_state_than_the_sources_isnt_used():
    """Nominatim's address details give the state each match is in, at no extra request."""
    cache = {}
    elsewhere = GeocodeMatch(19.5129, -99.0219, STREET, "highway=residential on way, rank 26", "Estado de México")
    geocoder = FakeGeocoder({"colonia El Zarco, Yautepec, Morelos": elsewhere})
    zarco = source(address="colonia El Zarco", stated_municipality="Yautepec", stated_state="Morelos")
    located = locate_source(zarco, OJO_SECO, cache, geocoder, live=True, states=STATES)
    assert (located.basis, located.reason) == (NONE, "the geocoder put it in Estado de México, not Morelos as the source says, so not used")
    # Arbitration reads the same answer back and refuses it the same way.
    assert locate_source(zarco, OJO_SECO, cache, states=STATES).reason == located.reason
    # An answer kept before matches had a state isn't refused, nor is one where the source's state
    # isn't known as the geocoder names it.
    old = {k: {key: value for key, value in entry.items() if key != "state"} for k, entry in cache.items()}
    assert locate_source(zarco, OJO_SECO, old, states=STATES).basis == STREET
    unknown = source(address="colonia El Zarco, Yautepec", stated_state="Morelia")
    geocoder = FakeGeocoder({"colonia El Zarco, Yautepec, Morelia": elsewhere})
    assert locate_source(unknown, OJO_SECO, {}, geocoder, live=True, states=STATES).basis == STREET


def test_a_plainer_form_gets_the_same_town_and_state_and_a_kilometre_marker_still_isnt_sent():
    geocoder = FakeGeocoder({"Calle Libramiento 12, Yautepec, Morelos": GeocodeMatch(18.90, -99.06, STREET, "", "Morelos")})
    plainer = source(address="Calle Libramiento 12 at the end of the road", stated_municipality="Yautepec", stated_state="Morelos")
    assert locate_source(plainer, OJO_SECO, {}, geocoder, live=True, states=STATES).basis == STREET
    assert [address for address, _country in geocoder.calls] == [
        "Calle Libramiento 12 at the end of the road, Yautepec, Morelos", "Calle Libramiento 12, Yautepec, Morelos",
    ]
    marker = source(address="Carretera Yautepec-Tlayacapan km 4", stated_municipality="Yautepec", stated_state="Morelos")
    assert locate_source(marker, OJO_SECO, {}, geocoder, live=True, states=STATES).reason == ALONG_A_ROAD_REASON
    assert len(geocoder.calls) == 2


def test_a_plainer_form_placed_in_another_state_isnt_used_either():
    elsewhere = GeocodeMatch(19.51, -99.02, STREET, "", "Estado de México")
    geocoder = FakeGeocoder({"Calle Libramiento 12, Yautepec, Morelos": elsewhere})
    plainer = source(address="Calle Libramiento 12 at the end of the road", stated_municipality="Yautepec", stated_state="Morelos")
    located = locate_source(plainer, OJO_SECO, {}, geocoder, live=True, states=STATES)
    assert len(geocoder.calls) == 2 and (located.basis, located.point) == (NONE, None)


def test_the_locate_step_and_arbitration_look_up_the_same_text():
    """Both read the run's state names, so the state the locate step sent is the one arbitration
    looks for. Without them, arbitration would look for "Edo. Méx." and find nothing."""
    toluca = {**OJO_SECO, "site_id": "1", "latitude": "19.29", "longitude": "-99.65"}
    stated = source(address="Ejido San Diego", stated_municipality="Toluca", stated_state="Edo. Méx.")
    geocoder = FakeGeocoder({"Ejido San Diego, Toluca, Estado de México": GeocodeMatch(19.30, -99.64, STREET, "", "Estado de México")})
    cache = {}
    live = locate_source(stated, toluca, cache, geocoder, live=True, states=STATES)
    assert list(cache) == [cache_key("Ejido San Diego, Toluca, Estado de México", "MEX")]
    assert locate_source(stated, toluca, cache, states=STATES) == live
    assert locate_source(stated, toluca, cache).reason == NOT_LOCATED


def test_a_nominatim_match_keeps_the_state_it_is_in():
    result = {"lat": "19.5128796", "lon": "-99.0219246", "category": "highway", "type": "residential", "osm_type": "way",
              "place_rank": 26, "address": {"road": "El Zarco", "county": "Ecatepec de Morelos", "state": "Estado de México"}}
    assert nominatim_match(result).state == "Estado de México"
    assert nominatim_match({**result, "address": {}}).state == ""
