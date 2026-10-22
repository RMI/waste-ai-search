# AI search: SMEs approve sources site by site, and Tier 1–2 values are approved automatically

**Status:** proposal, 21 October 2026. Nothing is built. This replaces the matching and review parts
of [2026-10-20-match-sources-by-location.md](2026-10-20-match-sources-by-location.md) (PR #27).
**Date:** 21 October 2026

## Why

- **Where the branches are now.** The stack (#30 to #32) sends every value the search finds to an
  SME, one row per site, attribute and year. Our own code also moves sources between sites, using
  distance and fuzzy name matching. That means a lot of rows, and SMEs check values they don't
  need to check.
- **The principle.** We trust the agent to extract values from a page. What a person has to check
  is whether the page is about our site.
- **Batch 1's rule comes back.** Batch 1 approved a Tier 1–2 value automatically when it filled an
  empty field (2,686 values). That returns, together with batch 1's 5 km location check, which now
  runs per source.

## Decisions

| # | Decision |
|---|---|
| 1 | **The agent decides whether a source is about the site searched,** as in batch 1. Our code no longer moves sources between sites by distance or name. |
| 2 | **Every source is filed under the site that was searched.** |
| 3 | **Auto-approved:** a Tier 1–2 value that fills an empty field. Tier 1 is a regulator, permit or operator; Tier 2 is government statistics, peer-reviewed or intergovernmental data. |
| 4 | **Exceptions, sent to SMEs whatever the tier:** the same page and facility name cited for two or more of our sites; source coordinates more than 5 km from ours; a "Not a Waste Facility" verdict. |
| 5 | **Every decision, automatic or SME, is per site–source pair, not per source.** A page with good data on three of our sites gets three decisions, and each loads only that site's values. SMEs review the pairs grouped by our site, so such a source appears under each. |
| 6 | **One value is loaded per site, attribute and year,** chosen automatically when approved sources disagree: best tier, then newest, then the most sources. |
| 7 | **A source's name and coordinates are only used to check the match.** They never replace ours. *(default)* |
| 8 | **Only fills of empty fields load.** A value that would change one we hold is listed, not loaded. *(default)* |
| 9 | **Dates become years:** `value_date` becomes `year`. |
| 10 | **The source's address and coordinates go in separate columns.** |

## The workflow

| # | Step | In short |
|---|---|---|
| 1 | **Pick sites** | As now (#29): skip names a search can't use, such as "domestic landfill". |
| 2 | **Search** | The agent returns only sources about this site. Each states its facility name, address and coordinates as written. |
| 3 | **Locate** | Code turns each source's coordinates or address into a point, and measures the distance to our site. |
| 4 | **Sort** | Every value is written to `found_values.csv` as auto-approved, SME review or not used. |
| 5 | **SME review** | A `Site_Review` tab: under each of our sites, the sources still to check. SMEs choose Correct site, Different site, New site or Junk. |
| 6 | **Accept** | `accept-reviews` loads values from auto-approved and SME-approved sources, and writes `accepted_values.csv`. |
| 7 | **Ingestion** | #261 loads `accepted_values.csv`. |

## The rules

### 2. Search

- **The batch 1 rule returns:** return only sources about this facility, and nothing if you can't
  confirm it. #31 replaced it with "report sources about other facilities too", which is reversed.
- **What each source reports:**
  - `facility_name_in_source`, the name as written;
  - `facility_name_english`, the agent's English rendering of it;
  - `stated_address`, the address as written;
  - `stated_coordinates`, coordinates or a grid reference as written.

  The last two replace `stated_location` and `stated_location_kind`. Older cached responses are
  split using their kind labels.
- **Our coordinates in the prompt stay rounded** to 2 decimals. A location equal to them counts as
  no location.
- **Optional:** tell the agent a facility's address is often in a page's contact box or footer.
  On gpt-5.4, 5 of 51 pages had an address the agent didn't copy.

### 3. Locate

- **`source_coordinates`** ("lat, lon") comes from the stated coordinates, a converted grid
  reference, or else the geocoded address. Addresses get today's clean-up first: "in" and "at"
  become commas, and directions and land survey descriptions are dropped.
- **`coordinates_from`** says which: stated, address (building) or address (street). A street-level
  point can be a kilometre or more from the address.
- **`distance_km`** is the distance from our site's point.
- **No coordinates means no 5 km check,** so a Tier 1–2 value auto-approves, as in batch 1.

### 4. Sort: `found_values.csv`

One row per site, year, source and attribute. It replaces `evidence.csv`, `resolved.csv`,
`review_queue.csv` and `supplementary_leads.csv`.

| Status | When |
|---|---|
| auto-approved | Tier 1–2, fills an empty field, and none of the exceptions in decision 4 |
| SME review | Tier 3–5, or a Tier 1–2 exception |
| not used | Confirms our value, would change our value, dead link, or a unit that couldn't be converted (the reason is given) |

**Columns:**
- the site's id and name, and the year;
- the source's id, URL and tier;
- the attribute, value and unit;
- the agent's quote and whether it is on the page, and the agent's note;
- status and reason;
- after review: `sme_decision` and `loaded`.

### 5. SME review: the `Site_Review` tab

- **One row per site–source pair still to check,** grouped by our site. Auto-approved pairs aren't
  listed.

| Group | Columns |
|---|---|
| Our site | site_id, site_name, original_site_name, country, our coordinates |
| The source | link, title, publisher, tier |
| What it says | name, address, coordinates, coordinates_from, distance_km |
| Flags | Quote not found on the page; same page and name also under site X; more than 5 km away; says Not a Waste Facility |
| For context | What approving would load: attribute = value (year) |
| SME | decision, other_site_id, reviewer, reviewed_date, notes |

| Decision | What happens |
|---|---|
| Correct site | Its values for this site load. |
| Different site, with a site id | Its values load under that site. |
| New site | Nothing loads. The source goes to `new_facility_candidates.csv`. |
| Junk | Nothing loads. |
| Blank | Nothing yet. |

- **The other tabs:** ReadMe, Run_Config, Definitions and Parse_Warnings stay. Review_Queue and
  Leads go; `found_values.csv` holds what Leads held.

### 6. Accept: `accept-reviews`

- **What it does:** it reads the reviewed workbook, applies each decision, and picks one value per
  site, attribute and year (decision 6).
- **What it writes:** `accepted_values.csv`, `found_values.csv`'s `sme_decision` and `loaded`, and
  any new sites to `new_facility_candidates.csv`.
- **When it runs:** any time. Values from pairs not yet reviewed just don't load until it runs
  again.

### Other files

- **`sources.csv`** (one row per URL) is kept.
- **`datasets_found.csv`** is kept, counting the names and locations a page gives and the searches
  that cite it, instead of the sites matching put it under.
- **`possible_duplicates.csv`** is kept, holding only pairs of our sites cited under the same page
  and facility name.
- **`new_facility_candidates.csv`** only gets SMEs' New site decisions.
- **`outside_run_matches.csv`** goes.

### 7. Ingestion

- **#261** loads `accepted_values.csv` and Run_Config, instead of `resolved.csv` and Review_Queue.
  Its handling of `corrected_site_id`, the match columns and location exactness goes. The one value
  per facility and year check stays.
- **Batch 1:** a one-off script converts its `resolved.csv` and reviewed Review_Queue into
  `accepted_values.csv`, after #26 re-keys it to wastemap_id.
- **#262, #264 and #266** are unaffected.

## On the 10-site tests

|  | gpt-5.4 | mini |
|---|---|---|
| Site–source pairs | 60 | 39 |
| Auto-approved | 23 pairs, 51 values | 19 pairs, 49 values |
| To SMEs | 37 pairs | 20 pairs: 14 Tier 3–5, 3 duplicates, 3 more than 5 km away |
| Review rows today | 102 | 90 |

In mini, Guelph's search cited five pages about Brandon's Eastview Landfill, all Tier 1–2.
Between them, the duplicate rule and the 5 km check catch four:
- two were cited for Brandon as well;
- two have addresses that put the facility 1,645 km away.

The fifth gets through: a Manitoba news release whose only location is "Brandon", cited only for
Guelph. It would be auto-approved under Guelph.

## The current PRs

| PR or branch | Plan |
|---|---|
| #21, #22, #23 (seed, name yield) | Keep |
| #24 (found location as one value) | Close: it's in #30, and SME corrections go away |
| #25 (handoff doc), #26 (batch 1 re-key), #33 (Terraform) | Keep. #26 feeds the batch 1 conversion; the handoff doc needs updating later |
| #27 (the matching plan) | Close: this plan replaces it |
| #28 (mirrors dropped), #29 (unsearchable names skipped) | Keep |
| #30 (nothing approved automatically) | Keep in the stack. Step 2 below brings auto-approval back; its description says so |
| #31 (source-first output, locate) | Keep. Step 1 reverses its prompt rule and splits the location |
| #32 (matching, per-year review, lists) | Keep in the stack. Step 2 removes the matcher; per-year keys, New site, `datasets_found.csv` and same-page duplicates stay |
| #34 (page text, quote check) | Keep. Its checks become flags on the sheet |
| #35 (token cost) | Keep |
| `source-coordinates` (no PR) | Open as a PR. Its street-level 2 km rule is reverted in step 2 |
| `sme-review-wording` (no PR) | Delete: the Review_Queue text it rewords goes |
| Ingestion #261 | Rework in step 6. Its `source_coordinates` commit becomes unnecessary |
| Ingestion #262, #264, #266 | Unaffected |

## Build order

New PRs, stacked on `source-coordinates`:

1. **Prompt and contract:** the agent decides the match, and address and coordinates are separate.
2. **Sorting:** `found_values.csv`, auto-approval and its exceptions, years. The matcher is removed.
3. **The `Site_Review` tab.**
4. **`accept-reviews`** and `accepted_values.csv`.
5. **Batch 1 conversion.**
6. **Ingestion #261 rework.**
7. **Docs:** the README; a design doc decision (F39) reversing F36 (nothing automatic) and F38
   (matching); and this plan's status.

## Accepted risks

- **Tier 1–2 sources without coordinates auto-approve unchecked,** as in batch 1. In the gpt-5.4
  test, 36 of 44 Tier 1–2 review rows came from sources with no usable location.
- **Extraction errors load unseen.** In the tests, 8% (gpt-5.4) and 11% (mini) of the values that
  would have loaded were wrong, even from the right facility. Most were planned or permitted figures
  read as actual. The QAQC of 20 batch 1 auto-approved values will show the rate for Tier 1–2.
- **The agent can claim the wrong facility.** In batch 1, Brandon's landfill page went under 14
  sites. #29's skipped names, the duplicate rule and the 5 km check cover most of what batch 1
  showed. A Tier 1–2 page cited for only one site and giving no usable location gets through, like
  the Manitoba news release under Guelph in the mini test.
