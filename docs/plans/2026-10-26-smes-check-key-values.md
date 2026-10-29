# AI search: SMEs check every site match, and seven key values from Tier 3–5 sources

**Status:** proposal, revised 29 October 2026. Builds on
[2026-10-21-approve-sources-site-by-site.md](2026-10-21-approve-sources-site-by-site.md) (PR #36),
whose stack is built but not merged, and changes two of its decisions (see
[What this changes](#what-this-changes)).
**Date:** 26 October 2026; revised 29 October 2026

## Why

- **Where the stack is now.** A Tier 1–2 source's match to our site is approved automatically,
  unless it hits an exception (same page under two sites, more than 5 km away, "Not a Waste
  Facility"). SMEs check the match of every other source. Once a match is approved, every value
  the source gives loads unseen.
- **What is wanted now.** Every site's matches are checked by a person, whatever the tier. And
  some values matter too much to load unseen from a weaker source, so for Tier 3–5 sources an SME
  checks those values against the page too:
  - **site type:** `facility_type`;
  - **closure year:** `closing_year`;
  - **incoming waste:** `annual_incoming_waste_metric_tonnes`;
  - **area:** `area_square_meters`. Pages often give a permitted or whole-property area rather
    than the area filled, and in hectares or acres;
  - **whether the site captures gas:** `has_landfill_gas_collection`;
  - **what the captured gas is used for:** `gccs_energy_project_type` (flared, electricity, RNG
    and so on);
  - **how much gas is captured:** `gccs_collection_efficiency`. Rarely found, but it moves the
    model's output a lot.

  The list Hugh was sent (names and coordinates; site type, area, incoming waste, closure year and
  gas use) is the same, with gas collection and collection efficiency added at Hugh's request. Names and coordinates are
  what the match check already shows.

## Decisions

| # | Decision |
|---|---|
| 1 | **An SME checks the match of every site–source pair,** Tier 1–2 included. Nothing is approved automatically. |
| 2 | **For a Tier 3–5 source, an SME also checks each of the seven key values** it would load. Its other values load with the match. |
| 3 | **A Tier 1–2 source's values all load with the match,** key values included. |
| 4 | **A source that doesn't match loads nothing:** Junk and New site load none of its values, whatever its value checks say. |
| 5 | **A key value loads only when both pass:** the pair is Correct site (or Different site) and the value is marked Correct. A blank value check doesn't load. |
| 6 | **One tab.** The value checks sit under their pair on `Site_Review`, so an SME reads a page once and decides the match and its values together. |
| 7 | **Only values that would load are listed:** fills of empty fields, as now. |
| 8 | **The seven attributes are one list in `schema.py`,** so changing it later is a one-line change. |

## What this changes

- **Decision 3 of the approve-sources plan (Tier 1–2 auto-approval) is reversed.** Tier 1–2 pairs
  go on `Site_Review` like the rest. Their exceptions (same page under two sites, more than 5 km,
  Not a Waste Facility) become flags on the row rather than the reason it is listed.
- **`found_values.csv`'s statuses:** "auto-approved" goes. A loadable value is "SME review", and a
  new column, `value_check`, is "required" for a Tier 3–5 key value.
- **719337d's rule goes:** a Tier 3–5 source whose values a better auto-approved source already
  gives was set aside as not used. With nothing auto-approved, it is listed. Where a better-tier
  source on the sheet gives a different value, the row says so, as the rival-value flags do now.

## The `Site_Review` tab

One row per site–source pair, grouped by our site, as now. Under a Tier 3–5 pair, one indented row
per key value it would load:

| Row | Shows | SME picks |
|---|---|---|
| Pair | Our site; the source's link, title, tier; its name, address, coordinates and distance; flags; what approving would load | Correct site / Different site / New site / Junk |
| Key value (Tier 3–5 only) | Attribute, value, unit, year, value as written, the quote, whether the quote is on the page, the agent's note | Correct / Wrong |

### Making it easier in Excel

The sheet already has dropdowns, clickable links, a frozen header and a rule between sites. On top
of that:

- **The value rows can be folded away** (Excel's row grouping), so an SME checking matches sees
  one row per source, and opens the values when they get to them.
- **The link jumps to the quote.** The source link adds a text fragment (`#:~:text=...`), so
  Chrome and Edge open the page scrolled to the quote, highlighted. It doesn't work on PDFs, where
  the plain link still opens the file.
- **Both points open in Google Maps.** Our coordinates and the source's become map links, instead
  of text to paste.
- **A rejected pair greys out its value rows,** once an SME picks Junk or New site, so nobody
  checks values that won't load.
- **A Tier 1–2 pair with no flags is marked as such,** so an SME can move through those quickly:
  the ReadMe says to confirm the name and location, and move on.
- **The ReadMe's steps are rewritten** for the one-tab check.

## `accept-reviews`

- Reads the pair rows and the value rows, applies decisions 3–5, then picks one value per site,
  attribute and year (best tier, newest, most sources).
- **Warns, naming the row,** where a value is marked Correct under a pair that is Junk, New site
  or blank, and where two different values for one field are both marked Correct.
- **`accepted_values.csv` keeps its columns,** so ingestion #261 needs no change.

## The prompt: planned closure years come back

On 22 October Hugh asked to keep expected closure years such as Moose Jaw's 2028, which 95c7c4a's
"no future values" rule drops. The prompt lets `closing_year` be a planned year, with `value_basis`
saying so; the row shows "planned". From a Tier 3–5 source an SME checks it; from a Tier 1–2 source
it loads, as a regulator's or operator's planned closure date. *(See open question 1.)*

## On the 10-site test (gpt-5.4, `test10-gpt54`)

|  | Today | With this plan |
|---|---|---|
| Pair rows on `Site_Review` | 32 | up to 61: every pair, Tier 1–2's 23 included |
| Value rows under them | 0 | 27, under 21 Tier 3–5 pairs |
| Values loading with no one looking at the source | 51 | 0 |

The 27 value rows: 8 site type, 8 gas collection, 7 gas use, 3 closure year, 1 area. No
incoming waste or collection efficiency came from a Tier 3–5 source in this test.

## Build order

Stacked on `source-review-docs`, each a branch:

1. **Sorting:** auto-approval removed; `KEY_ATTRIBUTES` in `schema.py`; `value_check` in
   `found_values.csv`; 719337d's rule removed.
2. **`Site_Review`:** every pair listed, value rows under Tier 3–5 pairs, and the Excel helps above.
3. **`accept-reviews`** reads value rows and applies decisions 3–5.
4. **Prompt:** planned closure years allowed, marked as planned.
5. **Docs:** README, a design-doc decision (F42, reversing F41's auto-approval), the approve-sources
   plan's status. Re-run `test10-gpt54` from its cached responses and update the figures above.

Tests go with each step.

## Open questions for Hugh

1. **A Tier 1–2 planned closure year** loads with its match, unchecked. Fine, or should a planned
   year always get a value check?
2. **Can an SME type the right value** when the agent misread it (say 12,000 t, not 120,000 t)?
   *Proposed: not now. Mark it Wrong and put the right value in notes.*
3. **Should `accepted_values.csv` say who checked a value?** *Proposed: not now;* it needs a change
   in ingestion #261.
4. **Batch 1:** its 2,686 values were auto-approved Tier 1–2 fills, which this plan would also have
   loaded once an SME approved the match. Should the batch 1 conversion send its matches to SMEs?
   *Proposed: count them per site first, then decide.*

## Accepted risks

- **A Tier 1–2 source's values load once its match is approved,** key values included. The 8%
  error rate of the 10-site test (planned or permitted figures read as actual) still applies to
  them.
- **An SME checks a value against the quote,** unless they open the page. The quote-not-on-page
  flag stays.
- **More SME work on matches:** every pair is now a row. Most Tier 1–2 pairs are quick, as the
  regulator or operator names the site.
