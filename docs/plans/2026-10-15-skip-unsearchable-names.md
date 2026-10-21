# Stop web-searching sites whose names can't be searched

**Status:** built on branch `skip-unsearchable-names`. Hugh decided on 20 October 2026 to skip all
four classes, in place of step 2's bar, and step 3 is the rule as built. Step 1 (the measurement)
was built first, on branch `seed-reads-one-consolidation-run`.
**Date:** 15 October 2026

## Short answers

- **What does this repo do?** It picks which sites a run sends to a web-search agent (Azure AI
  Foundry with Bing), then sorts what comes back into accepted values and a review workbook for
  SMEs. A batch is a group of countries searched in one run.
- **So we're removing sites from those batches?** Yes. They are removed before any searching
  happens, the same way US and Brazil sites are removed today. Nothing in the database changes.
- **On what basis?** The site's name gives a search nothing to look for. It is filler such as
  "OpenStreetMap Landfill", stray text such as a URL, or only a kind of site such as "domestic
  landfill". A kind of name is skipped only if batch 1 shows that searching it rarely finds
  anything.
- **When does it run?** The measurement runs once, now: you run it on the VPN before the next
  batch. Once agreed, the rule runs automatically inside every `search` and `run`, at the moment
  sites are selected.

## How a run picks its sites today

1. **Seed.** Read every facility from the shared table `consolidation.consolidated_facility`
   (about 19,500 sites).
2. **Select.** Keep the sites this run should search: the batch's countries (or `--iso3`,
   `--site-ids`), minus the default exclusions (US and Brazil), minus sites with nothing left to
   ask.
3. **Search.** Send each selected site to the agent. It makes about 19 billed Bing queries per
   site, roughly $0.27, and returns what it found, with sources.
4. **Arbitrate.** Values the rules can accept go to `resolved.csv`, and the rest go to the SME
   review workbook.

The new rule is one more removal in step 2 (`select_sites` in `src/waste_ai_search/search.py`).
It sits beside the US and Brazil rule and works the same way.

## The basis: the name gives a search nothing to look for

The agent finds a facility by its name and location. If the name is filler, stray text, or just a
kind of site, there is nothing to search for. The queries are then probably wasted, or they find a
different facility nearby.

There are five classes of name. The first four are candidates for skipping:

| Class | What it is | Examples | Sites in the 6 Oct name scan |
|---|---|---|---|
| No name | An empty name, or the filler the ingestion pipeline gives an OSM polygon that has no name | `OpenStreetMap Landfill` | 1,928 |
| Not a name | Text from another OSM tag that ended up as the name | `update the tag landuse after survey`, a URL, `4000` | about 24 |
| Generic label | Says what kind of site it is, not which one | `domestic landfill`, `Landfill`, `Municipal dump` | 874 for `domestic landfill` alone |
| Mining feature | A mining spoil heap or similar, not a waste site. Matched in the English name (blade, terrikon, overburden, spoil, slag, tailings pond) or the original (отвал, террикон, відвал) | `Overburden Blade`, `Blade No5`, `Terrikon` | about 72 |
| Named | Everything else | `Hartland Landfill` | — |

"Generic label" reuses a test that already exists, `is_generic_site_name` in
`src/waste_ai_search/pilot_selector.py`. It matches names made only of words like landfill, dump,
waste, garbage, rubbish and tip, plus a short list of exact phrases (`GENERIC_EXACT_NAMES`).

"Mining feature" leaves out "quarry" and "mine", because many real landfills sit in old quarries
and are named after them, and "tailings" on its own, because Brazil's *rejeitos* (household waste
left after sorting) is translated "tailings": a Brazilian "Tailings Landfill" is a municipal
landfill.

**What the rule does not decide:**

- **Whether the site is a landfill.** A skipped site stays in the database, on WasteMAP and in
  Climate TRACE, unchanged. It just isn't web-searched until it has a name worth searching for.
  Whether it is really a landfill is for the Climate TRACE review (see
  [Related work](#related-work)).
- **Whether a mining site leaves Climate TRACE.** The mining class only stops the web search.
  Taking those sites out of the model is the Climate TRACE review's job, and fixing their names is
  the ingestion ticket's.

## The rules tried on real names (15 October 2026)

Without database access yet, the rules were run on the 13,656 sites in the 5 October Climate TRACE
submission. That list is close to, but not the same as, the AI search's own list of sites.

| Class | Sites | Rule that caught them |
|---|---|---|
| No name | 1,133 (8%) | all `Openstreetmap Landfill` |
| Not a name | 0 | none in this list; the 6 Oct scan found them in the consolidated data |
| Generic label | 1,859 (14%) | 1,823 a listed phrase (`Dump` 986, `Landfill` 366, `Solid Waste Landfill` 231, `Municipal Dump` 139, `City Landfill` 56); 36 only generic words |
| Mining feature | 31 | `Overburden Blade`, `Blade No1` to `No11`, `Overburden Dump`: 30 in Russia, 1 in Kyrgyzstan |
| Named | 10,633 (78%) | |

What it shows:

- **Brazil accounts for most generic labels** (`Dump` 866, `Landfill` 287, `Municipal Dump` 135),
  but Brazil is already excluded from the search, so they don't matter here.
- **Batch 4 would lose most of its sites:** Turkey 464 of 559 (mostly `Openstreetmap Landfill`)
  and Russia 514 of 691 (`Solid Waste Landfill` 224, `Dump` 89, `Openstreetmap Landfill` 85,
  `City Landfill` 52, mining names 30). Batch 1 would lose 82 of 686, batch 2 37 of 806, batch 3
  (Mexico) 3.
- **The generic test misses names that only describe a kind of site:** `Controlled Landfill` 360,
  `Municipal Sanitary Landfill` 99, `Sanitary Landfill` 67, `Municipal Landfill` 31,
  `Public Dump` 11, `Former Landfill` 5 all count as real names, while `Municipal Dump` and
  `City Landfill` count as generic. Most of these are Brazilian, so excluded anyway.
- **Untranslated names count as real:** 15 sites in Cyrillic, such as `Полигон Тбо` ("MSW
  landfill"), which is generic.
- **Mining names are caught, with no false alarms:** all 31 matches are Russian or Kyrgyz spoil
  heaps. A first version also matched 21 Brazilian "Tailings" landfills, which are municipal (see
  above), so "tailings" alone was dropped.

Open questions this raises:

1. Should the generic test also catch description-only names like `Sanitary Landfill`,
   `Controlled Landfill` and `Municipal Landfill`? The test reads only the English name, since it
   knows only English words: a site whose English name is a generic translation is skipped even
   when its original names it (`Solid Waste Landfill` for `Полигон ТБО «Новосёлки»`), because the
   test can't tell that original from a generic one such as `Полигон ТБО`. An empty English name
   with a source spelling does count as named. Initials and route codes (`K & W Landfill`,
   `SR 85 Landfill`) count as a name too; at first they were classed as generic labels.
2. Is skipping about three quarters of Turkey and Russia right? Batch 1 included sites with no
   name (New Zealand had 44 in this list), so the measurement can show whether searching them
   found anything.

## Step 1: measure before deciding (built first)

Batch 1 has already searched sites with these names, so we can see what those searches found
rather than guess. Any other finished batch is measured the same way. A read-only script
compares the classes.

**What it compares against.** It compares each class with named sites that, like most unnamed
ones, come only from OpenStreetMap or Global Plastic Watch. Those sites start with the same empty
record, so the comparison is fair. All named sites would flatter the comparison, because many come
from government registries that already hold much more data.

**What it measures, for each class and batch:**

| Measure | Read from |
|---|---|
| Sites searched, and the Bing queries they used | `foundry_run_log.csv` (`web_searches`) |
| Sites where the search found at least one new value, whether it filled an empty field or challenged an existing one, and whether it was accepted automatically or sent to review | `resolved.csv`, `review_queue.csv` |
| Sites where it found a real facility name | the same files, rows for `found_facility_name` |
| Sites marked "Identity not confirmed": coordinates found more than 5 km away, so probably the wrong facility | `researcher_notes` |
| Bing queries per new value: what a finding costs | worked out from the above |
| If SMEs have reviewed: values accepted, rejected, and rejected as "Wrong facility" | the reviewed workbook in the SharePoint folder |
| Which consolidation runs the batch's seed read. More than one means the seed was a blend of runs (see [the consistent-batches plan](2026-10-15-consistent-batches.md)) | `consolidation_snapshot/manifest.json` |

Each run is read with its own site ids, so the change from `internal_facility_id` to `wastemap_id`
doesn't affect these numbers.

**What it forecasts.** It also works out how many sites in each class the batches that haven't run
yet would search, and what they would cost at the queries per site batch 1 actually used.

**What it lists.** `--list-sites` writes every site each batch would skip to
`outputs/name_yield/would_skip.csv`, with the rule it tripped and a Google Maps link, and prints
the most common names in each class, so the rules can be checked against real names. It needs only
the database, not blob storage.

**How it reads data.** For each run it reads only the small files it needs, straight from blob
storage into memory: `seed.csv`, `foundry_run_log.csv`, `resolved.csv` and `review_queue.csv`. It
reads SME decisions from the synced SharePoint folder without changing the workbook. The forecast
reads the database, using one consolidation run picked the way Climate TRACE picks it. Nothing is
written to blob, the database or SharePoint, and nothing is
re-arbitrated or republished. The output is a printed table plus a few small CSVs in
`outputs/name_yield/`, which git ignores: the per-site detail for each run, the summary, and the
forecast.

**How you run it.** Connect to the VPN and run `az login`. This checkout has no `.env` yet: copy
yours in, or copy `.env.example` and fill in the storage account and database settings. Then:

```bash
uv run python scripts/measure_name_yield.py --run-id <batch 1 run id>
```

With no run ids it lists the runs in blob, so nobody has to remember their names. A run folder
that is already on disk can be passed with `--run-dir` instead.

**What gets built:**

- `scripts/measure_name_yield.py`.
- `src/waste_ai_search/searchable_names.py`, which holds the four-class test. The measurement and
  the rule then classify names the same way.
- Tests that use a small fake run folder and no network.

## Step 2: decide, with the bar set before the numbers come in

Proposed bar (your call):

> Skip a class by default when its sites get a new value **less than half as often** as named
> map-only sites, **or** when SMEs rejected most of its reviewed values as the wrong facility.
> Keep searching any class that clears the bar.

If a class had too few sites to judge (fewer than 30 searched), keep searching it and look again
after the next batch.

## Step 3: the rule (built only after step 2)

- **Where and when it runs.** It runs inside site selection, on every `search` and `run`, before
  any query is sent. There is nothing extra to run: `uv run waste-ai-search run --batch 3` applies
  it.
- **What you see.** A line like the country rule's: `Skipped 412 site(s) with no searchable name
  (pass --include-unsearchable-names to search them).` The skipped sites and their class go to
  `skipped_sites.csv` in the run folder. The counts go in the workbook's Run_Config tab, so every
  workbook records which policy produced it.
- **Overrides.** `--site-ids` always wins: naming a site searches it, as with the country rule.
  `--include-unsearchable-names` turns the rule off for a run. Samples taken with `--pilot-size`
  include these sites only when that flag is passed.
- **Scope.** Only the classes approved in step 2 are skipped. The others are still searched.
- **Batches don't change.** The rule only removes sites from a batch. It never moves a site to
  another batch, so it can't make batches overlap. See
  [the consistent-batches plan](2026-10-15-consistent-batches.md).
- **Docs and tests.** "What is actually searched" in the README gets a fourth rule. Tests cover
  each class, both overrides and the skipped list.
- **Timing.** Before the next batch runs.

## Later, once upstream changes land

- The ingestion ticket may blank `OpenStreetMap Landfill`. If it does, those sites arrive with an
  empty name and the rule still skips them, because an empty name counts as no name.
- Once the types from ingestion PR #238 are in the database, a site already typed `Not a Waste
  Facility` or `Non-Municipal Waste Landfill` shouldn't be searched either. That is a separate
  small rule, for after #238 merges.

## Related work

- **Consistent batches.** [Its own plan](2026-10-15-consistent-batches.md) covers reading one
  consolidation run per seed, freezing the batches that have run, and never searching a site twice.
- **Climate TRACE review.** Runs in its own session, in the Climate TRACE repo. It turns the OSM
  report's flags into a review sheet for checking suspect sites in Google Maps, and drops the
  non-landfills from the model.
- **Ingestion name ticket.** For the ingestion repo's maintainers: fix how отвал and террикон are
  translated, stop using note and description text as names, and decide what to do with
  `OpenStreetMap Landfill`.
- **Results upstream.** The Climate TRACE review's results go to ingestion as a data file, not as
  code.

## Open questions for you

1. What is batch 1's run id, and has any other batch run yet? If you don't know the id, the
   script lists them.
2. Have SMEs started reviewing the batch 1 workbook? If not, step 1 still works, but it
   can't say how many findings were wrong.
3. Is the bar in step 2 right?
4. Is `--include-unsearchable-names` a good name for the flag?
