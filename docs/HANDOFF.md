# Waste AI Search handoff

The state of the WasteMAP AI facility search at handover, 16 October 2026: what runs, what's mid-flight, and what's waiting on other teams. It holds no credentials or `.env` values; get those from Rose or the platform team.

## What this is

This repo fills gaps in WasteMAP facility data. For each landfill or dumpsite in `consolidation.consolidated_facility`, an Azure AI Foundry agent with Bing grounding searches the web for missing attributes: status, type, opening and closing years, waste quantities, operator, cover, gas collection and coordinates.

Each found value must cite a source. The pipeline gives each source a credibility tier, from Tier 1 (regulator, permit) down to Tier 5 (unclassifiable), and arbitrates it against the existing database value. Values it trusts are auto-validated. The rest go to a review workbook that subject-matter experts (SMEs) work through in SharePoint.

- 12,122 searchable sites, split into 6 country batches. Batch 1 is done; batches 2–6 have not run.
- USA and Brazil are excluded by default (README, "What is actually searched").

## Where things live

| What | Where |
|---|---|
| How to run, and every rule | `README.md`. Start with "What is actually searched", "SME Review Workflow" and "Bing cost". |
| Why things are the way they are | `docs/metadata_search_redesign.md`. Decisions are numbered (Q1–Q34 settled up front, F1–F35 added later), each with its reason. |
| Run outputs | `outputs/runs/<run_id>/` in the repo checkout. Not in git and not yet in blob storage (see Open work). Copy them across if you work on a different machine. |
| A run's seed | `outputs/runs/<run_id>/seed.csv`: the whole corpus as read from the database when the run started. A resumed run reuses it, so the whole run searches the same seed. `refreshed_seed.csv` is the seed with pass 1's findings merged in, for the pass-2 gas follow-up. `consolidation_snapshot/` is a compressed copy of the database tables the seed was built from, with a manifest. |
| SME review workbooks | SharePoint: ClimateTRACE › Solid Waste › Waste_AI_Search › `<run_id>/`. Point `WASTE_AI_SEARCH_REVIEW_DIR` at your OneDrive-synced copy of that folder and runs publish there. |
| Upstream schema | `RMI/waste_data_ingestion_pipeline` owns the database and `StandardizedFacilityTableSpecification.md`. This repo pins a copy under `schema/`. |

## Setup

1. Install `uv`, clone the repo, run `uv sync`.
2. Copy `.env.example` to `.env` and fill it in. It covers the Foundry project and agent, the dev database connection, the SharePoint review folder and (later) blob storage.
3. Sign in to Azure with `az login`. The Foundry client and blob storage use `DefaultAzureCredential`, not keys.
4. Connect to the VPN. The dev database and the storage account are only reachable over the private network.
5. Check it: `uv run python scripts/check_environment.py` and `uv run python scripts/check_db_schema.py`.
6. Run the tests: `PYTHONPATH=tests/stubs:src uv run pytest -q`.

## Running a batch

```bash
# Search, check links, arbitrate, gas-collection follow-up, publish to SharePoint
caffeinate -is uv run waste-ai-search run --run-id batch2 --batch 2 --workers 10

# Re-arbitrate a finished run offline (no search cost)
uv run waste-ai-search arbitrate --run-id batch2 --no-publish

# Search specific sites only
uv run waste-ai-search run --run-id test --site-ids 2529 16596 --no-publish
```

What batch 1 taught us:

- **Use 10 workers.** 10 ran with no rate limits. 20 and 50 hit the gpt-5.4-mini deployment's token-per-minute quota (HTTP 429). The README's example says 16; use 10 until the quota is raised.
- **Rerunning resumes.** Sites with a saved response are skipped, so rerunning the same command retries only failed and unsearched sites. Rate-limited attempts cost no Bing queries.
- **Keep the Mac awake and on power.** `caffeinate -i` did not stop a lid-close sleep overnight. Use `-is` on AC power with the lid open.
- **Stop gracefully** with Ctrl-C (SIGINT). It finishes the sites in flight and writes the run log.
- **Cost.** Batch 1 used 39,031 Bing queries for 1,931 sites, about $550 at the README's rate. Searches are capped at 6 per site (`--max-web-searches`). The run log's `web_searches` column records queries per site.
- **The gas pass is part of the batch.** Sites where pass 1 found gas collection get a second, gas-only search: 426 sites and about 6,000 queries in batch 1.

## Batch 1 status

Batch 1 (CAN, GBR, AUS, NZL, IRL) was searched on 6 October. All 1,931 sites succeeded after one retry pass.

| Item | State | Notes |
|---|---|---|
| SME review | In progress | SMEs work in `[LIVE] batch1_review.xlsx` in the SharePoint batch1 folder. That file is the source of truth for their decisions. The folder also holds SME feedback notes. |
| Review queue | 3,904 rows | 1,122 sites. 2,932 values were auto-validated and are in `resolved.csv`. One SME takes Canada, the other the remaining four countries. |
| Hand-made workbook additions | Fragile | The batch 1 workbook has 6 added "Name check" `facility_type` rows (snow dumps, a colliery tip) and 4 annotated ones. They live only in `review_queue.csv` and the workbook, so re-arbitrating batch 1 drops them. |
| WP-558 arbitration changes | Not applied | Re-arbitrating batch 1 with current `main` changes 4 gas-project values. They are not in the live workbook. |
| SME guide | Done | `SME_Review_Guide.docx`, in the SharePoint folder and in `outputs/runs/batch1/`. |
| Transfer stations | Known issue | Batch 1 ran before Transfer Station and Recycling Center existed. 8 transfer stations came back "Not a Waste Facility", and 7 others were auto-validated as landfill types. SMEs correct the queued ones with `corrected_value`. The 7 auto-validated ones were left as they are. |

## Open work

### Open pull requests (all drafts)

| PR | What | Notes |
|---|---|---|
| #21 (WP-552) | The seed reads facilities by `wastemap_id` | Needed once dev is rebuilt from the ingestion pipeline's new tables, where `internal_facility_id` is gone. |
| #22 | Each run's seed reads one consolidation run | Stacked on #21. Pins a run with `CONSOLIDATION_RUN_ID`, or takes the newest for the default strategies. Ticket number to be added. |
| #23 | A script measures what searching found for sites with unsearchable names | Stacked on #22. Read-only. Step 1 of `docs/plans/2026-10-15-skip-unsearchable-names.md` on that branch. Its numbers decide which kinds of name batches 2–6 should skip. Ticket number to be added. |
| #24 (WP-552) | A found location is reviewed as one value; a correction can carry its date | Independent of #21–#23. In batch 1, half a location was accepted while the other half went elsewhere at 55 of 157 sites. |

### Database changes (RMI/waste_data_ingestion_pipeline#238)

PR #20 was closed on 16 October because the plan changed:

- **No `bulk_waste_type` column or enum in the database.** The facility tables hold municipal waste only. When building standardized rows:
  - "municipal solid waste", or no answer: write the tonnage as found;
  - "inert waste" or "others": set `facility_type` to 'Non-Municipal Waste Landfill' and leave the tonnage empty.

  Keep `bulk_waste_type` in the search and run files; it's how we know what each site takes.
- **`generate_enums.py` should read every category from the CHECK constraints on `consolidated_facility`**, as it already does for `waste_depth`. The enum types on dev are hand-made leftovers that will be dropped. This goes in a new PR once dev is rebuilt.
- #238 also creates `transformed.transformed_ai_search`, empty, as the load target. It adds Transfer Station, Recycling Center and Not a Waste Facility to `chk_facility_type`, and `ai_search_2026` to `chk_data_source`.
- Once dev carries them, take the three values off `PENDING_UPSTREAM_FACILITY_TYPES` in `schema.py`, but only when `check_db_schema.py` reports each one accepted.

### Not started

- **Promotion step.** Nothing turns SME decisions into database rows yet. Today `arbitrate` writes `std_facility_tbl_ai_search_<run>.csv` before review, unreviewed values included. A promote step should:
  - read the live SharePoint workbook;
  - take auto-validated values plus SME-Validated rows, and use `corrected_value` where an SME corrected one;
  - apply the municipal-only rule above;
  - write the standardized table and load it into `transformed.transformed_ai_search`.

  The year question under Open decisions decides its shape.
- **Blob storage (WP-534 follow-up).** The code exists but has never run against a real account. The account is `wmapingestiondata`, private network only. A Storage Blob Data Contributor role was requested for Rose; check whether you need one too. The container is not settled. Then:
  - add an optional path prefix;
  - upload the existing runs;
  - give `check-links` the same pull/push as `search`.
- **Batches 2–6.** Batch 2 is English-speaking countries such as IND, PHL, NGA and ZAF (856 sites, about $230 estimated). WP-558's prompt changes (transfer-station types, specific names, newer sources) apply from batch 2 onwards.

### Known bug

- **SharePoint publisher.** SharePoint changes an `.xlsx` file's bytes after sync. The publisher's "has an SME edited this?" check compares bytes, so it always says yes, and every republish writes a new dated copy beside the original. Fix: compare the review columns instead. No ticket yet.

## Drafted tickets

These were drafted with Rose; check Jira for which were filed.

- **Route OSM sites whose names show they aren't municipal waste sites to SME review instead of search.** About 186 sites: mining spoil heaps (mostly RUS and UKR), potash and red-mud sites, Montréal snow dumps. The list is in `outputs/osm_name_scan_2026-10-06.csv`.
- **Fix mistranslated and placeholder OSM names upstream.** Russian *отвал* (spoil heap) is translated as "Blade"; 14 Slovak sites are named "update the tag landuse after survey"; about 10 names are URLs.
- **Locate Brazil SINIR dumpsites by matching OSM and GPW landfills within the municipality.** Web search found no usable coordinates for any of 20 test sites, so Brazil is excluded from search.
- **Bing cost, remaining items:** skip low-yield sites (#23 measures this), find key sources once per country, compare search providers.
- **Skip near-empty attributes.** In batch 1 these were found for 3% of sites or fewer: `waste_depth`, `has_biocover`, `cover_types`, `flare_efficiency`, `gccs_collection_efficiency`, `gccs_ch4_flared_metric_tonnes`.
- **Unstable facility IDs** (#21 addresses this) and **E-PRTR duplicate facilities** (18 co-located pairs in `outputs/eprtr_colocated_facility_pairs.csv`).

## Open decisions

- **One record per facility, or per facility-year?** The spec says a standardized record's `year` is the year all its values apply to. This repo keeps one record per facility, stamped with the latest `value_date` (decision Q24). Every value's own date is kept in `resolved.csv` and the review queue, so per-year records can be built later without re-searching. In batch 1, 469 of 1,130 sites have time-varying values from more than one year; a per-year split would add about 805 records.
- **Do "as of today" dates count as a year of record?** 1,034 batch 1 values are dated 2026, mostly from current web pages. Probably fine for status-type fields, less so for quantities.
- **Sites that take several kinds of waste.** The search asks for one tonnage and one main waste type. A site taking 500 t municipal and 500 t mining waste comes back as 1,000 t with no type, and would load as 1,000 t of municipal waste. The proposal on PR #20:
  - ask for the municipal share when a source breaks it down;
  - scale the tonnage by that share;
  - run a cheap follow-up pass on batch 1's sites that have a tonnage.

## Conventions

- One branch per task, off an up-to-date `main`, named `WP-NNN-short-description` after the Jira ticket.
- Start every PR body with the ticket number followed by a space (`WP-558 `) so the GitHub–Jira integration links it.
- Request a Copilot review on each PR. Fix what each comment raises, using Copilot's proposed fix when it's sound, and reply in the thread naming the commit.
- Record new design decisions as the next F-entry in `docs/metadata_search_redesign.md`, with the ticket number.

## Gotchas

- **The database looks down when the VPN is off.** The error is a DNS failure ("failed to resolve host"), not an authentication one.
- **zsh doesn't split unquoted variables.** `--site-ids $IDS` passes one argument; write the IDs out.
- **A shell loop waiting on `pgrep -f "<command text>"` can match itself** and wait forever. Match the Python process instead: `pgrep -f '[p]ython.*waste-ai-search'`.
- **If `gh` can't save its login** because its config folder isn't writable, set `GH_CONFIG_DIR` to a folder you own.
- **`arbitrate` rebuilds the review workbook from scratch**, and the publisher never overwrites a copy someone has edited. Keep SME work in the `[LIVE]` file.
