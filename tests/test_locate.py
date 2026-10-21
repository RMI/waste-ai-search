"""Locate: each source's stated location becomes a point (build step 4 of the source-matching plan).

Coordinates and grid references are read locally. Addresses go to a geocoder, which only the
locate step calls, and every answer is cached by text and country. Every geocoder here is a fake:
tests/conftest.py stops any real request.
"""
from __future__ import annotations

import pytest

from waste_ai_search.locate import (
    AZURE_MAPS_API_VERSION,
    COORDINATES,
    DEFAULT_NOMINATIM_USER_AGENT,
    GRID_REFERENCE,
    HINT,
    NO_GEOCODER,
    NONE,
    NOT_LOCATED,
    ROOFTOP,
    STREET,
    AzureMapsGeocoder,
    GeocodeMatch,
    GeocoderError,
    Located,
    NominatimGeocoder,
    azure_match,
    cache_key,
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
    save_locations,
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


def source(stated, kind=COORDINATES, url="https://data.ontario.ca/landfills.csv", **extra):
    return {"url": url, "stated_location": stated, "stated_location_kind": kind, **extra}


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
        assert locate_source(source(stated, "none"), SITE).reason == "no location stated"


def test_coordinates_equal_to_the_prompts_count_as_copied():
    located = locate_source(source("48.84, -87.43"), SITE)
    assert (located.basis, located.reason) == (NONE, "copied from the prompt")


def test_coordinates_within_10_m_of_ours_count_as_copied():
    # Batch 1: Ontario's registry credited with our own OSM centroid, to every digit.
    copied = locate_source(source("48.843368376131856, -87.42962607259884"), SITE)
    assert (copied.basis, copied.reason) == (NONE, "copied from the prompt")
    # About 50 m away is a real location.
    assert locate_source(source("48.8438, -87.4296"), SITE).basis == COORDINATES


def test_wastebooks_coordinates_are_never_a_location():
    located = locate_source(source("52.1234, -1.2345", url="https://www.wastebook.co.uk/site/123"), SITE)
    assert (located.basis, located.reason) == (NONE, "wastebook.co.uk's coordinates are postcode centroids")


def test_mislabelled_coordinates_still_count():
    assert locate_source(source("48.4410, -89.3690", kind="street address"), SITE).basis == COORDINATES
    assert locate_source(source("48.4410, -89.3690", kind=""), SITE).basis == COORDINATES


def test_a_place_description_is_not_located():
    located = locate_source(source("5 km east of Thunder Bay on Highway 61", kind="place description"), SITE)
    assert (located.basis, located.reason) == (NONE, "a place description is not located")


def test_without_a_geocoder_an_address_stays_unlocated_and_says_so():
    cache = {}
    stated = source("1777 Brady Road, Winnipeg, MB", kind="street address")
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
    rooftop = locate_source(source("1777 Brady Road, Winnipeg, MB", "street address"), SITE, {}, geocoder, live=True)
    assert (rooftop.basis, rooftop.value(), rooftop.geocoder) == (ROOFTOP, "49.7958, -97.1601", "fake")
    street = locate_source(source("Brady Road, Winnipeg, MB", "street address"), SITE, {}, geocoder, live=True)
    assert street.basis == STREET
    town = locate_source(source("Winnipeg, MB", "street address"), SITE, {}, geocoder, live=True)
    assert (town.basis, town.reason) == (NONE, "geocoded only coarser than a street, so not used")
    missing = locate_source(source("Nowhere Lane", "street address"), SITE, {}, geocoder, live=True)
    assert (missing.basis, missing.reason) == (NONE, "the geocoder found no match")
    assert {country for _address, country in geocoder.calls} == {"CA"}


def test_an_address_is_geocoded_once_per_country():
    geocoder = FakeGeocoder({"1 Tip Road, Hamilton": GeocodeMatch(-37.78, 175.28, ROOFTOP, "")})
    cache = {}
    nzl = {**SITE, "country_iso3": "NZL"}
    for _ in range(2):
        locate_source(source("1 Tip Road, Hamilton", "street address"), nzl, cache, geocoder, live=True)
    assert geocoder.calls == [("1 Tip Road, Hamilton", "NZ")]
    locate_source(source("1 Tip Road, Hamilton", "street address"), {**nzl, "country_iso3": "CAN"}, cache, geocoder, live=True)
    assert len(geocoder.calls) == 2


def test_an_address_cached_without_a_geocoder_is_tried_again_once_there_is_one():
    cache = {}
    stated = source("1 Tip Road", "street address")
    locate_source(stated, SITE, cache, None, live=True)
    geocoder = FakeGeocoder({"1 Tip Road": GeocodeMatch(48.44, -89.37, ROOFTOP, "")})
    assert locate_source(stated, SITE, cache, geocoder, live=True).basis == ROOFTOP


def test_a_failed_lookup_is_not_cached():
    cache = {}
    with pytest.raises(GeocoderError):
        locate_source(source("1 Tip Road", "street address"), SITE, cache, FakeGeocoder(error=TimeoutError("slow")), live=True)
    assert cache_key("1 Tip Road", "CAN") not in cache


def test_po_boxes_and_facility_names_are_never_sent():
    geocoder = FakeGeocoder()
    po_box = locate_source(source("PO Box 1234, Thunder Bay, ON", "street address"), SITE, {}, geocoder, live=True)
    assert po_box.reason == "a PO box is not where the site is"
    named = source(
        "Thunder Bay Solid Waste and Recycling Facility", "street address",
        facility_name_in_source="Thunder Bay Solid Waste and Recycling Facility",
    )
    assert "facility's name" in locate_source(named, SITE, {}, geocoder, live=True).reason
    assert geocoder.calls == []


def test_a_leading_facility_name_is_taken_off_the_address():
    geocoder = FakeGeocoder()
    stated = source(
        "Brady Road Landfill, 1777 Brady Road, Winnipeg", "street address",
        facility_name_in_source="Brady Road Landfill",
    )
    locate_source(stated, SITE, {}, geocoder, live=True)
    assert geocoder.calls == [("1777 Brady Road, Winnipeg", "CA")]


def test_coordinates_win_over_an_address_unless_it_lands_more_than_2_km_away():
    geocoder = FakeGeocoder({
        "Gate Road": GeocodeMatch(48.4500, -89.3700, ROOFTOP, ""),   # about 1 km away
        "Head Office": GeocodeMatch(48.3800, -89.2500, ROOFTOP, ""),  # about 11 km away
    })
    close = locate_source(source("48.4410, -89.3690 | Gate Road", "coordinates | street address"), SITE, {}, geocoder, live=True)
    assert (close.basis, close.value()) == (COORDINATES, "48.441, -89.369")
    far = locate_source(source("48.4410, -89.3690 | Head Office", "coordinates | street address"), SITE, {}, geocoder, live=True)
    assert far.basis == NONE and far.reason.startswith("doubtful: its address lands")


def test_a_located_address_beats_coarse_coordinates():
    geocoder = FakeGeocoder({"Gate Road": GeocodeMatch(48.4500, -89.3700, ROOFTOP, "")})
    located = locate_source(source("48.4, -89.4 | Gate Road", "coordinates | street address"), SITE, {}, geocoder, live=True)
    assert located.basis == ROOFTOP


def test_copied_coordinates_leave_the_address():
    geocoder = FakeGeocoder({"Gate Road": GeocodeMatch(48.4500, -89.3700, ROOFTOP, "")})
    stated = source("48.843368376131856, -87.42962607259884 | Gate Road", "coordinates | street address")
    assert locate_source(stated, SITE, {}, geocoder, live=True).basis == ROOFTOP


def test_the_locate_step_records_every_part_it_reads():
    cache = {}
    locate_source(source("SU 123 456", GRID_REFERENCE), {**SITE, "country_iso3": "GBR"}, cache, live=True)
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
        ({"category": "building", "type": "yes"}, ROOFTOP),
        ({"category": "highway", "type": "residential"}, STREET),
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
