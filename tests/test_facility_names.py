"""Facility names are compared as entity linkage compares them (build step 5 of the source-matching plan).

facility_names.py is CJ's, from RMI/waste_data_ingestion_pipeline#225, so both repositories mean the
same by "the same name". The one addition is the generic words this corpus needs, to be agreed with
CJ: without them batch 1's seven "Rubbish Tip" sites each matched 68 others. What makes a match
strong enough to file a source by is matching's, in test_match.py.
"""
from __future__ import annotations

import pytest

from waste_ai_search.facility_names import distinctive_words, name_similarity


def test_the_module_says_it_mirrors_entity_linkage():
    from pathlib import Path

    import waste_ai_search.facility_names as names

    header = Path(names.__file__).read_text(encoding="utf-8").splitlines()[0]
    assert "RMI/waste_data_ingestion_pipeline#225" in header


@pytest.mark.parametrize("name", [
    "Rubbish Tip", "Refuse Depot", "Waste Disposal Grounds", "Nuisance Grounds", "Lieu d'enfouissement technique",
    "LET", "Landfill Site", "OpenStreetMap Landfill", "Waste Management Facility",
])
def test_generic_names_have_no_distinctive_words_and_cant_be_judged(name):
    assert distinctive_words(name) == set()
    assert name_similarity(name, "Croydon Refuse Tip") is None


def test_the_added_words_leave_the_place_name():
    assert distinctive_words("Croydon Refuse Tip") == {"croydon"}
    assert distinctive_words("Minitonas Nuisance Grounds") == {"minitonas"}
    assert distinctive_words("Lieu d'enfouissement technique de Sainte-Sophie") == {"sainte", "sophie"}
    assert distinctive_words("LET de Lachute") == {"lachute"}


@pytest.mark.parametrize("name, words", [
    ("Noosa Resource Recovery Centre", {"noosa"}),
    ("Nambour Landfill & Resource Recovery Centre", {"nambour"}),
    ("Installation de stockage de déchets non dangereux de Hendaye", {"hendaye"}),
    ("Installation de Stockage des Déchets Non Dangereux (ISDND)", {"isdnd"}),
    ("Landfill for Non-Hazardous Waste, Gien", {"gien"}),
    ("Tiradero a cielo abierto del municipio de Ensenada", {"ensenada"}),
    ("Open Air Dump of Tecate", {"tecate"}),
    ("Dipòsit controlat de Tivissa", {"tivissa"}),
    ("Sanitary landfill in ditches of Itapipoca", {"itapipoca"}),
    ("Dépôt à neige du Ruisseau", {"neige", "ruisseau"}),
])
def test_the_words_of_other_kinds_of_site_are_generic_too(name, words):
    """Without them, a source with no location naming Noosa's Resource Recovery Centre moved 20 km
    to Nambour's, and 187 pairs of French sites matched each other strongly on 'Installation de
    Stockage de Déchets Non Dangereux' alone."""
    assert distinctive_words(name) == words


def test_case_accents_suffixes_and_permit_codes_are_ignored():
    assert distinctive_words("Viridor Waste Management Limited, Foxhall Landfill Site EPR/BW2943IG") == {
        "viridor", "foxhall",
    }
    assert name_similarity("Lixão de Pirambóia", "LIXAO DE PIRAMBOIA") == 1.0


def test_a_misspelling_still_scores_high_and_conflicting_qualifiers_score_nothing():
    assert name_similarity("Seaton Meadows Landfil", "Seaton Meadows") >= 0.8
    assert name_similarity("Eastview North Landfill", "Eastview South Landfill") == 0.0

