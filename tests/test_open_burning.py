"""has_open_burning is asked of every site, at Carbon Mapper's request (10 November 2026)."""
from __future__ import annotations

from datetime import date

from waste_ai_search.arbitrate import extract_evidence
from waste_ai_search.prompt_builder import build_site_prompt, requested_attributes

SOURCE = {
    "url": "https://news.example/fire",
    "source_title": "Fire at the municipal dump",
    "publisher": "Local paper",
    "source_type": "News report",
    "publication_date": "2025-03-01",
}


def _mapped(value):
    claim = {
        "attribute_name": "has_open_burning", "value": value, "unit": "", "value_basis": "Direct",
        "confidence": "High", "sources": [SOURCE], "evidence_summary": "s", "quoted_evidence_short": "q",
    }
    _grouped, rows, _sources, _warnings = extract_evidence(
        {"site_id": "1"}, {"attributes": [claim]}, "run", "v", date(2026, 1, 1)
    )
    return [row["mapped_value"] for row in rows]


def test_it_is_asked_of_every_site_with_or_without_gas_collection():
    """No database column holds it, so the seed is always blank and every site is asked."""
    assert "has_open_burning" in requested_attributes({"site_id": "1", "has_landfill_gas_collection": "FALSE"})
    assert "has_open_burning" in requested_attributes({"site_id": "1", "has_landfill_gas_collection": "TRUE"})
    assert "has_open_burning" not in requested_attributes({"site_id": "1", "has_open_burning": "TRUE"})


def test_the_prompt_says_what_counts_as_burning():
    prompt = " ".join(build_site_prompt({"site_id": "1", "site_name": "Basurero"}, ["has_open_burning"]).split())
    assert "Whether waste is burned in the open at the site" in prompt
    assert "Yes where a source reports burning there, even one fire" in prompt
    assert "with the date of the burning, not the article's, in value_date" in prompt
    assert "a ban on burning is not a No" in prompt
    assert "Not an incinerator, a flare or a gas engine." in prompt


def test_it_reads_as_a_tristate_boolean():
    assert _mapped("Yes") == ["TRUE"]
    assert _mapped("No") == ["FALSE"]
    assert _mapped("Unknown") == [""]
