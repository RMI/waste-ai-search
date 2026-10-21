# AI search: file each source under the site it is about, and review everything

**Status:** agreed 20 October 2026, with both decisions below taken as recommended. Nothing
built yet. To be built before batch 2.
**Date:** 20 October 2026

## The problem

The search finds real data about real landfills, but files it under the wrong site. Everything
the agent returns is filed under the site it was asked about. Neither this repo nor ingestion
checks that a source is actually about that site. Batch 1 (1,931 sites):

- **Pages were filed under many sites.** 501 URLs were filed under more than one site. Brandon's
  landfill page went under 14 sites up to 1,716 km apart, 12 of them named "domestic landfill".
- **One site's answer is a bundle of sources.** A site that returned anything cites a median of 4
  different pages, and 91% use different pages for different values. So each source needs its
  own check, not one check per site.
- **The only check rarely runs.** The 5 km identity gate needs the agent's own found coordinates.
  Those came back for 276 sites, and for 191 of them the only source was a map mirror such as
  Mapcarta. Mirrors copy OpenStreetMap, which is mostly where our sites came from.
- **Wrong values skipped review.** 2,932 values were accepted automatically, including all 131
  won by a mirror.
- **Ingestion doesn't check either.** Ingestion PR #264 attaches each AI search row to the
  wastemap_id it was searched for.

## The workflow

| # | Step | In short |
|---|---|---|
| 1 | **Pick sites** | Skip names that give a search nothing to look for ("domestic landfill", "OpenStreetMap Landfill"; measured in PR #23). US and Brazil stay excluded. |
| 2 | **Search** | The agent researches the site as today, but reports each **source** with the facility name and location as the source writes them. Map mirrors are dropped. |
| 3 | **Locate** | Code turns each source's location into coordinates: parsed when it gives coordinates or a grid reference, geocoded when it gives an address. |
| 4 | **Match** | Each source is filed under the site of ours it is about, by location, with the name as a check. It may be filed under the site searched, another of our sites, a likely new landfill, or the site searched with a "no location" flag. |
| 5 | **Review** | For each (matched site, attribute), every source is pooled into one decision. **Nothing is accepted automatically.** SMEs see why each value was filed where it was, and can re-file it. |
| 6 | **Hand off** | Reviewed values load through ingestion under the matched site's wastemap_id, and consolidation's source ranking decides. New-landfill candidates and possible duplicates go out as lists. |

## The rules

### 2. Search

- **Each source reports the facility it describes:**
  - `facility_name_in_source`, the name exactly as written, used for matching;
  - `stated_location`, coordinates, a grid reference or the site's street address, copied as
    written and never worked out;
  - `stated_location_kind`.

  Each value keeps the fields the code reads today: unit, value_basis, confidence_score, value_date,
  evidence_summary, search_terms_used and quoted_evidence_short.
- **The prompt's "identity first" section changes.** Today the agent returns nothing unless it is
  sure it found this facility. Instead, it reports what each source says the facility is, and code
  decides. It still researches only the site asked about, so a registry page doesn't turn into
  values for every site it lists.
- **The found name stays specific (WP-558).** A site's `found_facility_name` value is built from its
  sources' names, with the municipality or operator added where the source gives only a generic
  name.
- **The prompt gets our coordinates rounded to 2 decimals (about 1 km),** plus the municipality.
  That is enough to search with, and nothing worth copying. As a backstop, a stated location equal
  to the prompt's coordinates counts as no location. In batch 1 the agent copied our exact OSM
  point six times and credited it to an Ontario registry.
- **Two map sites are dropped by website.** Dropped citations are counted in
  `parse_warnings.csv`.
  - **mapcarta.com copies OpenStreetMap.** All 319 batch-1 citations trace to the OSM way in
    the URL: coordinates to within a metre, names and operators word for word, typos included.
    Nothing came from anywhere else, and some of it was stale OSM. 30 of the citations pointed
    at a different landfill.
  - **maptons.com is an unattributed scrape of business listings,** probably Google's. 27 of
    its 36 coordinate pairs were more than 2 km from the site searched.

  Sites are not dropped by the agent's source-type label, which it applies unreliably:
  asbestossafety.gov.au was labelled a map directory 38 times. Wikimapia is an independent
  crowd-sourced map, not a mirror, so it stays as a weak source.
- **Two sites copy government data and need care:**
  - **wastebook.co.uk** republishes the Environment Agency register. Its coordinates are postcode
    centroids, so they are never used as a location. Its "issued" date is a permit date, not a
    closure date.
  - **landfills.ca** republishes Statistics Canada's infrastructure database.

  The agent is told to cite the government source where it can.
- **One reader converts any cached response,** old or new, to the source-first shape. The link
  check, the run log, validation, the merge of gas follow-up passes and retry detection all go
  through it.

### 3. Locate

| Location given | Becomes | Used for |
|---|---|---|
| Coordinates with 3 or more decimals | Parsed locally | Matching, and a location value |
| Coordinates with fewer decimals | Parsed locally | A hint only |
| UK or Irish grid reference, 100 m or finer | Converted locally (`pyproj`) | Matching, and a location value |
| Street address | Geocoded (Azure Maps). PO boxes skipped. | Rooftop level: matching and a value. Street level: matching only. |
| Anything else, or a copy of the prompt's coordinates | No location | — |

- **Coordinates and an address together:** the coordinates win. If the address lands more than
  2 km away, the location is treated as doubtful and not used.
- **A facility name is never geocoded.** Geocoders find landfills by name from OSM, which would just
  give us our own data back.
- **Geocoding runs during search,** next to the link check, and its results are cached in
  `source_locations.json`. Arbitration stays offline. A `locate --run-id` command backfills it, as
  `check-links` does.

### 4. Match

Distance is measured to a site's approximate edge, using its area (capped at 2 km), the way CJ's
entity-linkage PR (RMI/waste_data_ingestion_pipeline#225) does.

| The source has | Rule | Filed under |
|---|---|---|
| A location | Exactly one of our sites within 1.5 km | That site, whether or not it was the one searched |
| A location | Two or more within 1.5 km | The one whose name fits. If neither fits, the site searched, flagged, and the pair goes on `possible_duplicates.csv`. |
| A location | None within 1.5 km, but its name matches the site searched within 5 km (2 km on a single shared word; 25 km where our location is flagged inexact) | The site searched. Its location goes to review as a coordinate correction. |
| A location | Nothing of ours that close | No site. Listed in `new_facility_candidates.csv`. |
| No location | — | The site searched, flagged **no location**. The flag also says if the source's name fits the site searched, fits another of our sites in the same country within 200 km (named, ties included), or the same page was cited for other searches. |

- **The same page counts once per facility it names.** A source is its exact URL, query string
  included (today's cleaning drops it and merged 103 different NRW documents), plus the name it
  gives. Cited by several searches, a located source matches once and is filed once.
- **Names are a check, not the main test.** Name alone merges distant sites: "Eastview Landfill"
  is both a Brandon and a Guelph site, 1,716 km apart. Similarity uses distinctive words, as in
  #225, with more generic words added: tip, refuse, depot, grounds, nuisance, lieu, enfouissement,
  technique, LET. That needs agreeing with CJ, so both repos mean the same by "same name".
- **Only sites in this run's selection get values filed under them.** A match to another batch's
  country, or to the US or Brazil, goes on a list, not the Review_Queue. #261 stops on a facility
  with values from two runs.
- **A site's trust still decides what is asked of SMEs.** Where our name or coordinates come from a
  Tier 1–2 source (about 10,600 names, and 4,063 coordinate pairs), a source's name and location
  are used for matching, but are not queued as values to review.
- **"Not a Waste Facility" is filed only under the site searched,** and only if that site was offered
  the check. Otherwise it goes to Leads, never to the new-landfill list.
- **Removed:** the 5 km identity gate, the 1 km "same place" rule (`settle_near_coordinates`), and
  PR #24's 0.5 km rule. Matching replaces them.

### 5. Review

- **One decision per (matched site, attribute).** All sources filed under a site, from any search,
  go into one `resolve()` call, and the queue keeps one row, with the losing sources in its
  evidence summary. This matters downstream: #261 stops on two values for one facility, attribute
  and year.
- **Nothing is accepted automatically.**
  - A value that differs from ours goes to the Review_Queue whatever its tier.
  - A value equal to ours is a confirmation. It is kept in `evidence.csv` but not queued and not
    loaded, so the AI search doesn't become the credited source for a value nobody reviewed.
  - "Not found" rows stay out.
  - `resolved.csv` is written header-only, which #261 accepts.
  - The tier floor and the Leads route for weak sources stay as they are.
- **Review_Queue changes:**
  - `searched_site_id`: which search found the value.
  - `match`: the rule from step 4 and its reason, e.g. "The source places it 0.2 km from this site
    and 47 km from the site searched."
  - `facility_name_in_source` and `stated_location`.
  - `corrected_site_id`, which the SME edits to re-file a value under another site. Where several
    candidates exist, it offers them as a dropdown.
  - Every site column (site_id, names, country, baseline) describes the matched site.
    `searched_site_id` is the only one that describes the search.
  - A colour for rows filed under another site, rows with no location, and ties.
- **The rejection reason gains two values**, with plain hyphens:
  - "Wrong facility - another site in our database"
  - "New facility - not in our database"

  "Wrong facility" stays, for batch 1's workbook.
- **The gas follow-up still runs inside `run`.** It reads unreviewed `has_landfill_gas_collection`
  findings, by matched site, but only for sites in the run's selection.
- **The standardized CSV, its `.load.sql`, and their Run_Config count are deleted.** They are built
  before review, and #261 replaces them.

### 6. Hand off

- **Reviewed values** load through #261. A location is still one `found_location` row ("lat, lon",
  from #24), now with a `location_basis` column. #261 sets `is_location_exact` from it:
  - TRUE for stated coordinates, a grid reference, a rooftop address or an SME correction;
  - street-level locations are never offered as values.
- **The AI search ranks below every regulator and above Waste Atlas, GPW and OSM** (#264). A
  permit's coordinates 0.3 km from OSM's are therefore a value like any other, and win once
  validated.
- **Two lists go to the ingestion team,** since ingestion has no path for them:
  - `new_facility_candidates.csv`: sources that matched none of our sites, plus rows an SME marked
    "New facility". Each has the URL, name, stated location and derived coordinates, our nearest site
    and its distance, and the search that found it.
  - `possible_duplicates.csv`: pairs of our sites that one source fits equally well, or that two
    searches both cited for one page. It goes to CJ, whose #225 tackles E-PRTR re-registrations.

## Changes to Hugh's open PRs

| PR | Change |
|---|---|
| waste-ai-search#24 | Fold in: keep the single `found_location` row and `corrected_value_date`; drop the 0.5 km rule |
| waste-ai-search#23 | `name_yield` counts rejection reasons by prefix ("Wrong facility…") and credits findings to `searched_site_id` |
| waste-ai-search#26 | Treat the new Review_Queue columns as optional; re-key `searched_site_id`, `corrected_site_id` and the two lists |
| ingestion #261 | Add the new Review_Queue columns as optional, in the loader and in `create_raw_tables.sql`. Add the two rejection reasons. Read `corrected_site_id` and `location_basis`. Note that confirmations no longer load. Keep `CONFIRMING_KM`: it only affects batch 1's two-row layout, whose nearby coordinates mostly came from mirrors. |
| ingestion #262, #264 | None |

## Decisions (Hugh, 20 October 2026)

1. **Batch 1 is re-arbitrated under these rules before it loads.** That means no new searches, but
   its 2,932 auto-accepted values go to SMEs. The alternative was loading it as it stands, minus
   the values a mirror won.
2. **The prompt gets our coordinates rounded to 2 decimals.**

## Risks

1. **How many sources will give a location is unknown, and it decides how much this helps.** In
   batch 1, only 4.6% of official sources' quotes held an address or coordinates. The agent wasn't
   asked for them, so the real figure should be higher. If it stays low, most values take the "no
   location" path, filed as today but flagged for SMEs.
2. **SME workload rises.** With no automatic acceptance, most values go to SMEs. The live test gives
   a per-site figure. Batch 2 is 856 sites before unnamed ones are skipped.
3. **Office addresses.** Permits often give the operator's address. The prompt asks for the site's
   address, and rooftop-only values limit the damage.
4. **Blocked on setup.** The live test needs the new Foundry agent (RMI-PROJECT-WASTEMAP-SUB). Real
   runs need an Azure Maps account, which is IT's to create. For the test, Nominatim (OSM's free
   geocoder, one lookup a second) is enough.

## Testing

1. **Hand-built fixtures** for the known cases, with per-source names and locations: Brandon and
   Guelph Eastview, West Perth/Mitchell, Thunder Bay (the copied coordinates), and Cowpen Bewley and
   Foxhall (UK duplicates).
2. **A batch 1 replay, as a plumbing check.** Batch 1 has few located sources. A replay by the
   review of this plan filed 9 misfiled pages under the right site: Trail Road, Otter Lake,
   Fredericton and 6 others. Its assertion: never two rows for one (site, attribute).
3. **A live test on about 30 batch-1 sites**, the known cases included, for about $8 of Bing. It
   measures:
   - the share of sources that give a location;
   - review rows per site;
   - attribute yield against batch 1 under the same 6-search cap;
   - response size, token use and timeouts.
4. **The existing tests** for the identity gate, the near-coordinate rule, trusted names and
   coordinates, Brazil, WP-525 promotion and the follow-up are rewritten on purpose. So are the 15
   files that build attribute-first payloads.

## Build order

Separate PRs, each useful for batch 2 on its own:

1. Drop map mirrors by website, and dedupe sources on the exact URL.
2. Skip unsearchable names (from #23).
3. Turn off automatic acceptance, and remove the near-coordinate rules here. Delete the
   standardized CSV. Fold in #24.
4. Source-first output, rounded prompt coordinates, and the locate step. **The live test runs
   here**, and decides whether step 5 is worth building as specified.
5. Matching, pooled review, the new Review_Queue columns and rejection reasons, the two lists, and
   the matching #261 changes.
