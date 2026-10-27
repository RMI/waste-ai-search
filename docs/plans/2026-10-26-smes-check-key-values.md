# AI search: SMEs check seven key values, as well as which site a source is about

**Status:** proposal, 26 October 2026. Builds on
[2026-10-21-approve-sources-site-by-site.md](2026-10-21-approve-sources-site-by-site.md) (PR #36),
whose stack is built but not merged.
**Date:** 26 October 2026

## Why

- **Where the stack is now.** SMEs check one thing: whether a source is about our site (the
  identity check, on `Site_Review`). Once a pair is approved, automatically or by an SME, every
  value it gives loads unseen. We trust the agent to read values off a page.
- **What changes.** Seven values matter too much to load unseen. A person checks each one against
  its source, whatever the source's tier:
  - **site type:** `facility_type`;
  - **closure year:** `closing_year`;
  - **incoming waste:** `annual_incoming_waste_metric_tonnes`;
  - **whether the site captures gas:** `has_landfill_gas_collection`, the most important of the gas
    values;
  - **whether the gas project still runs:** `gccs_current_project_status`. A project that has
    stopped captures nothing, so it matters almost as much as gas collection itself;
  - **what the captured gas is used for:** `gccs_energy_project_type` (flared, electricity, RNG
    and so on);
  - **how much gas is captured:** `gccs_collection_efficiency`. The search rarely finds it (none
    in the 10-site test), but when it does it moves the model's output a lot.
- **What we know of the error rate.** In the 10-site tests, 8–11% of the values that would have
  loaded were wrong even from the right facility, mostly planned or permitted figures read as
  actual. Closure years and tonnages are exactly those.

## Decisions

| # | Decision |
|---|---|
| 1 | **The seven checked attributes never load without an SME's OK on the value,** whatever the tier. They are one list in `schema.py`, so adding one later is a one-line change. |
| 2 | **The identity check stays as it is.** A pair is still approved automatically (Tier 1–2, no exception) or on `Site_Review`. |
| 3 | **A checked value loads only when both checks pass:** its pair is approved (auto, Correct site or Different site) and its value is marked Correct. |
| 4 | **The value check is a separate tab, `Value_Review`,** one row per value, grouped by site and attribute so rival values for one field sit together. It asks a different question from `Site_Review`, and may be done by a different person. |
| 5 | **Only values that would load are listed:** fills of empty fields, as now (decision 8 of the approve-sources plan). A value that confirms or would change ours stays "not used". |
| 6 | **The other attributes are unchanged:** they load with their pair, unseen. |

## How it works

### `found_values.csv`

- **`status` keeps meaning the pair's route:** auto-approved, SME review or not used.
- **A new column, `value_check`:** "required" for a loadable value of a checked attribute, blank
  otherwise.
- **After review:** `value_decision` beside `sme_decision`; `loaded` is yes only when both pass.

This keeps the two checks apart. An auto-approved pair still auto-approves its operator and
opening year, while its closing year waits on `Value_Review`.

**The gas follow-up search is unchanged.** The other gas values are asked only of a site with gas
collection. `refresh-seed` already carries a pass 1 "yes" forward, reviewed or not, so the second
pass asks for the rest. Only loading waits on the SME, so a wrong "yes" costs one extra search,
and its gas values still get checked before they load.

### The `Value_Review` tab

| Group | Columns |
|---|---|
| Our site | site_id, site_name, country |
| The value | attribute, value, unit, year, value as written, value date, how the agent read it (`value_basis`, confidence, note) |
| The evidence | quote, quote found on the page?, link, title, tier |
| The pair | review_id, and its site check: "auto-approved" or "on Site_Review" |
| SME | value_decision, reviewer, reviewed_date, notes |

| value_decision | What happens |
|---|---|
| Correct | Loads, if its pair is approved too. |
| Wrong | Doesn't load. |
| Can't tell | Doesn't load. |
| Blank | Nothing yet. |

- **Rows are ordered** by site, then attribute, then year, then tier, so an SME sees every
  candidate for one field together.
- **Two different values marked Correct for one field:** `accept-reviews` loads one by the usual
  rule (best tier, newest, most sources) and prints a warning naming both rows.
- **"Not a Waste Facility" stays on `Site_Review`.** It is a verdict on the pair, not a value.
  Every other `facility_type` value goes to `Value_Review`.

### `Site_Review`

- **An auto-approved pair still isn't listed,** even when some of its values wait on
  `Value_Review`: its site is settled, only those values wait.
- **`would_load` marks a checked value** with "(value checked on Value_Review)", so an SME approving
  the site knows those values still need a second OK.
- **The rival-value flags** stop treating a checked attribute's auto-approved value as certain to
  load: it now says "waits on Value_Review" instead of "loads instead".

### `accept-reviews`

- Reads both tabs. Applies decision 3, then picks one value per site, attribute and year.
- **Warns, naming the row,** where a value is marked Correct but its pair is Junk or New site (the
  value check is then moot), and where its pair is still undecided (it loads once the pair is).
- **`accepted_values.csv` keeps its columns,** so ingestion #261 needs no change. *(See open
  question 3.)*

### The prompt: planned closure years come back

On 22 October Hugh asked to keep expected closure years such as Moose Jaw's 2028, which 95c7c4a's
"no future values" rule now drops. With every closure year checked by an SME, that is safe:

- the prompt lets `closing_year` be a planned year, with `value_basis` saying it is planned;
- `Value_Review` shows "planned" in the value's row, so the SME decides whether to keep it.

Every other attribute keeps the actual-figures-only rule.

## On the 10-site test (gpt-5.4, `test10-gpt54`)

|  | Today | With this plan |
|---|---|---|
| Values auto-approved, loading unseen | 51 | 32 |
| Values on `Value_Review` | 0 | 54, across all 10 sites |
| …of which from auto-approved pairs | | 19 (8 site type, 4 incoming waste, 3 gas project status, 2 closure year, 1 gas collection, 1 gas use) |
| …of which from pairs on `Site_Review` | | 35 |

So about five value checks per site, on top of the site checks. Most are quick: the quote is
next to the value. No collection efficiency was found: the agent offered six, all as unverified
leads, which don't load.

## Build order

Stacked on `source-review-docs`, each a branch:

1. **Sorting:** `CHECKED_ATTRIBUTES` in `schema.py`; `found_values.py` writes `value_check`.
2. **The `Value_Review` tab,** and the `Site_Review` changes (`would_load`, rival flags, pairs left
   off). The workbook's ReadMe gets a step for it.
3. **`accept-reviews`** reads `Value_Review` and applies decision 3.
4. **Prompt:** planned closure years allowed, marked as planned.
5. **Docs:** README, a design-doc decision (F42), this plan's status. Re-run `test10-gpt54` from
   its cached responses and update the figures above.

Tests go with each step.

## Open questions for Hugh

1. **Decided, 26 October: the other gas values are left out.** Methane flared a year
   (`gccs_ch4_flared_metric_tonnes`) and the flare values (`has_flare`, `flare_efficiency`) load
   with their pair, as now.
2. **Can an SME type the right value** when the agent misread it (say 12,000 t, not 120,000 t)?
   That needs a `corrected_value` column and a way to record that the loaded value is the SME's.
   *Proposed: not now. An SME marks it Wrong and writes the right value in notes.*
3. **Should `accepted_values.csv` say who checked a value?** A `value_reviewer` column would need
   one small change in ingestion #261. *Proposed: not now.*
4. **Batch 1:** its 2,686 auto-approved values include these seven attributes, loaded unseen. Should
   the batch 1 conversion (step 5 of the approve-sources plan, not built) send them to
   `Value_Review` too? *Proposed: count them first, then decide.*

## Accepted risks

- **Values of the other attributes still load unseen,** including operator, opening year, waste in
  place, methane flared and the flare values.
- **An SME checks the value against the quote, not the whole page,** unless they open the link. A
  quote not found on the page is flagged, as now.
