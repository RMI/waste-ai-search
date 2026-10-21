"""Add the values SMEs marked as a new facility to a run's new_facility_candidates.csv.

Arbitration lists the located sources that match none of our sites (match.py). An SME reviewing
the workbook can find another: a value filed under one of our sites whose source is about a
landfill we don't hold, rejected as "New facility - not in our database". The
collect-new-facilities command reads the reviewed workbook and appends those to the same list,
once each, and a later arbitration keeps them. Read-only on the workbook.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .input_loader import SEED_NAME, load_sites, write_csv_records
from .match import FOUND_BY_SME, NEW_FACILITY_FILE, NEW_FACILITY_HEADERS, Site, SiteIndex, candidate_id
from .name_yield import read_review_workbook
from .schema import LOCATION_ATTRIBUTE, NEW_FACILITY_REASON, normalize_scalar


def _reason(text: Any) -> str:
    """A rejection reason as typed: any case, and an en dash read as the hyphen the list uses."""
    return " ".join(normalize_scalar(text).replace("–", "-").replace("—", "-").casefold().split())


def reviewed_workbook(run_dir: Path, run_id: str, review_dir: Path | None) -> Path:
    """Where the SMEs' copy of a run's workbook is: the SharePoint folder's, else the run folder's."""
    name = f"{run_id}_review.xlsx"
    if review_dir is not None and (review_dir / run_id / name).is_file():
        return review_dir / run_id / name
    return run_dir / name


def sme_candidates(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """One new_facility_candidates.csv row per source an SME marked as a new facility, however many
    of its values they rejected for it."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if _reason(row.get("rejection_reason")) != _reason(NEW_FACILITY_REASON):
            continue
        site_id = normalize_scalar(row.get("site_id"))
        url = normalize_scalar(row.get("winning_source_url"))
        name = normalize_scalar(row.get("facility_name_in_source"))
        key = candidate_id(FOUND_BY_SME, site_id, url, name)
        latitude = longitude = ""
        if normalize_scalar(row.get("attribute_name")) == LOCATION_ATTRIBUTE:
            # The SME's own location if they gave one, else the one the source states.
            parts = (normalize_scalar(row.get("corrected_value")) or normalize_scalar(row.get("resolved_value"))).split(",")
            if len(parts) == 2:
                latitude, longitude = (part.strip() for part in parts)
        candidate = out.setdefault(key, {
            "candidate_id": key,
            "found_by": FOUND_BY_SME,
            "country_iso3": normalize_scalar(row.get("country_iso3")),
            "source_urls": url,
            "facility_names_in_source": name,
            "facility_names_english": "",
            "stated_locations": normalize_scalar(row.get("stated_location")),
            "latitude": "",
            "longitude": "",
            "location_basis": "",
            "nearest_site_id": "",
            "nearest_site_name": "",
            "nearest_site_km": "",
            "searched_site_ids": normalize_scalar(row.get("searched_site_id")) or site_id,
            "_attributes": [],
            "_site": f"{site_id} ({normalize_scalar(row.get('site_name'))})",
            "_by": " on ".join(
                part for part in (normalize_scalar(row.get("reviewer")), normalize_scalar(row.get("reviewed_date"))) if part
            ),
            "_notes": [],
        })
        if latitude and not candidate["latitude"]:
            candidate["latitude"], candidate["longitude"] = latitude, longitude
            candidate["location_basis"] = normalize_scalar(row.get("location_basis")) or "SME"
        candidate["_attributes"].append(normalize_scalar(row.get("attribute_name")))
        note = normalize_scalar(row.get("researcher_notes"))
        if note and note not in candidate["_notes"]:
            candidate["_notes"].append(note)

    rows_out = []
    for candidate in out.values():
        note = (
            f"Marked a new facility by an SME{' (' + candidate['_by'] + ')' if candidate['_by'] else ''} on "
            f"{', '.join(dict.fromkeys(candidate['_attributes']))}, filed under site {candidate['_site']}."
        )
        if candidate["_notes"]:
            note += " " + " ".join(candidate["_notes"])
        rows_out.append({
            **{key: value for key, value in candidate.items() if not key.startswith("_")},
            "note": note,
        })
    return rows_out


def add_nearest_sites(candidates: list[dict[str, Any]], seed: Path) -> None:
    """Our site nearest each candidate that has a point, from the run's seed, as arbitration lists it."""
    located = [row for row in candidates if row["latitude"] and row["longitude"]]
    if not located or not seed.exists():
        return
    sites, _headers = load_sites(seed)
    index = SiteIndex([Site.from_seed(site) for site in sites])
    for row in located:
        try:
            point = (float(row["latitude"]), float(row["longitude"]))
        except ValueError:
            continue
        nearest = index.nearest(point)
        if nearest is not None:
            km, site = nearest
            row["nearest_site_id"], row["nearest_site_name"], row["nearest_site_km"] = site.site_id, site.name, f"{km:.1f}"


def collect_new_facilities(run_dir: Path, workbook: Path) -> int:
    """Append the workbook's new-facility rejections to the run's list. Returns how many were new."""
    rows = read_review_workbook(workbook)
    if rows is None:
        raise FileNotFoundError(f"No reviewed workbook with a Review_Queue tab at {workbook}.")
    path = run_dir / NEW_FACILITY_FILE
    existing: list[dict[str, str]] = []
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as handle:
            existing = [dict(row) for row in csv.DictReader(handle)]
    listed = {row.get("candidate_id") for row in existing}
    added = [row for row in sme_candidates(rows) if row["candidate_id"] not in listed]
    add_nearest_sites(added, run_dir / SEED_NAME)
    write_csv_records(path, [*existing, *added], NEW_FACILITY_HEADERS)
    print(
        f"{len(added)} new-facility candidate(s) added from {workbook.name} to {path} "
        f"({len(existing) + len(added)} listed in all)."
    )
    return len(added)
