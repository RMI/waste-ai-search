"""F31: has_flare, flare_efficiency and bulk_waste_type are searched; three GCCS methane volumes
are not."""
from __future__ import annotations

from datetime import date

from waste_ai_search.arbitrate import extract_evidence
from waste_ai_search.prompt_builder import build_site_prompt, requested_attributes

SOURCE = {
    "url": "https://example.gov/report",
    "source_title": "Annual waste statistics",
    "publisher": "Waste Authority",
    "source_type": "Regulator",
    "publication_date": "2024-01-01",
}
UNSEARCHED = {
    "gccs_ch4_generated_metric_tonnes",
    "gccs_ch4_collected_metric_tonnes",
    "gccs_ch4_flow_to_project_metric_tonnes",
}


def _claim(name, value, unit=""):
    return {
        "attribute_name": name, "value": value, "unit": unit, "value_basis": "Direct",
        "confidence": "High", "sources": [SOURCE], "evidence_summary": "s", "quoted_evidence_short": "q",
    }


def _mapped(*claims):
    _grouped, rows, _sources, _warnings = extract_evidence(
        {"site_id": "1"}, {"attributes": list(claims)}, "run", "v", date(2026, 1, 1)
    )
    return [row["mapped_value"] for row in rows]


def test_bulk_waste_type_is_no_longer_asked_and_flares_only_with_gas_collection():
    """bulk_waste_type was retired on 30 October 2026: whether incoming waste is municipal is marked
    on the amount itself (not_municipal)."""
    no_gas = requested_attributes({"site_id": "1", "has_landfill_gas_collection": "FALSE"})
    gas = requested_attributes({"site_id": "1", "has_landfill_gas_collection": "TRUE"})
    assert "bulk_waste_type" not in no_gas and "bulk_waste_type" not in gas
    assert "has_flare" not in no_gas and "flare_efficiency" not in no_gas
    assert "has_flare" in gas and "flare_efficiency" in gas


def test_three_methane_volumes_are_no_longer_searched_or_prompted():
    site = {"site_id": "1", "site_name": "Deponie", "has_landfill_gas_collection": "TRUE"}
    attributes = requested_attributes(site)
    assert not UNSEARCHED & set(attributes)
    assert "gccs_ch4_flared_metric_tonnes" in attributes  # the rest of GCCS is still asked
    prompt = build_site_prompt(site, attributes)
    assert not [name for name in UNSEARCHED if name in prompt]
    assert "bulk_waste_type" not in prompt


def test_incoming_waste_counts_municipal_waste_only():
    """The standardized spec's rule: municipal waste only, as far as the source allows; where it
    doesn't say, the amount is taken as municipal (Hugh, 30 October)."""
    prompt = " ".join(build_site_prompt({"site_id": "1", "site_name": "Deponie"}).split())
    assert "Municipal solid waste only. Where the source splits the amount by kind of waste, return the municipal part." in prompt
    assert "Where it says the amount is mainly other waste" in prompt
    assert "return it with not_municipal = true. Where it doesn't say, return it as written." in prompt
    assert '"not_municipal": "true only for an incoming waste amount the source says is mainly other waste; otherwise false"' in prompt


def test_new_values_are_typed_and_checked():
    assert _mapped(_claim("has_flare", "Yes")) == ["TRUE"]
    assert _mapped(_claim("flare_efficiency", "98", "%")) == ["0.98"]
    assert _mapped(_claim("bulk_waste_type", "MSW")) == ["municipal solid waste"]
    assert _mapped(_claim("bulk_waste_type", "household waste")) == ["municipal solid waste"]
    assert _mapped(_claim("bulk_waste_type", "construction and demolition waste")) == ["inert waste"]
    assert _mapped(_claim("bulk_waste_type", "Industrial waste")) == ["others"]
    assert _mapped(_claim("bulk_waste_type", "landfill gas")) == [""]  # not a member: dropped
    assert _mapped(_claim("bulk_waste_type", "Unknown")) == [""]  # null, so it is asked again


def test_an_older_response_with_a_dropped_attribute_still_reads():
    """Re-arbitrating a run cached before F31 keeps its methane volumes."""
    assert _mapped(_claim("gccs_ch4_collected_metric_tonnes", "1200", "metric tonnes CH4")) == ["1200"]


def test_a_landfill_for_other_waste_is_a_non_municipal_waste_landfill():
    """The standardized spec's type for a site taking mainly waste other than municipal solid
    waste (waste_data_ingestion_pipeline #238). With bulk_waste_type retired, it is the one place
    a page saying "inert waste landfill" is recorded."""
    prompt = build_site_prompt({"site_id": "1", "site_name": "Deponie"}, ["facility_type"])
    assert "Non-Municipal Waste Landfill - a disposal site taking mainly waste other than municipal solid waste" in " ".join(prompt.split())
    assert "What it takes decides this, whatever its engineering." in prompt
    for claimed in ("Non-Municipal Waste Landfill", "inert landfill", "C&D landfill", "hazardous waste landfill"):
        assert _mapped(_claim("facility_type", claimed)) == ["Non-Municipal Waste Landfill"], claimed
