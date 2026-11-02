"""Facility names are compared as entity linkage compares them (build step 5 of the source-matching plan).

facility_names.py started as a copy of RMI/waste_data_ingestion_pipeline#225's name scoring, and is
this repository's own. It adds the generic words this corpus needs: without them batch 1's seven
"Rubbish Tip" sites each matched 68 others. The matcher that filed sources by it went in step 2 of the
approve-sources plan, with its tests (test_match.py); datasets_found.csv still tells a page's facility
names apart by their distinctive words. Operators' names are compared as names too, so two spellings
of one body's name don't raise a rival flag on Site_Review.
"""
from __future__ import annotations

import pytest

from waste_ai_search.facility_names import distinctive_words, name_similarity, operator_words, same_operator


def test_the_module_says_where_it_started():
    from pathlib import Path

    import waste_ai_search.facility_names as names

    header = Path(names.__file__).read_text(encoding="utf-8").splitlines()[0]
    assert "RMI/waste_data_ingestion_pipeline#225" in header


@pytest.mark.parametrize("name", [
    "Rubbish Tip", "Refuse Depot", "Waste Disposal Grounds", "Nuisance Grounds", "Lieu d'enfouissement technique",
    "LET", "Landfill Site", "OpenStreetMap Landfill", "Waste Management Facility",
    # A site's state: a council's list can call every row "Closed Landfill".
    "Closed Landfill", "Former Landfill Site", "Disused Tip",
])
def test_generic_names_have_no_distinctive_words_and_cant_be_judged(name):
    assert distinctive_words(name) == set()
    assert name_similarity(name, "Croydon Refuse Tip") is None


def test_the_added_words_leave_the_place_name():
    assert distinctive_words("Croydon Refuse Tip") == {"croydon"}
    assert distinctive_words("Minitonas Nuisance Grounds") == {"minitonas"}
    assert distinctive_words("Lieu d'enfouissement technique de Sainte-Sophie") == {"sainte", "sophie"}
    assert distinctive_words("LET de Lachute") == {"lachute"}
    assert distinctive_words("Wootton (Closed) Landfill") == {"wootton"}


@pytest.mark.parametrize("name, words", [
    ("Noosa Resource Recovery Centre", {"noosa"}),
    ("Nambour Landfill & Resource Recovery Centre", {"nambour"}),
    ("Prairie Green Integrated Waste Management Facility (Landfill)", {"prairie", "green"}),
    ("Prairie Green IWMF", {"prairie", "green"}),
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



# --- operators -----------------------------------------------------------------------------------
@pytest.mark.parametrize("name_a, name_b", [
    # Every rival flag on Sol's round-2 page rows was one of these (sites 68, 6674, 10239), and one of
    # Terra's (4040).
    ("Servicios, Operaciones y Suministros de la Isla (SOS)", "Servicios, Operaciones y Suministros (SOS)"),
    ("PRO-FAJ Hidro Limpieza, SA de CV", "PRO-FAJ Hidro Limpieza S.A. de C.V."),
    ("H. Ayuntamiento de Tixkokob 2024-2027", "Ayuntamiento de Tixkokob"),
    ("Confinamiento y Destrucciones Tepotzotlán", "Comercializadora Terrestre Tráfico, SA de CV y/o Confinamiento y Destrucciones Tepotzotlán"),
    # A name and its bracketed acronym (3035), and a legal form however it is spaced.
    ("Consultas Nacionales de Información y Asesorías SA de CV (Conadia)", "Conadia S.A. de C.V."),
    ("Pro-Faj Hidro Limpieza S. A. de C. V.", "PRO-FAJ HIDRO LIMPIEZA"),
    ("Honorable Ayuntamiento de Motul", "Ayuntamiento de Motul"),
])
def test_two_spellings_of_one_operators_name_are_one_operator(name_a, name_b):
    assert same_operator(name_a, name_b)
    assert same_operator(name_b, name_a)


@pytest.mark.parametrize("name_a, name_b", [
    # 7864: the municipality was inferred; Sol found the company that runs it.
    ("Red Ambiental", "Ayuntamiento de San Luis Potosí"),
    ("Ayuntamiento de Tixkokob", "Ayuntamiento de Motul"),
    ("Tecnosilicatos de México, S.A. de C.V.", "Conadia"),
    ("PASA", "Servicios, Operaciones y Suministros de la Isla (SOS)"),
])
def test_two_bodies_are_two_operators(name_a, name_b):
    assert not same_operator(name_a, name_b)
    assert not same_operator(name_b, name_a)


def test_an_operators_words_leave_out_small_words_legal_forms_titles_and_years():
    assert operator_words("H. Ayuntamiento de Tixkokob 2024-2027") == ({"ayuntamiento", "tixkokob"}, set())
    assert operator_words("Servicios, Operaciones y Suministros (S.O.S.)") == ({"servicios", "operaciones", "suministros"}, {"sos"})
    assert operator_words("Empresa S. de R.L. de C.V.") == operator_words("Empresa S.A.P.I. de C.V.") == ({"empresa"}, set())
