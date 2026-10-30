# AI search: SMEs check every site match, and six key values from weaker sources

**Status:** built on local branch `sme-value-checks` (30 October 2026), not merged; docs (build
step 5) not yet updated. See [As built](#as-built). It changes how the AI search's results are reviewed,
replacing parts of [2026-10-21-approve-sources-site-by-site.md](2026-10-21-approve-sources-site-by-site.md)
(PR #36; built on local branches, not merged).
**Date:** 26 October 2026; revised 29 and 30 October 2026

## In short

- A person checks **every source** the search found: is it about our site?
- For a **weaker source** (news, trade press, NGO reports, directories, blogs), a person also checks
  **six key values** it gives against the page, and can type the right value if the search misread
  it.
- A **strong source** (a regulator, permit, operator or government statistics) is trusted to have
  been read correctly: once its match is approved, all its values load.
- A source that isn't about our site loads nothing.
- It all happens on one Excel tab, `Site_Review`.

## Words used here

| Word | Meaning |
|---|---|
| **Source** | One web page, under one facility name. A page that gives two facilities' names is two sources. |
| **Pair** | One of our sites and one source its search found. Each pair gets one row and one decision. A page cited for two of our sites is two pairs, decided separately. |
| **Tier** | How much we trust the kind of source. **Tier 1:** regulator, permit or registry, court decision, municipal document, environmental impact assessment, operator disclosure. **Tier 2:** government statistics, peer-reviewed or intergovernmental data. **Tier 3:** news, trade press, NGO report. **Tier 4:** directory, map site, wiki, aggregator, blog, or an undated page. **Tier 5:** the agent couldn't say what kind of source it is. "Strong" here means Tier 1–2, "weaker" Tier 3–5. |
| **A pair's tier** | The best tier the search gave that page. A page cited twice (once undated, so Tier 3, and once dated, so Tier 1) counts as Tier 1. |
| **Fill** | A value for a field that is empty in our database for that site (and year, for a yearly field such as incoming waste). Only fills load. A value that matches ours, or would change ours, is recorded but never loaded. |
| **Key values** | The six attributes below. |

## The six key values

| Attribute | Why |
|---|---|
| `facility_type` (site type) | Decides how the model treats the site. |
| `closing_year` | Decides whether the site still receives waste. |
| `annual_incoming_waste_metric_tonnes` | Pages often give a permitted limit or design capacity instead. Counts municipal waste only (see the 30 October municipal-incoming-waste plan): an SME marks rubble, soil or tailings Wrong, or types the municipal part. |
| `area_square_meters` | Pages often give the whole property, or a permitted area, often in hectares or acres. |
| `has_landfill_gas_collection` | Whether the site captures gas: the gas value that matters most. |
| `gccs_collection_efficiency` | Rarely found, but it moves the model's output a lot. |

The reviewers' own list of what they want to check had names and coordinates (which the match check
covers), then site type, area, incoming waste, closure year and gas use; Hugh added gas collection and collection
efficiency. Gas project status, methane flared and the flare values were considered and left out.
Hugh dropped gas use on 30 October, after the Mexico test: it was 4 of the 9 value rows there, and
their quotes were mostly a project title such as "Monterrey I LFG to Energy Project", so checking
them added little.
The list is one line of code, so it is easy to change.

## The rules

| # | Rule |
|---|---|
| 1 | **A person checks the match of every pair,** strong sources included. Nothing is approved automatically. |
| 2 | **For a pair of Tier 3–5, a person also checks each key value it would fill,** on its own row. The pair's other values load with the match. |
| 3 | **A pair of Tier 1–2 loads all its values once its match is approved,** key values included. If an SME spots a misread value from a strong source, they say so in `notes`; it still loads. |
| 4 | **A source that isn't about our site loads nothing:** Junk or New site loads none of its values, whatever its value rows say. |
| 5 | **A key value from a Tier 3–5 pair loads only if both checks pass:** the pair is Correct site (or Different site) and the value is marked Correct. A blank value check loads nothing. So does a key value with no value row, such as one that confirmed our value at the site searched but would fill a field at the Different site. |
| 6 | **An SME can give the right value** when the source is right but the search misread it: mark the value Wrong and type the right value in `corrected_value`. It loads instead of the search's. This is optional: Wrong with no correction loads nothing. |
| 7 | **One value loads per field** (per site, attribute and, for a yearly attribute, year). If several approved sources give different values, the best tier wins, then the newest, then the value most sources give. |
| 8 | **The sources' own names and coordinates never replace ours.** They are shown to check the match. |

## The `Site_Review` tab

One row per pair, grouped by our site (a line separates sites). Pairs with a warning come first
within each site, then by tier. Under each Tier 3–5 pair, one row per key value it would fill.

**A source row** shows, left to right:

1. **Our site:** id, name in English and as written, country, our coordinates (click to open
   Google Maps).
2. **The source:** link, title, publisher, tier, whether the link worked.
3. **Where the source puts the facility:** its name for it, its address and coordinates as written,
   the point we read from them (click for Google Maps), and how far that is from our site.
4. **flags:** warnings, shaded orange. For example: the same page is also cited for another of our
   sites; the source puts the facility more than 5 km away; the quote isn't on the page; it says
   the site isn't a waste facility.
5. **would_load:** every value the page gives, one line per value. Key values say "(check
   independently)": each loads only once its own ↳ row is marked Correct (called "(check below)"
   until Hugh's SME pass on 30 October).
6. **decision:** Correct site / Different site / New site / Junk; **other_site_id** for Different
   site.

**A value row** (indented, can be folded away with Excel's +/−) shows:

- **check:** which value, e.g. `closing_year` or `annual_incoming_waste_metric_tonnes (2019)`.
- **would_load:** the value as it would load, e.g. `closing_year = 2008`.
- **evidence:** the quote from the page, the value as the page writes it (e.g. "12 ha"), and the
  search's note.
- **flags:** for example: the quote isn't on the page; this is a planned closure, not an actual
  one; a stronger source on the sheet gives a different value, so this one loads only if that
  source is rejected.
- **The link** opens the page scrolled to the quote, highlighted (Chrome and Edge; not PDFs).
- **decision:** Correct / Wrong; **corrected_value** optional.

**Rows to ignore turn grey:** once a source is marked Junk or New site, its value rows grey out.

**`corrected_value`** is typed in the unit the row shows: tonnes a year, square metres, a year, or a
fraction from 0 to 1 for collection efficiency (75% is read as 0.75). Site type, gas collection
(TRUE or FALSE) offer a dropdown. If an SME only knows the figure in another unit, they
put it in `notes`.

A `corrected_value` typed with no decision is read as Wrong: typing a value says the found one is
wrong.

**What the SME fills in:** `decision`, and where it applies `other_site_id` or `corrected_value`;
`reviewer`, `reviewed_date`, `notes`. Nothing else is read back.

### What an SME does, row by row

1. Open the source link. Compare its name and location with our site's; use the map links.
2. Read the flags.
3. Pick the source's decision.
4. If it is Correct site or Different site and has value rows: for each, check the value against
   the quote and the page, and pick Correct or Wrong. If Wrong and you know the right value, type
   it in `corrected_value`.
5. Skip the value rows of a source you marked Junk or New site.

## After review: `accept-reviews`

Reads the tab back and writes `accepted_values.csv`, which ingestion loads.

- It applies rules 3–7.
- It **stops and names the row** where something can't be read: a decision that isn't one of the
  choices, a Different site without a site id, or a `corrected_value` that isn't a valid value
  (not a number, a site type that isn't on the list, an efficiency above 100%, a closure year that
  isn't a year).
- It **warns, naming the row,** but carries on, where a value is marked Correct or corrected under a
  source that is Junk, New site or not decided; where a value is marked Correct and also has a
  `corrected_value` (the correction is ignored); and where two different values for one field are
  both marked Correct.
- `accepted_values.csv` keeps its columns. It doesn't say which values a person checked or
  corrected; `found_values.csv` (every value the search found) does, in new columns
  `value_check` ("required" for a Tier 3–5 key value, else blank), `value_decision` and
  `corrected_value`.

## Planned closure years

Hugh asked (22 October) to keep expected closure years, such as a regulator's "closes in 2028".
Today the search is told to leave them out. So:

- The search may return a planned or expected `closing_year`, and marks it with a new field,
  `planned: true`. Other attributes still exclude planned, permitted and design figures.
- A planned year from a Tier 3–5 source is checked like any key value; its row is flagged
  "planned closure". From a Tier 1–2 source it loads with the match.
- A planned closure doesn't count as evidence the site has closed (the check for a closed landfill
  misread as "Not a Waste Facility" ignores it).

**This changes what the search returns, so it must be built before batch 3 runs.** Everything else
here works on the search's saved responses and can be changed later without searching again.

## What changes from the 21 October plan

- **No automatic approval.** Strong sources go to SMEs too. The old reasons for sending a strong
  source to SMEs (same page under two sites, more than 5 km away, "Not a Waste Facility") are now
  warnings on its row.
- **No "covered" sources.** A weaker source whose values a strong one already gives was left off
  the sheet (commit 719337d). With nothing approved automatically, it is listed; where a stronger
  source gives a different value, the row says so.
- **Value rows,** `corrected_value` and the planned closure year are new.

## On the earlier 10-site test (gpt-5.4; 6 Canadian sites, and one each in the UK, Ireland, Australia and New Zealand)

|  | Before | With this plan |
|---|---|---|
| Source rows | 32 (38 before 719337d) | up to 61, the 23 strong sources included |
| Value rows | 0 | 27, under 21 weaker sources |
| Values loaded with no person looking at the source | 51 | 0 |

The 27 value rows: 8 site type, 8 gas collection, 7 gas use, 3 closure year, 1 area. Without gas
use (dropped 30 October), 20.

## Build order

1. **Response format and prompt:** the `planned` field; planned closure years allowed.
2. **Sorting:** automatic approval and 719337d's rule removed; the key-value list; `value_check`.
3. **`Site_Review`:** every pair, value rows, warnings against stronger sources, map and quote
   links, folding, greying out.
4. **`accept-reviews`:** value rows and corrected values.
5. **Docs:** README, the design doc's decision log, and the 21 October plan's status.
6. **A 10-site Mexico test** (batch 3), for Hugh to review as an SME before batch 3 runs.

## As built

On branch `sme-value-checks`, cut from `smes-check-key-values` (this plan). Build steps 1–4 and 6
are done; step 5 (README, design-doc decision, the 21 October plan's status) waits until Hugh has
tried the sheet. 1,133 tests pass.

- **Response format:** a value gives `planned` (true only for a planned `closing_year`). The prompt
  allows a planned closing year and still excludes every other future year.
- **`normalize_values.py`** holds the conversion, mapping and range checks the search's values went
  through in `arbitrate.py`, so a `corrected_value` is checked by the same code.
- **Site_Review columns,** left to right: our site; the source; where it places the facility;
  `flags`, `would_load`, `evidence`; the SME's six columns; then `review_id` and `check`, which
  decisions are read back by. Our site's id and name stay in view when scrolling right. A value
  row's `source_title` reads "↳ check closing_year", and its `source_url` shows "open the page at
  the quote".
- **Where the sheet differs from the plan:** a strong source with no warnings isn't specially
  marked; the flags column and the tier say enough.

### The 10-site Mexico test (`outputs/runs/test10-mex`, gpt-5.4, 30 October)

Ten batch 3 sites: six large ones (SIMEPRODE, Los Laureles, Puebla, Mérida, Bordo Poniente,
Tlalnepantla) and four small ones, two with generic names. About $2.40 in all ($0.22 a site, plus
one gas follow-up).

| | |
|---|---|
| Source rows | 32 (12 Tier 1–2, 20 Tier 3–5) |
| Value rows | 9 (5 without gas use, dropped since) |
| Values that would load | 68 |
| Sources placing the facility | 0 of 32: none stated usable coordinates or an address we could place, so the 5 km check never ran |

What it showed:

- **A planned closure came back as planned:** Puebla's Chiltepeque landfill, "useful life of five
  years, equivalent to 2030", flagged on its value row.
- **A generic name matched a strong source about another place.** Site 6086, "BASURERO MUNICIPAL",
  got a Tier 1 state congress page about Tlapa de Comonfort's dump; Tlapa is about 60 km from our
  point. The
  agent called it "consistent with the site context area". Under the 21 October plan it would have
  loaded unseen; here an SME sees it, but with no distance to go on. The test named its sites, so
  it searched this one, but a batch run skips generic names (20 October). Even so, that skip knew
  only English words, and would have searched all 417 of Mexico's sites named just "BASURERO
  MUNICIPAL". Since 30 October it knows Spanish words too, as well as English words that only
  describe a kind of site ("Municipal Landfill", "Open-Air Dump"), so batch 3 skips 667 of its 2,220
  sites, not 8.
- **Two of the four small sites found nothing.**

## Open questions

1. **Planned closure years reach ingestion as `closing_year`,** with nothing to say they are planned.
   Is that what ingestion should load, or should planned years stay out of `accepted_values.csv`
   until it can tell them apart?

## Settled

- Strong sources' values are trusted as read, for every attribute (29 October).
- SMEs can give a corrected value, optionally (29 October).
- `accepted_values.csv` doesn't record who checked a value (29 October).
- Batch 1 is out of scope for now (29 October).

## Accepted risks

- **A strong source's values load once its match is approved.** In the 10-site test, 8% of loaded
  values were wrong even from the right facility, mostly planned or permitted figures read as
  actual.
- **An SME checks a value against the quote** unless they open the page. A quote not found on the
  page is flagged.
- **A corrected value is loaded under the source it corrects,** with that source's link and tier,
  though the figure is the SME's.
- **More SME work on matches:** every source is a row. Most strong sources are quick, as the
  regulator or operator names the site.
