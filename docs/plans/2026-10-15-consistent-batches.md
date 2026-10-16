# Keep batches consistent: the same sites, never searched twice

**Status:** A (one consolidation run per seed) built on branch
`seed-reads-one-consolidation-run`, stacked on PR #21. B and C not started.
**Date:** 15 October 2026

## Short answers

- **Is batching consistent?** The batch lists are. A site's batch depends only on its country
  (`in_batch` in `src/waste_ai_search/schema.py`). Batch 1 is Canada, the UK, Australia, New
  Zealand and Ireland. Batch 6 is every country that no other batch names. Build batch 3 today or
  next month and it holds the same countries, so batch 1's sites can't end up in it.
- **Can we change how later batches are built?** Yes, as long as nobody edits the country list of
  a batch that has already run. Changing the batches that haven't run yet, or adding the skip rule from
  [the unsearchable-names plan](2026-10-15-skip-unsearchable-names.md), is safe. That rule only
  removes sites from a batch and never moves them to another.
- **What isn't consistent is the seed.** The seed is the list of sites, and what's known about
  them, read from the shared database table. That table has changed in ways this repo doesn't
  handle yet. This is the bigger risk, and the first fix below addresses it.

## What can go wrong

### 1. The seed reads every consolidation run at once

`consolidation.consolidated_facility` now keeps every consolidation run side by side. The table's
own definition says to filter by `consolidation_run_id` to get one coherent set. Climate TRACE's
config warns that dev already holds runs built from two different crosswalks, and Climate TRACE
picks one run: a pinned `CONSOLIDATION_RUN_ID`, or else the newest run for its strategy
(`legacy_replica_v1`). This repo's seed filters by nothing, and neither do the original-name
lookups. Once the table holds more than one run, a batch is built from a blend of runs:

- Sites that an older run had but the newest one dropped come back. That includes non-landfills
  removed upstream.
- One site's values can come from different runs.

Batch 1 may already be affected. Each run saved a manifest
(`consolidation_snapshot/manifest.json`) listing the consolidation runs its seed read, and more
than one means the seed was a blend. The measurement in the other plan prints this for each batch.

### 2. A run only remembers its own searches

The only "already searched" check happens inside a single run folder. A new run id has no memory
of earlier runs, so any overlap is searched, and paid for, again without a warning. With
country-only batches, overlap needs one of these:

| How | Effect |
|---|---|
| Someone edits a finished batch's country list | The removed country falls into batch 6 and is searched again |
| A site's country is corrected in the database | The site moves batch, so it is searched twice or never |
| New sites appear in a finished batch's countries | They're never searched. Running that batch again under a new id would pay for all of it again (batch 1: about 1,900 sites, about $525) |

### 3. Site ids changed meaning

Batch 1 used `internal_facility_id`, which consolidation reassigned on every rebuild. The
design doc has an example: site 11467 was "Abule Egba Landfill" in Nigeria, then a Canadian
"domestic landfill" after a rebuild. The stable id is now `wastemap_id`, and your open PR #21
switches the seed to it. Batch 1's site 11467 and today's 11467 can therefore be different places,
so any comparison across runs has to translate the old ids first.

## Proposal

### A. Pin each run to one consolidation run (before the next batch)

- Pick the run the way Climate TRACE does: `CONSOLIDATION_RUN_ID` if it is set, otherwise the
  newest run for the agreed strategy. Both repos then read the same list of sites.
- The seed, the original-name lookups, the snapshot and the "rebuilt while seeding" check all read
  that one run.
- The run records which consolidation run it used, in its manifest and in the workbook's
  Run_Config tab.
- If no run matches, it stops with a clear message instead of falling back to something else.
- It doesn't need to wait for PR #21: the old and new tables both have `consolidation_run_id`.

### B. Freeze the finished batches (small, any time)

- `schema.py` lists the batches that have already run, with their run ids.
- A test fails, and explains why, if a finished batch's countries change.

### C. Skip sites that an earlier batch already searched (after PR #21)

- At site selection, before any query, drop sites a finished batch already searched. The run
  prints `Skipped 37 site(s) already searched in batch1 (pass --search-again to search them
  anyway).` `--site-ids` still wins.
- Sites are matched on `wastemap_id`. Runs after PR #21 already use it. Batch 1, and any
  other batch run before PR #21, is translated once:
  - Each run's saved snapshot links its sites to their source records (`data_source` +
    `facility_id`).
  - Today's crosswalk links those source records to a `wastemap_id`.
  - A site counts as searched if any of its source records was searched.
  - Sites that can't be matched are listed for a person to check, not guessed.
- Running a finished batch again (for example `--batch 1 --run-id batch1_new_sites`) then searches
  only the sites added since, instead of paying for the whole batch again.

## Order

1. A, before the next batch.
2. B at any time. It's a few lines.
3. C after PR #21 merges. It doesn't block the remaining batches: while B holds, they can only overlap
   a finished batch through a country correction, which is rare.

## Open questions for you

1. What is batch 1's run id, and has any other batch run yet? They go in B's list.
2. For A, is the newest `legacy_replica_v1` run the right default, matching Climate TRACE?
3. Is C worth building now, or after the remaining batches?
