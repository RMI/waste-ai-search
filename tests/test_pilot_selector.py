from pathlib import Path

from waste_ai_search.input_loader import load_sites
from waste_ai_search.pilot_selector import is_generic_site_name, select_mixed_pilot


FIXTURE = Path(__file__).parent / "fixtures" / "osm_sites_sample.csv"


def test_mixed_pilot_is_deterministic_and_includes_priority_countries():
    sites, _headers = load_sites(FIXTURE)

    first = select_mixed_pilot(sites, size=5)
    second = select_mixed_pilot(sites, size=5)

    assert [row["site_id"] for row in first] == [row["site_id"] for row in second]
    assert len(first) == 5
    assert {"NGA", "PHL"}.issubset({row["country_iso3"] for row in first})
    assert any(row["pilot_selection_reason"].startswith("priority_country") for row in first)


def test_generic_name_detection_catches_hard_cases():
    assert is_generic_site_name("OpenStreetMap Landfill")
    assert is_generic_site_name("Municipal Garbage Dump")
    assert not is_generic_site_name("Olushosun Landfill")
