# Waste AI Search

AI-assisted waste site activity data discovery using Azure Foundry.

This repository is designed to take a post-consolidated facility waste activity dataset, enrich the search context with cached reverse-geocode fields when
available, call a Foundry-compatible search client, and write a review-ready
Excel workbook.


## Setup

```bash
uv --cache-dir .uv-cache sync
```

Run tests:

```bash
PYTHONPATH=tests/stubs:src uv --cache-dir .uv-cache run pytest -q
```

## Configure Azure Foundry

For live Foundry mode, create an Azure AI Foundry Agent with web search enabled,
then copy `.env.example` to `.env` or export the same variables:

```bash
cp .env.example .env
```

Required values:

```bash
AZURE_AI_PROJECT_ENDPOINT=
AZURE_AI_AGENT_NAME=
```

`AZURE_AI_AGENT_ID` is optional, but useful for older Azure AI Projects agent
APIs or if multiple agents share a name.

Authentication uses `DefaultAzureCredential`, so local development can use:

```bash
az login
```

For service principal or future Azure Container Apps Jobs, set:

```bash
AZURE_TENANT_ID=
AZURE_CLIENT_ID=
AZURE_CLIENT_SECRET=
```

Check the local environment without making a live Foundry call:

```bash
uv --cache-dir .uv-cache run scripts/check_environment.py
```

Default outputs:

```text
outputs/runs/<run_id>/seed.csv
outputs/runs/<run_id>/<run_id>_candidate.xlsx
outputs/runs/<run_id>/raw_foundry_responses/site_<site_id>.json
outputs/runs/<run_id>/status/site_<site_id>.json
```

## Seeding

`search` and `run` read `consolidation.consolidated_facility` directly. There is no seed file to
generate or keep current, and no `inputs/` folder — the database is the source of truth, and a
checked-in export is stale the moment consolidation moves.

```bash
uv run python -m waste_ai_search.cli run --iso3 MEX      # seeds from the database
uv run python -m waste_ai_search.cli run --input-csv seed.csv   # pinned corpus instead
```

`--iso3` is pushed into SQL, so a single-country run reads only that country rather than pulling
all 19,492 facilities to keep 83.

**Every database-seeded run snapshots what it read** to `outputs/runs/<run_id>/seed.csv`, and
records `seed_source` in the workbook's Run_Config tab. Without that a run would not be
reproducible: the corpus moves underneath you and nothing would say which version produced a
given output — which would undercut the `git_sha` stamped on every standardized row.

`--input-csv` remains for two cases: pinning an exact corpus, and the gas-collection follow-up,
which rewrites the seed between passes into the run directory.

A run therefore needs database access. Without the VPN, seed a file first with
`scripts/seed_metadata_search.py` and pass it with `--input-csv`.

## Run The Metadata Search

**One command does everything:**

```bash
uv run waste-ai-search run --run-id pilot10 --iso3 NGA PHL --limit 10
```

That searches, arbitrates, and — if any facility turns out to have a gas collection system the
seed did not know about — automatically runs a second, narrow pass for that facility's gas-capture
attributes and re-arbitrates. Same run id, same output directory, no reseeding by hand. Pass
`--no-followup` to stop after the first pass.

Why a second pass exists at all: gas-capture attributes are only asked of facilities known to have
a collection system, and `has_landfill_gas_collection` is itself one of the searched attributes. A
facility whose system is discovered mid-run therefore needs one more, cheap request. The follow-up
is cached separately (`site_<id>__gccs.json`) so it never overwrites the first pass, and
arbitration merges both.

**Or drive the phases yourself** — useful for long runs, because `arbitrate` is free and you can
run it as often as you like against a search that is still in progress:

```bash
# Phase 1 - query Foundry, cache one raw JSON response per site. Resumable.
uv run waste-ai-search search --run-id pilot10 --iso3 NGA PHL --limit 10

# ...or sample a spread instead of the first N. --limit follows seed order, which is
# facility-id order and clusters by country; --pilot-size draws across countries and
# deliberately includes generic-named hard cases.
uv run waste-ai-search search --run-id pilot10 --pilot-size 10

# Phase 2 - rebuild every output from the cached responses. Free, offline, re-runnable.
uv run waste-ai-search arbitrate --run-id pilot10

# Phase 3 - what `run` does for you: fold promoted values back into the seed and search again.
uv run waste-ai-search refresh-seed --run-id pilot10
uv run waste-ai-search search --run-id pilot10_pass2 \
  --input-csv outputs/runs/pilot10/refreshed_seed.csv
```

Re-running `search` skips sites that already have a cached response unless `--force` is passed, so
an interrupted long run resumes where it stopped. Because `arbitrate` reads the cache rather than
the network, a change to the credibility rubric can be re-applied to a completed run at no cost.

### What is actually searched — read this before a full run

Three rules narrow the search. Together they mean a full run queries **12,392 of 15,537
facilities** and makes **167,410 attribute-requests** instead of 22 x 15,537 = 341,814.

**1. United States facilities are excluded by default — 3,145 sites, 20% of the corpus.**

US landfills are already covered by `usa_ghgrp_2026` and `lmop_2024`, both Tier 1. An AI search
there mostly produces equal-tier conflicts for a human to adjudicate rather than new data. Override
in any of three ways — an explicit request always beats the default:

```bash
uv run waste-ai-search search --run-id r --include-excluded-countries   # search everything
uv run waste-ai-search search --run-id r --iso3 USA                     # naming it wins
uv run waste-ai-search search --run-id r --site-ids 499                 # listing ids wins
```

The run prints how many sites it dropped, so this is never silent.

**2. Brazil facilities are searched for coordinates only, and only where the location is flagged
inexact — which today means 0 of 303 sites.**

For a Brazilian facility, the search asks for `found_latitude` and `found_longitude` and nothing
else — **and only when `is_location_exact` is FALSE.** Every other Brazilian attribute comes from a
government source, so searching it risks overwriting better data than the search can find.

**Right now this rule matches nothing.** `is_location_exact` is TRUE for all 303 Brazilian
facilities (in fact for all 15,537 corpus-wide), so all 303 are skipped entirely and the run prints:

```
Skipped 303 site(s) with no attributes left to search.
```

The rule is a default now so that inexact-location facilities are scoped correctly the moment they
enter the consolidation — you should not have to remember to add it later. Unknown is **not**
treated as inexact: only an explicit FALSE triggers a coordinate search.

**3. Everywhere else: gap-fill, plus the gas-capture gate.**

- Identity (`found_site_name`, `found_latitude`, `found_longitude`) is always requested.
- Metadata attributes are requested **only where that facility's baseline is empty**.
- The seven `gccs_*` attributes are requested **only where gas collection is known present** —
  212 to 603 facilities each, not 13,000. See the two-pass note above for why this loses nothing.

Both country rules live in [`schema.py`](src/waste_ai_search/schema.py) as
`DEFAULT_EXCLUDED_ISO3` and `COORDINATES_ONLY_ISO3`, so changing policy is a one-line edit.

**Why these rules exist, in one line each:** the US is already Tier 1 covered by GHGRP and LMOP, so
searching it manufactures equal-tier conflicts rather than data; Brazil's non-coordinate attributes
are government-sourced and should not be overwritten by a web search.

A site left with no attributes to search is dropped from selection entirely — querying it would
spend a request and a prompt to receive nothing.

| Scope | Sites | Attribute-requests |
|---|---|---|
| Everything, no rules | 15,537 | 341,814 |
| Gap-fill + GCCS gate only | 15,537 | 197,110 |
| **+ country rules (default)** | **12,089** | **166,804** |

At the measured 42s/site that is roughly **6.3 days** serial, down from 8.0.

### Attributes searched

**22 attributes are requestable.** No site is asked for all 22 — the scoping rules above decide
which ones a given facility gets.

**Always requested — identity.** These are the only evidence that the agent researched the *right*
facility. They drive the distance check and the 5 km identity gate, and an auto-validated fill is
only trustworthy because identity was confirmed.

| Attribute | Type | Unit asked for | → standardized column |
|---|---|---|---|
| `found_site_name` | text | — | `facility_name` |
| `found_latitude` | numeric | `decimal degrees` | `latitude` |
| `found_longitude` | numeric | `decimal degrees` | `longitude` |

**Gap-fill — requested only where that facility's baseline value is empty.**

| Attribute | Type | Unit asked for | → standardized column |
|---|---|---|---|
| `facility_status` | enum | — | `facility_status` |
| `facility_type` | enum | — | `facility_type` |
| `opening_year` | integer year | `year` | `opening_year` |
| `closing_year` | integer year | `year` | `closing_year` |
| `has_landfill_gas_collection` | boolean | — | `has_landfill_gas_collection` |
| `annual_incoming_waste_metric_tonnes` | numeric | `metric tonnes/year` | `annual_incoming_waste_metric_tonnes` |
| `waste_in_place_metric_tonnes` | numeric | `metric tonnes` | `waste_in_place_metric_tonnes` |
| `area_square_meters` | numeric | `square meters` | `area_square_meters` |
| `waste_depth` | enum (derived) | `meters` in, category out | `waste_depth` |
| `has_cover` | boolean | — | `has_cover` |
| `cover_types` | enum array | — | `cover_types` |
| `has_biocover` | boolean | — | `has_biocover` |

`waste_depth` is the only **derived** categorical. The agent still reports a number and its
unit; the pipeline converts to metres, then bins on the spec's 5 m boundary. The metre value is
kept in `normalized_value` as evidence and the category lands in the column. A depth of zero or
less is not a measurement, so it is stored NULL rather than binned to `<=5m`.

`has_biocover` is **not** derived from `cover_types` — a plain soil, clay or sand cover is not a
biocover, and the prompt says so explicitly. `consolidated_facility` now carries the column but it
is entirely NULL today, so the gap-fill rule still fires for every facility.



**Gas capture — additionally gated on `has_landfill_gas_collection` being known TRUE.** Asking a
facility with no capture system about methane flaring buys a certain "nothing". Nothing is lost by
the gate: `has_landfill_gas_collection` is itself gap-filled, so `refresh-seed` promotes a
discovery into the baseline and the next pass picks up that facility's GCCS attributes.

| Attribute | Type | Unit asked for | → standardized column |
|---|---|---|---|
| `gccs_ch4_flared_metric_tonnes` | numeric | `metric tonnes CH4` | `gccs_ch4_flared_metric_tonnes` |
| `gccs_ch4_generated_metric_tonnes` | numeric | `metric tonnes CH4` | `gccs_ch4_generated_metric_tonnes` |
| `gccs_ch4_collected_metric_tonnes` | numeric | `metric tonnes CH4` | `gccs_ch4_collected_metric_tonnes` |
| `gccs_ch4_flow_to_project_metric_tonnes` | numeric array | `metric tonnes CH4` | `gccs_ch4_flow_to_project_metric_tonnes` |
| `gccs_energy_project_type` | enum array | — | `gccs_energy_project_type` |
| `gccs_current_project_status` | enum array | — | `gccs_current_project_status` |
| `gccs_collection_efficiency` | fraction 0–1 | `fraction between 0 and 1` | `gccs_collection_efficiency` |

**Computed locally, never searched.**

| Attribute | Type | Purpose |
|---|---|---|
| `distance_to_original_coordinates_km` | numeric, `km` | Haversine between seed and found coordinates. No source, so no tier. Drives the identity gate; review-only, not a standardized column. |

#### Units are converted, not demanded

The agent is asked for the source's **original** unit and the pipeline converts to the spec's
canonical unit. A unit it cannot convert produces a warning and the value is **not promoted**, so
an unconverted number can never land in a column whose name asserts a unit.

| Target | Accepted source units |
|---|---|
| `area_square_meters` → m² | m², **ft² / sq ft** (×0.092903), **acres** (×4046.856), hectares, km² |
| `waste_depth` → m, then binned | m, **feet** (×0.3048), yards → `<=5m` / `>5m` |
| mass → metric tonnes | metric tonnes, kg, US short tons, long tons, pounds |
| rates → per year | /day ×365, /week ×52, /month ×12, /hour, /minute |
| CH₄ → metric tonnes | t, kg, **ft³ CH₄** (×0.0192 kg), **m³ CH₄** (0.679 kg), **MMCFD** (×1e6×365) |
| `gccs_collection_efficiency` → 0–1 | fraction, or a percentage (÷100) |

Bold entries are the spec's own conversion table. Three deliberate refusals: an ambiguous
`"tons"` is **never** converted (metric vs short is a 10% error); a **landfill-gas** volume is
refused because the spec's formula needs a methane fraction the source did not give; and a value
whose unit cannot be identified is refused rather than assumed.

Allowed categorical values come from the **live Postgres enums** — regenerate with
`scripts/generate_enums.py`. Anything outside them maps to NULL with a parse warning rather than
being forced into a wrong category.

### Outputs

| File | Purpose |
|---|---|
| `<run_id>_review.xlsx` | **The SME artifact.** Only rows needing a human decision, with dropdowns. |
| `resolved.csv` | One row per facility × attribute: what we now believe and why. |
| `evidence.csv` | One row per source-backed claim. The audit trail. |
| `sources.csv` | Deduped source registry keyed by normalized URL. |
| `std_facility_tbl_ai_search_<run_id>.csv` | 44-column spec-conformant standardized table. |
| `std_facility_tbl_ai_search_<run_id>.load.sql` | Load statement naming every column, so table column order is irrelevant. |
| `supplementary_leads.csv` | Findings below the credibility floor, plus the agent's own leads. |
| `parse_warnings.csv` | Dropped values, unit failures, unmappable enums. |

### Source credibility

Tier is assigned **locally** from `source_type` and publisher, never taken from the agent — measured
against the pilot responses, the agent's self-assigned tier put news reports at Tier 2, 3 and 4
simultaneously. Its number is kept as `agent_proposed_tier` for drift monitoring only.

| Tier | Class | Can fill an empty baseline | Can override a baseline |
|---|---|---|---|
| 1 | Regulator, permit/registry, operator disclosure | yes, auto-validated | yes |
| 2 | Peer-reviewed, IFI/UN dataset, govt statistics | yes, auto-validated | yes |
| 3 | News, trade press, NGO report | yes, needs review | no |
| 4 | Aggregator, wiki, directory listing | yes, needs review | no |
| 5 | Source type could not be established | yes, needs review | no |

Baseline credibility is judged **per attribute**, from
`consolidation.value_resolution_ledger`, not from the facility's composite
`contributing_data_sources` — so an `osm_2022` site name is tiered as OSM even when the same
facility also has a GHGRP filing. The dataset is shown as `baseline_source`.

A source with no publication date is capped at Tier 3 whatever its publisher. An **empty**
baseline is filled at any tier — a sourced value beats no value — but only Tier 1-2 fills
auto-validate. Once a baseline value exists, only Tier 1-2 may override it.

### Regenerating the categorical enums

Allowed values come from the live database, not a hand-kept list — from Postgres enum types where
one exists, and from CHECK constraints on `consolidated_facility` where one does not
(`waste_depth` is the latter):

```bash
uv run python scripts/generate_enums.py    # rewrites src/waste_ai_search/db_enums.py
```

Re-run it whenever the database vocabularies change. See
[Standardized Facility Schema](#standardized-facility-schema) for how this fits with the
vendored spec, and
[docs/metadata_search_redesign.md](docs/metadata_search_redesign.md) for the full design and the
accepted risks.

## SME Review Workflow

The reviewer opens **one file**: `outputs/runs/<run_id>/<run_id>_review.xlsx`. Everything else in
the run directory is audit material you only need if a row raises a question.

### 1. Open the Review_Queue tab

This is the worklist. It contains only rows the pipeline would not sign off itself — nothing else.
An empty queue means nothing needs you.

Rows arrive here for one of two reasons:

- **A value came from a weak or unclassifiable source** (Tier 3, 4 or 5). It filled a field that was
  empty, but no one has confirmed it.
- **A value disagrees with what the database already holds**, at equal or better credibility.

Everything the pipeline was confident about — Tier 1-2 fills, confirmations, and every search
that found nothing — is already marked `Auto-validated` with `reviewer = AI Agent`, and is not
shown here. In the 10-site pilot that was 122 of 136 rows, leaving 12 for a human.

### 2. Read the row

The five columns that decide your judgement:

| Column | What it tells you |
|---|---|
| `resolution` | Why this row needs you (see the table below) |
| `baseline_value` vs `resolved_value` | What we had vs what the search found |
| `winning_source_tier` vs `baseline_tier` | Whose claim is more credible |
| `baseline_source` | Which dataset supplied the value we already had (blank if there was none) |
| `winning_source_url` | The actual source. **Open it.** |
| `evidence_summary` | The pipeline's one-line explanation of the decision |

Rows are colour-coded by `resolution`, and the tier scale is on the `Definitions` tab.

### 3. Decide

| `resolution` | What to check |
|---|---|
| **Filled empty baseline** | The field was empty and now has a value. Open the URL: does the source actually say this, and is it about *this* facility? |
| **Overrides baseline** | A more credible source contradicts existing data. Confirm the new value is right before it replaces a database value. |
| **Conflict - needs review** | Two sources of equal or near-equal credibility disagree, and the pipeline will not pick a winner. You pick, or reject both. |
| **Confirmed baseline** (rare here) | Normally auto-validated. If it appears in the queue, the site failed the identity check — see below. |

**The single highest-value check: identity.** Tier cannot detect a perfectly credible document that
describes the *wrong landfill*. If `researcher_notes` says
*"Identity not confirmed: source-backed coordinates are N km from the seed location"*, treat every
row for that site as suspect regardless of tier — the pipeline has already withdrawn its own
sign-off. In the pilot this caught a site whose findings were 238 km from the facility.

### 4. Record the decision

Edit only these four columns; everything else is read-only:

| Column | Enter |
|---|---|
| `validation_status` | `Validated` or `Rejected` (dropdown) |
| `reviewer` | Your name |
| `reviewed_date` | The date |
| `researcher_notes` | Why, especially for a rejection |

`Validated` means the value is correct and may be merged. `Rejected` means it must not be.

### 5. Optional tabs

- **Leads** — findings that lost: below the credibility floor, weaker than the baseline, or
  unconvertible units. Read-only. If you judge one good enough, promote it by hand.
- **Parse_Warnings** — values the pipeline dropped or could not map, each attributed to its
  `site_id` and `site_name`. Worth scanning for systematic problems (a recurring unit or enum
  failure usually means an upstream fix, not 50 individual reviews).
- **Definitions** — the allowed values, mirrored from the live database enums.

### If a site could not be searched

`sites_to_retry.csv` lists sites where the agent's own web-search tool failed. These are **not**
"no data found" — nothing was searched. They need a re-run, not review:

```bash
uv run waste-ai-search search --run-id <run_id> --force --site-ids <ids from that file>
uv run waste-ai-search arbitrate --run-id <run_id>
```

### Auditing a decision

`resolved.csv` holds every facility x attribute searched, including `Not found` and auto-validated
rows — the review queue is a strict subset of it. `evidence.csv` holds one row per source-backed
claim with the tier rule applied to each source, so you can see exactly why a value won or lost.
`sources.csv` is the deduped source registry.

## Reverse Geocoding

Reverse geocoding is optional but strongly recommended for live pilot runs. The
pipeline supports cache-only geocoding through JSONL records with either a
`site_id` or a rounded coordinate key.

Example cache row:

```json
{"site_id":"11489","municipality":"Lagos","admin1":"Lagos","admin2":"","formatted_address":"Olushosun, Lagos, Nigeria","geocoder_provider":"manual","geocoder_confidence":"High"}
```

Pass the cache to the search phase so the prompt can use municipality and admin names, which
matters most for local-language searching:

```bash
uv run waste-ai-search search --run-id pilot10 --use-geocode-cache \
  --geocode-cache data/geocode_cache.jsonl
```

Without `--use-geocode-cache` those fields stay empty and every site records
`geocode_status = disabled`.

Live Azure Maps batch reverse geocoding should be added later as a separate
pre-processing command that writes this cache.

## Standardized Facility Schema

The schema has **two sources of truth, and they cover different things.** Neither one is enough
on its own.

| | Authoritative for | Read by |
|---|---|---|
| **The database** | Column names, types, and which values a categorical column accepts | `scripts/check_db_schema.py`, `scripts/generate_enums.py` |
| **The vendored spec** | What a column *means*, its units, its numeric ranges, and how a derived value is produced | `scripts/sync_schema.py` |

The database wins on structure, and a rename there breaks this repo immediately — which is
exactly what happened when `area` became `area_square_meters`: `refresh-seed` started failing
with *column does not exist* before anyone had touched the spec.

But the database carries **no column comments and no range constraints** on
`consolidated_facility`, so it cannot tell you what a column means or what a valid number is. The
clearest case is `waste_depth`. The database says:

```sql
CHECK (waste_depth IS NULL OR waste_depth = ANY (ARRAY['<=5m', '>5m']))
```

That gives the two permitted strings. It does **not** say to convert the source's depth to metres
first, bin at 5 m, or store NULL for a reported zero. Those rules exist only in the spec's prose,
and getting them from the constraint alone would write a bogus `<=5m` for every source reporting
a depth of 0.

### Checking structure against the database

```bash
uv run python scripts/check_db_schema.py       # do the code's columns and vocabularies match?
uv run python scripts/generate_enums.py        # regenerate src/waste_ai_search/db_enums.py
uv run python scripts/generate_enums.py --check  # is the committed copy stale?
```

Both need database access (so, the VPN). Exit `1` means real drift; exit `2` means the database
could not be reached, so CI can tell a network blip from a genuine mismatch.

`generate_enums.py` reads allowed values from **both** places they live: Postgres enum types, and
CHECK constraints on `consolidated_facility`. `waste_depth` is only in the second — it is a `text`
column with a constraint and no enum type, which is why a generator reading `pg_enum` alone never
saw the depth vocabulary appear.

### Checking semantics against upstream

`schema/StandardizedFacilityTableSpecification.md` is **owned by
`RMI/waste_data_ingestion_pipeline`** (`facility_etl/`), not by this repository. It is vendored
here and pinned to an exact upstream commit recorded in `schema/SCHEMA_SOURCE.json`, so a rename
upstream lands as a reviewable diff rather than silently invalidating a run in flight.

```bash
uv run python scripts/sync_schema.py            # pull upstream main, rewrite the vendored copy
uv run python scripts/sync_schema.py --check    # network: are we behind upstream?
uv run python scripts/sync_schema.py --verify   # offline: has the vendored copy been edited?
```

Do not edit the vendored spec by hand — change it upstream and re-sync. `--verify` and a
spec-vs-code column check run offline in the test suite; `--check` runs weekly in CI
(`.github/workflows/schema-drift.yml`), which needs a token that can read the private upstream
repo, stored as the `UPSTREAM_SCHEMA_TOKEN` secret.

When either source renames or retypes a column, the code bound to it must move in the same commit:
`STANDARDIZED_FACILITY_COLUMNS`, `ATTRIBUTE_TO_STANDARD_COLUMN` and the attribute sets in
`schema.py`, the units and guidance in `prompt_builder.py`, the target sets in `unit_converter.py`,
and the DB column lists in `seed_source.py`. The column test fails until they agree.

## Wastemap Postgres Database

Connection settings live in `.env` (see `.env.example` for the keys):

```
WASTEMAP_DB_HOST=wastemap-database-dev.postgres.database.azure.com
WASTEMAP_DB_PORT=5432
WASTEMAP_DB_NAME=postgres
WASTEMAP_DB_USER=<your-user>
WASTEMAP_DB_PASSWORD='<your-password>'
WASTEMAP_DB_SSLMODE=require
```

Quote the password in `.env` if it contains special characters. Azure requires TLS, so
keep `sslmode=require`.

Verify connectivity:

```bash
uv run python scripts/check_database.py
```

Query from code (`src/waste_ai_search/db.py`):

```python
from waste_ai_search.db import fetch_all, fetch_one, execute, connect

rows = fetch_all("SELECT * FROM facility WHERE country_id = %s LIMIT 10", (1,))
count = fetch_one("SELECT count(*) AS n FROM facility")["n"]

# Multi-statement work in one transaction (commits on clean exit, rolls back on error)
with connect() as conn:
    with conn.cursor() as cur:
        cur.execute("UPDATE facility SET name = %s WHERE id = %s", ("New name", 42))
```

Always pass values as parameters (`%s` placeholders), never f-strings, to avoid SQL
injection.

## Seed The Metadata Search From The Database

`consolidation.consolidated_facility` holds one row per **facility-year** (44,260 rows
covering 15,537 physical facilities). The seeder collapses it to one row per physical
facility, keyed on `internal_facility_id`:

```bash
uv run python scripts/seed_metadata_search.py                   # all facilities
uv run python scripts/seed_metadata_search.py --iso3 NGA PHL    # priority countries only
uv run python scripts/seed_metadata_search.py --require-coordinates
uv run python scripts/seed_metadata_search.py --no-backfill     # latest year verbatim
```

`has_biocover` is defined by the spec as of upstream `8c0bb3fe` and sits next to `has_cover` /
`cover_types` in `STANDARDIZED_FACILITY_COLUMNS`, matching the spec's own column order.

Regenerate it whenever the schema or the consolidation changes — the checked-in copy is the CLI
default, so a search runs against whatever was last committed. The regeneration for the
name-provenance fields also grew the corpus from 15,559 to 19,492 facilities, and brought Brazil
from 303 sites (all with exact locations) to 4,225, of which **3,913 are flagged inexact**. The
`COORDINATES_ONLY_ISO3` rule that had never matched anything now fires for all 3,913, scoping each
to a coordinate-only search.

This is now **optional**. `search` and `run` read `consolidation.consolidated_facility` directly,
so no file is needed to start a run. Use this script only to pin a corpus you want to re-run
later, or to work offline; it writes to `outputs/consolidated_sites.csv` and is fed back with
`--input-csv`.

### Original (untranslated) facility names

`consolidated_facility.facility_name` has already been machine-translated, so a search on it alone
looks for a string no local source ever wrote. The clearest case in the corpus: the Mexican site
`DELICIAS` — a city in Chihuahua — reaches the pipeline as `DELICACIES`.

The seed recovers the source's own spelling via `consolidation.value_resolution_ledger`, which
records which dataset supplied each facility's name and under what `data_source_facility_id`,
joined back to that dataset's `raw_data.raw_<source>_translated` table:

| Source | Raw table | Join key |
|---|---|---|
| `osm_2022` | `raw_osm_translated` | `id` |
| `eprtr_2022` | `raw_eprtr_translated` | `facilityinspireid` |
| `mexico_inegi_2016` | `raw_mexico_inegi_translated` | `sdfn_rsur_cvegeo` |
| `sinir_2024` | `raw_sinir_cities_served_by_landfills_translated` | `facility_code` |

Only these four went through translation. The other seven sources are English-language, so their
stored name *is* the original. **14,692** of 19,492 facilities match a raw row, and they split
three ways: **7,992** carry a genuinely different second name, **4,458** translated to themselves
but keep a known non-English source language, and **2,242** came from a source already in English.

OSM is the awkward one: it stores a blob of tags rather than a bare name, in two spellings (JSON
in `name_left`, a Python dict repr in `fixed_name` and `translated`), so `_osm_tag_name` parses
both and pulls out `name`.

The seed gains `original_site_name`, `source_language` and `name_data_source`. Those are two
independent facts, and the prompt branches on both:

| Seed state | Guidance the agent gets |
|---|---|
| Original differs from translation (7,992) | Both names; search the original in the local language first |
| Translated source, name unchanged (4,458 non-English) | One name, but search it in the recorded ISO 639-1 language too |
| English-language source (11,452) | One name, stated plainly as the source's own |

A facility whose original matches its translation is left blank rather than repeating the string —
but its `source_language` is still recorded, because a Portuguese source that translated to itself
still needs searching in Portuguese.

### How duplicate rows are collapsed

Identity columns (`facility_name`, `iso3c_plus`, `area_square_meters`, `latitude`, `longitude`) are
constant within an `internal_facility_id`, so deduping is lossless for identity. The
year-varying measurements are not, so the fold:

1. takes the **latest year** as the reference row;
2. **backfills** only the baseline fields that are still empty, from the most recent year
   that has a value (disable with `--no-backfill`);
3. records provenance in `reference_year`, `source_year_min`, `source_year_max`,
   `source_row_count`, and `backfilled_fields` (e.g. `facility_status@2013`).

`False` and `0` are real values and are never treated as empty, so they are never
overwritten by an older year.

Backfilling matters for baseline coverage — latest-year-only would drop
`waste_in_place_metric_tonnes` from 2,499 facilities to 1,137, and
`annual_incoming_waste_metric_tonnes` from 4,607 to 3,636.

`has_biocover` is read straight from `consolidated_facility`, which has carried the column
since upstream `8c0bb3fe`. It is not derived from `cover_types`.


------
To-dos:
1) ~~Use source_id in ledger table to find original language site name so that the search can result
   better data~~ — done: see [Original (untranslated) facility names](#original-untranslated-facility-names);
2) ~~Update the standardized facility schema~~ — done: realigned to upstream `8c0bb3fe` and
   pinned via `scripts/sync_schema.py`, which reports drift against the ETL repo;
3) Parallel processing; 
4) Saving search result json files to blob storage;
5) Process to promote SME validated data into a raw AI_discovery data source;
6) Process to shape AI_discovery data source into standardized facility schema, similar to other raw data sources.
7) Rerun consolidation;
8) Run through modeling pipeline with AI discovered activity data folded in;

