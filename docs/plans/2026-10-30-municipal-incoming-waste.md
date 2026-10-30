# AI search: incoming waste counts municipal waste only

**Status:** built on branch `sme-value-checks`, not merged. Agreed with Hugh on 30 October 2026.
**Date:** 30 October 2026

## In short

The standardized spec's "Municipal waste only" rule (on `waste_data_ingestion_pipeline`'s `main`)
says every transform takes non-municipal waste out as far as its source allows:

- A site taking mainly other waste is typed `Non-Municipal Waste Landfill`.
- Where the source shows how much of a site's tonnage is municipal, only that share is kept.
- Where it doesn't, the tonnage stays as reported.

AI search now follows the same rule for `annual_incoming_waste_metric_tonnes`:

| What the page says | What happens |
|---|---|
| Nothing about what the amount is made of | Taken as municipal; loads as written. The usual case. |
| A split ("120,000 t, of which 20,000 t construction debris") | The agent returns the municipal part, 100,000 t. |
| The amount is mainly other waste ("20,000 t/yr of quarry tailings") | The agent returns it with `not_municipal = true`. Listed as not used ("not municipal waste"); never loads. |

On Site_Review, an SME checks an incoming-waste amount from a Tier 3-5 page like any key value:
an amount of rubble, soil or tailings is marked Wrong, so it loads nothing, or just its municipal
part is typed in `corrected_value`. No new decision.

A site taking mainly other waste is typed `Non-Municipal Waste Landfill` (commit a3af038).

## Why not bulk_waste_type

`bulk_waste_type` was a separate value: first what a whole site mainly takes, then (3054aac) what
an incoming waste amount is made of. As a separate value it had to be matched back to the right
amount, from the same source and year, which neither `accepted_values.csv` nor the loader could do
cleanly. Marking the amount itself, as `planned` marks a closing year, needs no matching. So
`bulk_waste_type` is retired: no longer asked (`UNSEARCHED_ATTRIBUTES`), and a value in a response
cached before is listed as not used ("no longer asked").

## How other sources compare

- **US GHGRP:** on `main` the transform still loads each filing's total tonnage. Hugh's open
  `waste_data_ingestion_pipeline` #244 takes out C&D and inerts, from each filing's own split
  where it has one and from estimated shares (0.87 and 0.74) where it doesn't. AI search estimates
  nothing: where a page gives no split, the amount stays as written, as the spec says.
- **LMOP:** loads its acceptance rate as reported, with no split, as the spec says.
- **Sludge:** the spec counts sludge as other waste; #244 counts sewage sludge as municipal, as the
  Climate TRACE pipeline does. AI search follows the spec.

## Ingestion

New runs carry no `bulk_waste_type`, and a not-municipal amount never reaches
`accepted_values.csv`, so the loader needs no new column. The loader in #261 types a site
Non-Municipal from its `bulk_waste_type`; with none in new runs that rule no longer fires, and can
be taken out of #261 when convenient.

This changes what the search asks and returns, so it must be in place before batch 3.
