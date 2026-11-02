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
outputs/runs/<run_id>/consolidation_snapshot/
outputs/runs/<run_id>/raw_foundry_responses/site_<site_id>.json
outputs/runs/<run_id>/<run_id>_review.xlsx
outputs/runs/<run_id>/found_values.csv
outputs/runs/<run_id>/accepted_values.csv     (written by accept-reviews)
```

The rest are listed under [Outputs](#outputs).

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

**A run reads one consolidation run.** `consolidated_facility` keeps every consolidation run side
by side, and so do the value resolution ledger and `entity_linkage.crosswalk`. Read whole, they
would blend runs: sites a newer run dropped would come back, and one site's values could come from
different runs. So a seed reads exactly one run, picked the way the Climate TRACE pipeline picks
it, so both read the same sites:

- `CONSOLIDATION_RUN_ID` in `.env` pins one run exactly;
- otherwise the newest run for `CONSOLIDATION_STRATEGY` and `ENTITY_LINKAGE_STRATEGY`, both
  `legacy_replica_v1` by default. The newest run overall can be an experiment against another
  crosswalk.

The run prints which one it read, and `consolidation_run` in the workbook's Run_Config records it.
A pinned id that isn't there, or no run of the strategy, stops the run rather than falling back to
another. Runs seeded before this read every run in the table; their Run_Config says "a blend of N
runs" when there was more than one.

**A run reads the database once.** The first read is written to
`outputs/runs/<run_id>/seed.csv`, and every later phase of that run reads the snapshot instead of
querying again — both arbitrations, the refresh between passes, a resumed search, and a standalone
`arbitrate`. So every phase sees the corpus the search ran against, `arbitrate` never needs the database (it makes no agent calls; with blob storage configured it pulls from blob, so pass `--local` to stay fully offline), and
`seed_source` in the workbook's Run_Config records where the run is pinned.

A run id is therefore pinned to its corpus. To seed afresh, use a new `--run-id`. Only the
search seeds a run: `arbitrate` and `refresh-seed` on a run with no `seed.csv` (a mistyped
`--run-id`, or a run from before seeds were pinned) stop and say so rather than read today's
database, which may no longer match what was searched.

A run seeded with `--input-csv` is pinned the same way: the supplied file is copied, byte for byte, into the run's `seed.csv` on first use, and every later phase reads that copy — so the run can still be re-arbitrated after the file is moved or deleted. Passing a *different* file to an existing run id stops with an error rather than searching one corpus while the snapshot records another; use a new `--run-id`. The one exception is the pass-2 follow-up's `refreshed_seed.csv`, which is read as given once the original snapshot exists and never replaces it. Any other CSV is pinned and checked against the snapshot, even one stored inside the run directory.

`--input-csv` remains for two cases: pinning an exact corpus, and the gas-collection follow-up,
which rewrites the seed between passes into the run directory.

**A run also keeps a copy of the consolidation tables it searched (F32).** Since
RMI/waste_data_ingestion_pipeline#196, a facility keeps its `wastemap_id` across consolidation
rebuilds, and that is every output's `site_id`. The column keeps the name `internal_facility_id`,
so older runs still read. Runs from before then used an `internal_facility_id` that every rebuild
reassigned. Before seeding from the database, a run saves
`outputs/runs/<run_id>/consolidation_snapshot/`, which goes to blob with the rest of the run:

| File | What it gives you |
|---|---|
| `consolidation.consolidated_facility.csv.gz` | The baseline the search filled gaps in, with all facility-year rows |
| `consolidation.value_resolution_ledger.csv.gz` | For each baseline value, the source and the record in it (`data_source`, `data_source_facility_id`) |
| `entity_linkage.crosswalk.csv.gz` | Every source record linked to each facility, whether or not it supplied a value: the crosswalk run that consolidation consumed |
| `manifest.json` | When it was taken, the consolidation run (id, strategies, crosswalk run, date, how it was chosen), and each table's row count and checksum |

`data_source` + `facility_id` in the crosswalk maps a run's `site_id` to its raw records. For a run
from before #196 it is the only way onto today's `wastemap_id`. It is also how a later run can
search only what changed. Each table is filtered to the one run, every country rather than just
those searched, in one read-only transaction; the run stops if consolidation is rebuilt while it
is seeding. Blob transfers go in stages - data, then
`manifest.json`, then `seed.csv` last - so a resumed run whose copy lacks the manifest, or a file it
lists, stops rather than carry on without its consolidation state, and a seed with no snapshot
folder at all can only be a run that never had one. A run seeded with `--input-csv` reads no
database, so it takes no snapshot.

A run therefore needs database access. Without the VPN, seed a file first with
`scripts/seed_metadata_search.py` and pass it with `--input-csv`.

## Link checking

At the end of the search phase, every cited URL is fetched once and given a verdict, saved to
`link_check.json` in the run folder. Citations of the two dropped map sites (see
[Source credibility](#source-credibility)) are never used, so they are not fetched. Ported from
refining-ai-search (RDP-63):

| Verdict | Meaning | Effect |
|---|---|---|
| `OK` | 200 | Normal |
| `Broken` | 404, 410 or a redirect loop | Its values are not used (`dead link` in `found_values.csv`) |
| `Unverified` | 403, 429, 5xx, timeout, failed connection, a name that doesn't resolve, TLS error | Left alone — usually bot-blocking, not a dead page |

A broken URL is retried with a literal comma for `%2C` and with the trailing slash toggled; a
respelling that works replaces it. Existing escapes are never decoded otherwise.

**Only public addresses are ever contacted.** Cited URLs are untrusted and are fetched from a machine that is often on the VPN, so every hop — redirects included — is resolved first and refused if any address is private, loopback, link-local (including the cloud metadata address) or otherwise non-public. The connection then goes to the address that was checked, so a second DNS answer cannot redirect it. A refused URL is `Unverified` and never fetched; a malformed one is `Unverified` too, rather than stopping the check. So is a name that doesn't resolve, with its own status (`DoesNotResolve`), since a name server can fail for a while.

**An Unverified page can be fetched again.** `check-links --retry-unverified` fetches once more,
with 45 seconds to answer rather than 15, each URL left Unverified by a timeout, a failed connection,
a name that didn't resolve, a 429 or a server error, and keeps its new verdict and text. Never a 403:
that site refuses automated readers on purpose. No headless browser is used either: these sites
refuse those too, and one would resolve names itself, past the address check above. Of the 32 pages
the October 2026 model tests couldn't read, 20 were 403s, 5 timeouts, 2 server errors, 1 a name that
didn't resolve and 4 scanned PDFs.

The fetching happens during **search**, which needs the network anyway; `arbitrate` only reads
`link_check.json`, so it stays offline. A dead source's values are not used, but the same value from
another, working source still is.

```bash
uv run waste-ai-search check-links --run-id <run_id>   # backfill an older run, then arbitrate
uv run waste-ai-search check-links --run-id <run_id> --retry-unverified   # fetch again what may pass
uv run waste-ai-search run --no-link-check ...          # skip checking
uv run waste-ai-search arbitrate --keep-broken-link-evidence ...  # let dead sources stay usable
```

Run_Config records `links_checked`, `links_broken`, `links_repaired` and `broken_link_route`, so
every workbook states which policy produced it.

### The text of each page that loads

**A page that loads is also read, and its text kept** in `page_text/` in the run folder: one gzip file
per exact URL, named by its `source_id`. The text comes from the same response the verdict does, so
it costs no extra request. Up to 25 MB and 60 seconds of each page are read.

| Page | What is kept |
|---|---|
| HTML | Its visible text, without scripts and styles. A page with little text is noted: it may be built by JavaScript, which the check can't run. |
| PDF | Its text layer, up to 300 pages. A scanned PDF has none, and says so. |
| Spreadsheet (`.xlsx`, `.xls`, `.csv`) | Every cell, up to 500,000 across the workbook. |
| Word `.docx`, plain text | All of it, up to 8 MB of a document's markup. |
| Anything else, or binary content served as a web page | Nothing; `link_check.json` says why. |

`link_check.json` records, beside each verdict, the text file and what kind of page it was, or why
no text was kept, or why it was cut short (its size, the 60 seconds, or a broken connection). The
next `check-links` reads a page again where it was checked before text was kept, its text file has
gone, or its read was interrupted, and keeps its verdict as it was, since a run may already have been
reviewed on it. Nothing else is fetched twice. `--no-page-text` on `search`, `run` and `check-links`
keeps verdicts only.

**Arbitration checks each source against its page, offline** (`on_page.py`). The agent quotes each
source and copies where it puts the facility, but both are its own report; the prompt asks for each
quote word for word, in the source's own language. Both sides are compared in plain form:
- lower case, no accents or invisible characters;
- punctuation and line breaks as spaces;
- a word a PDF hyphenated across a line read whole.

They are compared again with numbers whole ("45,000" is "45000") and units apart ("10ha" is "10
ha"), "%" read as "percent", and road abbreviations written out ("Rd" is "road"). Scripts written
without spaces, such as Chinese, Japanese and Thai, are matched anywhere in the text.

| Result | Means |
|---|---|
| `found` | Every part of the quote, split at an ellipsis, is on the page as written. A stated coordinate counts where the page gives it to as many decimals or more, trailing zeros aside, or puts a point within 250 m of it in any form; a grid reference is compared without its spaces. |
| `partly found` | Not as written, but 80% of its words and every number in it are on the page, or one of its two coordinates is. A quote with a figure the page doesn't have is `not found`, however many of its words are there. |
| `not found` | Neither. |
| `not in the part read` | Not found, but only part of the page was read (a long PDF or spreadsheet, a cut body), so it may be in the rest. Not flagged. |
| `no page text` | No text was kept: the page didn't load, refuses automated reading, is a scan, is built by JavaScript, or wasn't checked. Not flagged. |

A quote of fewer than three words ("Open", "Status Operational") is not checked: finding it would
say nothing. Where a source states both coordinates and an address, the one its point was read from
is checked, since an address on the page says nothing of coordinates beside it that aren't.

`found_values.csv` records both results for every value, in `quote_on_page` and `location_on_page`.
On the Site_Review tab, a source whose quote is not on its page, or only partly, is flagged ("quote
not found on the page", naming the attributes), and so is one whose address or coordinates aren't.
Nothing is dropped or downgraded for it, since a page can say something in a way the check can't
follow, in an image or a map, say. An auto-approved source isn't on Site_Review, so its page checks
are only in `found_values.csv`. Run_Config counts `page_text_kept`, `quotes_on_page` and
`locations_on_page`.

**Is a page-reading agent worth adding?** The search agent sees only what Bing hands it, which may be
part of a page. A second pass, a model reading each kept page for the facility's address or
coordinates, would recover what the agent left out. This read-only script measures how often there
is something to recover:

```bash
uv run python scripts/measure_missed_locations.py --run-id <run id> --out missed.csv
```

It looks in each kept page for coordinates or a grid reference within 5 km of a site whose search
cited the page, or a street address (a civic number before capitalised words and a road word, or a
road's name before its number). It counts the pages where the agent stated no location although the
page has one. A point 5 to 300 km away is counted apart, as another facility's, since a regional
register lists many, and so is a page with only a postcode. The count is an upper bound, since an
address may be an office's, so spot-check `missed.csv` before deciding.

## Locating each source

The agent reports where each source puts the facility, copied as the source writes it, in two
fields: `stated_coordinates` (coordinates or a grid reference) and `stated_address` (see
[What the agent returns](#what-the-agent-returns)). Right after the link check, the search phase
turns them into a point and says how far it can be trusted, as its basis
([`locate.py`](src/waste_ai_search/locate.py)):

| The source states | Becomes | Basis | `coordinates_from` |
|---|---|---|---|
| Coordinates with 3 or more decimals (or seconds) | Parsed locally | `coordinates` | `stated` |
| A UK OS or Irish grid reference, 100 m or finer, ITM metres labelled ITM, or an easting and northing each labelled (`E 531090 N 180600`) in the UK or Ireland | Converted locally (`pyproj`) | `grid reference` | `grid reference` |
| An address placed on its building or address point | Geocoded | `rooftop` | `address (building)` |
| An address placed only on its street | Geocoded | `street` | `address (street)` |
| Coordinates with fewer decimals, or a coarser grid reference | Parsed locally | `hint` | blank: too approximate to show, but still measured for the 5 km check |
| Anything else: a town or postcode match, a major road, a PO box, a kilometre marker | — | `none` | blank |

**What the point is for.** A source's location never replaces ours. It is only used to check that
the source is about our site. Three columns of `found_values.csv` and the Site_Review tab carry it:

- `source_coordinates`: the point, as `lat, lon`, which Google Maps finds when pasted into its
  search box;
- `coordinates_from`: where the point came from, as in the table. A point on a street can be a
  kilometre or more from the address;
- `distance_km`: how far the point is from our site's coordinates.

A Tier 1–2 source placing the facility more than 5 km from our site goes to SMEs rather than being
approved automatically (see [Filing and approving each source](#filing-and-approving-each-source)).
A source that states no location we can read, or a site of ours with no coordinates, is not
checked. Where a source gives both coordinates and an address, the coordinates are used, unless the
guards below throw both out.

**Rough points are still measured.** Coordinates too coarse to show (a hint), and a doubtful pair's
coordinates and address, give no `source_coordinates`, but each still says roughly where the source
puts the facility. The 5 km check measures them: a hint is far only beyond 5 km plus its own
coarseness, and a doubtful pair is far if either its coordinates or its address is more than 5 km
from ours. `distance_km` then gives the farthest of them, `location_note` says why there is no
point, and `status_reason` says "roughly". A Tier 1 source stating `50.66, -97.20` for a site at
Brady Road, 100 km south, would otherwise be approved unseen.

Coordinates are read as `lat, lon`, in decimal degrees or degrees, minutes and seconds, with
hemisphere letters or words (`North`, `West`). `lon, lat` is read only where that is unambiguous: a
hemisphere or a label says so, or the first number is beyond 90. A header naming both axes, such as
`Lat/Long:` or `(lat, long)`, gives their order, and a well-known-text `POINT(lon lat)` is read
longitude first. In prose, a number followed by a unit (km, m, ha,
%) is a distance or an area, not a coordinate, and the first pair in range is the one used.

**West is often written O.** Spanish, Portuguese, French and Italian write west as O (`oeste`,
`ouest`, `ovest`), so a Mexican page's `98° 50’ 42.8” O` is west. In test10-mex-sample2 it was read
as east, placing Chicoloapan's page 15,289 km from our site instead of 0.5 km. In German and Dutch
O is east (`Ost`, `oost`), so in Germany, Austria, Switzerland, Liechtenstein, Luxembourg, the
Netherlands and Belgium it is read as east. A hemisphere written with its axis's name counts too,
for a number with no sign or hemisphere of its own: `98°50'42.8" longitud oeste`, `Longitude West:
97.1234`, `latitud sur`. "Sur" counts as south only there, since in French it means "on".

A labelled easting and northing (`E`/`N`, `Easting`/`Northing` or `X`/`Y`, in metres) is read in
the country's grid: the British National Grid in the UK, and in Ireland the Irish Grid or ITM,
whose numbers never overlap. A UK source that says Irish Grid, as Northern Ireland's do, is read
in Ireland's grids. Elsewhere the grid is unknown, so such metres are not read. The `E` is never
taken for an Irish Grid square, and where a text holds a licence number and a grid reference, the
reference 100 m or finer wins.

Three guards throw a location out, with the reason in `found_values.csv`'s `location_note`:

- **Copied from the prompt.** Coordinates equal to the rounded point the prompt gives. In batch 1,
  whose prompts carried our full point, the agent credited our exact OSM centroid to an Ontario
  registry six times. The prompt no longer carries that point, so a source stating it exactly is
  not a copy but a source that agrees with us, such as the regulator our seed came from. A
  response cached attribute-first, before the agent answered source by source, came from a prompt
  with our full point, so there a point within 10 m of ours is still copied: batch 1's six copies
  were each within a few metres. Left in, a copy would pass the 5 km check whatever the source is
  about.
- **wastebook.co.uk.** Its coordinates are postcode centroids.
- **A doubtful pair.** Where a source gives coordinates and an address, the coordinates win; if the
  address lands more than 2 km from them, neither is used as the source's point, though both are
  still measured for the 5 km check. Coarse coordinates (a hint) give way to a located address that
  agrees with them: within 2 km plus the hint's own coarseness, about 0.8 km for 2 decimals. An
  address farther off, such as an operator's office, makes the pair doubtful too.

**A facility name is never geocoded.** Geocoders find landfills by name from OpenStreetMap, where
most of our sites came from, so it would hand our own point back. Only the stated address is
sent, with any leading facility name taken off, and text that is only a name is not sent at all.
The names are the source's own, any name an older response gave as `found_facility_name`, and our
site's `site_name` and `original_site_name`; `&` and `and` count as the same, and a name is taken
off only where a comma, a dash or a number follows it, so "Hart" never shortens "Hartland Avenue".
Text with no civic number in which a part ends in a word for a waste site is a name too, whoever's
it is: "Mapleward Road Landfill", "Highway 16 Transfer Station" or "Main Street Recycling Centre,
Brandon MB" is never sent, while "5405 Mapleward Road" is. The words are landfill, dump, tip, eco-centre, and site,
station, centre, depot, park or grounds after waste, refuse, recycling, transfer, disposal and the
like.

**A PO box is where post goes, not where the site is.** A PO box, `Box 219`, `C.P.`, case postale,
private or locked bag, `PMB`, `BP` and the like is taken out of an address, and what is left is
sent only if it still has a civic number or names a road: "29786 Rte. 2 PO Box 253 Wellington PE"
is sent as "29786 Rte. 2, Wellington PE", while "PO Box 1234, Thunder Bay, ON" is not sent. Where a
source gives two addresses, the second is tried when the first placed nothing.

**A kilometre marker, a distance or a major road places nothing.** Mexican landfills are often
addressed by a kilometre marker along a road ("Carretera Colotlán km 15.5"), and a geocoder can't
use one: it finds the road by name and returns whichever stretch it picked. In test10-mex-sample2
that put a Tier 1 page 129 km from Hasars. So text with a kilometre marker or a distance ("km
15.5", "kilómetro 12", "a 3 km del centro") is never sent, though its plainer form may be (below).
For the same reason, a match on a major road itself, its house number not found, is not used: a
motorway, trunk, primary or secondary road, which runs between towns or across a city. Each of the
four such matches in the October 2026 test runs was more than 5 km from the site.

**An address written as a sentence is sent again, plainly.** Sources write "765 33rd Street East in
Brandon, Manitoba" or "Reynolds Bach Drive, accessed from Eastern Hutt Road in Silverstream", and a
geocoder finds nothing. Where the words as written place nothing at street level, the address is
sent again without the words around it: "in" and "at" become commas, and directions ("accessed
from", "off Highway 11", "approximately 21 kilometers from"), a lot or unit on its own ("Lots 1 &
2"), "City of" and a Canadian legal land description ("on NW 1/4 17-10-18 WPM") are taken out. So
"3610 Victoria Avenue East on NW 1/4 17-10-18 WPM in Brandon, Manitoba" is sent again as "3610
Victoria Avenue East, Brandon, Manitoba". What is left is sent only if it is still a street with its
place: "3610 Victoria Avenue East" alone is not, since another town may have one. In the 10-site
test runs, it gave a point to 5 more of gpt-5.4's 42 sources stating a location (11 to 16), and 12
more of mini's 39 (6 to 18). Both answers are kept, so a re-run asks neither again. Most of these
points are on the street, not the building: where a geocoder has no house numbers for a road, it
returns a point somewhere along it, which can be a kilometre or more from the address. So such a
point is labelled `address (street)` in `coordinates_from`, for the SME to weigh.

### Geocoder

No geocoder is configured by default: addresses stay unlocated, with the reason "no geocoder
configured". Choose one with `WASTE_AI_SEARCH_GEOCODER` in `.env`, or with `--geocoder` on
`search`, `run` and `locate`. Each address is sent with its site's country.

| Geocoder | Settings | How a match is read |
|---|---|---|
| `azure-maps` | `AZURE_MAPS_SUBSCRIPTION_KEY`, or `AZURE_MAPS_CLIENT_ID` to sign in with Entra ID (`az login`) | [Get Geocoding](https://learn.microsoft.com/en-us/rest/api/maps/search/get-geocoding?view=rest-maps-2026-01-01), api-version 2026-01-01. An address matched to its rooftop or parcel is `rooftop`; an interpolated address, a block or an intersection is `street`. A match it had to broaden (`UpHierarchy`), an ambiguous one, or Low confidence is not used. The account is IT's to create. |
| `nominatim` | `NOMINATIM_URL` (the public instance if blank) and `NOMINATIM_USER_AGENT` (default `waste-ai-search (RMI)`, which names the application, never a person) | OpenStreetMap's geocoder, at most one request a second. An address point or building is `rooftop`; a minor road, or a house number Nominatim interpolated along its street (`place=house` on a way), is `street`, as with Azure Maps. A major road (motorway, trunk, primary, secondary) is not used. A mapped feature that carries the address, such as the landfill itself, is not used: it would be OSM's own point. Enough for the live test. |
| `none` | | Addresses stay unlocated. |

### `source_locations.json`

Every answer is kept in `source_locations.json` in the run folder, keyed by the stated text and its
country, never by site. So an address is looked up once however many sources state it, and a
resumed search or the follow-up pass looks up only what is new. An address kept as "no geocoder
configured" is looked up once a geocoder is set; a failed lookup is not kept, and is tried again
next time. Any other answer is kept, so to look addresses up again with another geocoder, delete
the file first. It is saved as the step goes, so an interrupted run keeps what it paid for.

Arbitration only reads it, so it stays offline. It parses coordinates and grid references itself,
which needs no network, so a run's coordinates are read even before it is located; an address
not yet looked up says "not located yet: run `locate --run-id`". Blob storage carries the file
like `link_check.json`.

```bash
uv run waste-ai-search locate --run-id <run_id>       # backfill an older run, then arbitrate
uv run waste-ai-search run --no-locate ...            # skip locating
uv run waste-ai-search run --geocoder nominatim ...   # choose the geocoder for this run
```

Run_Config records `locations_stated` and `locations_used` (the usable ones: coordinates, grid
reference, rooftop and street), each counted by basis, and `geocoder`: which geocoder answered,
`none configured`, `none needed (no address stated)` or `locate step not run`.

## Filing and approving each source

We trust the agent to read values off a page. What a person has to check is whether the page is
about our site. So the search files every source under the site it searched, approves Tier 1–2
values for empty fields automatically, and sends SMEs only the sources that need a look
([the plan](docs/plans/2026-10-21-approve-sources-site-by-site.md), design doc F41;
[`found_values.py`](src/waste_ai_search/found_values.py)). It all runs offline in arbitration, so
a change here can be re-applied to a finished run for free.

### Every source is filed under the site searched

**The agent decides whether a source is about the site it was asked about,** as in batch 1. It
returns only sources about that facility, and nothing if it can't confirm it (see
[What the agent returns](#what-the-agent-returns)). Our code doesn't move a source to another site.
A source's name, address and coordinates are only used to check that it is about our site; they
never replace ours.

**Decisions are made per site–source pair.** A pair is one of our sites and one source its search
cited. A source is one exact page (see [Outputs](#outputs)) under one facility name, compared
without case, accents or punctuation; a blank name counts as one name. A page with good data on
three of our sites is three pairs, each decided on its own, and each loads only that site's values.
`review_id` names a pair, and is the same each time the run is arbitrated, so an SME's decision on it
can be read back.

Until this plan, arbitration filed each source under the site of ours it was about, by distance
and name (`match.py`, design doc F38), and SMEs reviewed every value. Both are gone, and so is
`outside_run_matches.csv`: a source is only ever filed under a site the run searched.

### What becomes of each value

Every value found is written to `found_values.csv` with one of three statuses:

| Status | When |
|---|---|
| `auto-approved` | It fills an empty field, and its pair's source is Tier 1–2 with none of the exceptions below. It loads without review. |
| `SME review` | It fills an empty field, and its pair's source is Tier 3–5, or Tier 1–2 with an exception. It loads once an SME approves the pair on Site_Review. |
| `not used` | Anything else. `status_reason` says why: it confirms our value; it would change a value we hold; its link is dead or missing; it could not be converted or mapped; the agent itself marked it an excluded candidate; it says Not a Waste Facility of a site not offered that check; it is a planned closure year that has passed; it is one of the agent's unverified leads; or an auto-approved source of a better tier already gives every value its source gives (see below). |

**A source that adds nothing isn't reviewed.** One value loads per site, attribute and year, and
the best tier wins. So where an auto-approved source of a better tier already gives every value a
source would load, for the same site, attribute and year, approving that source could load
nothing: it is marked not used instead of going to SMEs. In the 10-site tests that was 6 of
gpt-5.4's 38 sources for review and 1 of mini's 22. A source that says the site is Not a Waste
Facility is reviewed all the same.

Tier 1 is a regulator, permit or operator; Tier 2 is government statistics, peer-reviewed or
intergovernmental data (see [Source credibility](#source-credibility)). A pair takes the tier of
its best-rated citation, so a follow-up pass that left a page undated doesn't lower it.

**Only fills of empty fields load.** A value that would change one we hold is listed as not used.
An empty field is our value for that site, attribute and year, left blank. A value's year is the
year its `value_date` starts with (1950 to the run's year), else the run's year. For a yearly
attribute (status, waste received, waste in place, waste depth, cover types and the gas figures,
as consolidation keeps them by year), the seed holds our value for its reference year only, so for
any other year our latest value stands in, and `status_reason` says so. Where our value for the
reference year was carried forward from an earlier one, we hold nothing for that year, so a value
found for it fills an empty field.

**A planned closure year that has passed isn't used.** The agent marks a `closing_year` the source
gives as planned or expected (`planned`). Where that year is before the run's, the plan can't say
whether the site closed: in October 2024 Milenio reported that El Huixmí would stop taking waste that
December, and it was still taking waste in October 2026. An actual closure has to come from a
source that reports one. Such a year isn't used even where it matches ours, so a Different site
doesn't weigh it again at the other site. A planned year still to come is used like any other
closing year: from a Tier 3–5 source it is flagged planned on its ↳ row, and from a Tier 1–2 source
it loads with its page, unmarked. `accepted_values.csv` has no column to say it is planned, so
ingestion reads it as the year the site closes.

**Three exceptions send a Tier 1–2 pair to an SME:**

1. **The same page and facility name are cited for two or more of our sites.** It is about one of
   them at most, unless they are one facility recorded twice. Each site's pair goes to SMEs, and
   the sites go on `possible_duplicates.csv` (unless the name is blank). For this check, names are
   compared by their distinctive words, so "The Eastview Landfill" and "Eastview Landfill site" are
   one name, though each is still its own pair. A page cited with no name for two sites counts as
   one name here too.
2. **The source places the facility more than 5 km from our site's coordinates.** This is batch 1's
   check, now run per source (see [Locating each source](#locating-each-source)). Coordinates too
   coarse to show, and a doubtful pair's coordinates and address, are still measured. A source
   stating no location we can read, or a site of ours with no coordinates, isn't checked, so its
   Tier 1–2 values are approved, as in batch 1.
3. **The source says the site is "Not a Waste Facility"** (see
   [Not a waste facility](#not-a-waste-facility-wp-525)).

**One value per site, attribute and year is loaded.** Where approved values disagree,
`accept-reviews` picks one: the best tier, then the newest `value_date`, then the value the most
sources give (a page cited twice counts once), then the agent's order. Tier always comes first,
however the values are dated. For an attribute that isn't yearly, it is one value per site and
attribute, as ingestion keys it, and `accepted_values.csv`'s `year` is then the winning value's own
year, for the record, not part of the key.

Run_Config counts `site_source_pairs`, `pairs_auto_approved` and `pairs_sme_review`, each with its
values. Pairs for review are split into Tier 3–5 and the Tier 1–2 pairs each exception sent.
`values_not_used` is counted by reason.

### What else is listed for ingestion

- **`possible_duplicates.csv`:** pairs of our sites whose searches cited the same page under the
  same facility name. They may be one facility recorded twice, as E-PRTR's re-registrations are
  (Cowpen Bewley, Foxhall), or one search may have claimed another site's source, as batch 1's
  Brandon page under 14 sites did. `reason` says the second is likelier where the sites are more
  than 5 km apart. A page cited with no facility name is left out: both pairs still go to an SME,
  but a blank name says nothing of two sites being one, and in the gpt-5.4 test Ontario's
  province-wide landfill gas dataset paired two sites 332 km apart.
- **`datasets_found.csv`:** pages that look like datasets, such as a regulator's list of its
  landfills. A search reports only the entry it used, so the rest of such a page goes unread, and
  every site on it pays its own search to find it again. Loaded once through ingestion, as E-PRTR
  is, it would cover them all. A page is listed when it names 2 or more facilities (names told
  apart by their distinctive words), places 2 or more (points within 2 km of each other count as
  one), or is a regulator, registry or statistics publisher's page found by 3 or more searches (a
  town's landfill page is found by every nearby search too).
- **`new_facility_candidates.csv`:** landfills none of our sites is. Only an SME's New site decision
  adds one now.

### On the 10-site tests

Arbitrated again from the cached `test10-gpt54` and `test10-mini` responses:

| | gpt-5.4 | mini |
|---|---|---|
| Site–source pairs | 61 | 41 |
| Auto-approved | 23 pairs, 51 values | 19 pairs, 51 values |
| To SMEs | 38 pairs, all Tier 3–5 | 22 pairs: 14 Tier 3–5, and 8 Tier 1 by exception: 6 for the same page and name, 3 more than 5 km away (one pair both) |
| Not used | 72 values, 63 of them the agent's unverified leads | 17 values, 14 of them unverified leads |
| Review_Queue rows before this plan | 102 | 90 |

In mini, Guelph's search cited five pages about Brandon's Eastview Landfill, all Tier 1–2. Three
were cited for Brandon as well (one as "The Eastview Landfill" there and "Eastview Landfill" under
Guelph), and two have addresses that put the facility 1,645 km away (one of them also cited for
Brandon), so four go to SMEs, and the three shared pages go to SMEs under Brandon too. The fifth gets
through: a Manitoba news release whose only location is "Brandon", cited only for Guelph. It is
auto-approved under Guelph.

**Accepted risks:**

- **Tier 1–2 sources with no location are approved unchecked,** as in batch 1. In the gpt-5.4 test,
  36 of the old review queue's 44 rows from Tier 1–2 sources had no usable source location.
- **Extraction errors load unseen.** In the tests, 8% (gpt-5.4) and 11% (mini) of the values that
  would have loaded were wrong, even from the right facility. Most were planned or permitted
  figures read as actual. The prompt now asks for actual figures, dates and features only -
  nothing planned, proposed, ordered or required, permitted, design or projected, and no future
  years - which those tests predate; a live run will show how much it helps.
- **The agent can claim the wrong facility.** The duplicate rule and the 5 km check catch most of
  what batch 1 showed. A Tier 1–2 page cited for only one site and giving no usable location gets
  through, like the Manitoba news release above. Its only location, "Brandon", is geocoded and
  cached at the town's point, 1,652 km from Guelph, but a town-level point isn't used for any check.
- **Values the agent itself doubts are auto-approved** (a decision for Hugh). Only a value it marks
  an excluded candidate is set aside. Of the 51 auto-approved values in each test, gpt-5.4 has 10
  the agent marked Inferred with Low confidence (9) or whose quote isn't on the cited page (1), and
  mini 10 (quote not found 7, Inferred/Low 2, Conflicting 2), all of which load: Guelph's
  `facility_status = Active` that the agent itself said conflicts with a decommissioned landfill,
  and a forecast `closing_year = 2028` at Moose Jaw, among them.
- **A pair takes its best citation's tier,** so a value only a weaker citation of the same page
  gives is approved at the better tier: in mini, the gas follow-up's undated citation of Cowpen
  Bewley's register (Tier 3) gives values approved at the first pass's Tier 1.
- **A tier comes from the agent's `source_type`.** A personal blog the agent called an operator
  disclosure is Tier 1 in gpt-5.4, and auto-approves.

## Storage and sharing

### Run results in Azure Blob Storage

Each run folder is mirrored to blob storage at `outputs/runs/<run_id>/` (adapted from
refining-ai-search RDP-52):

- **Before searching**, any earlier attempt at the same `--run-id` is pulled down, so responses
  already in storage are reused instead of paid for again. A local file is replaced only by a
  newer blob copy - another machine's later work, such as fresh link verdicts - and the run's
  `seed.csv` and `consolidation_snapshot/` never are, since they pin the run's corpus.
- **A search that fails or is interrupted still pushes** what it finished.
- **Raw responses go up as soon as a search pass finishes**, so paid-for results are safe even if
  arbitration fails; the whole folder goes up again after arbitration.
- `arbitrate` pulls first, so a run searched on another machine can be arbitrated here.

Blob is used **once it is configured** — set `AZURE_STORAGE_ACCOUNT` (signed in with `az login`) or
`AZURE_STORAGE_CONNECTION_STRING`. Until then every run says *"results stay local only"*. `--local`
or `WASTE_AI_SEARCH_LOCAL=1` keeps a run local regardless.

The account sits behind a firewall: off the `RMI-SP-FLEX-VNET` VPN, Azure answers
`AuthorizationFailure`, which looks like a credential problem but is not. The CLI says so and exits.

### Review workbooks in SharePoint

After arbitration, the review workbook is copied to `WASTE_AI_SEARCH_REVIEW_DIR/<run_id>/`, a
SharePoint folder synced locally by OneDrive, which uploads it. `--no-publish` skips this.

**An SME's work is never overwritten.** Every published file is fingerprinted in the run's
`published.json`. Republishing replaces a SharePoint copy only if nobody has saved it since; a copy
that has been edited, or is open in Excel, is left alone, and the new version is written beside it
with a timestamp.

## Run The Metadata Search

**One command does everything:**

```bash
uv run waste-ai-search run --run-id pilot10 --iso3 NGA PHL --limit 10
```

That searches, arbitrates, and — if any facility turns out to have a gas collection system the
seed did not know about — automatically runs a second, narrow pass for that facility's gas-capture
attributes and re-arbitrates. Same run id, same output directory, no reseeding by hand. Pass
`--no-followup` to stop after the first pass. Nothing has been reviewed by then, so the follow-up
reads the gas collection findings in `found_values.csv` that are auto-approved or waiting for SME
review. Every value is filed under the site searched, so only a site the run searched is followed
up.

Why a second pass exists at all: gas-capture attributes are only asked of facilities known to have
a collection system, and `has_landfill_gas_collection` is itself one of the searched attributes. A
facility whose system is discovered mid-run therefore needs one more, cheap request. The follow-up
is cached separately (`site_<id>__gccs.json`) so it never overwrites the first pass, and
arbitration merges both.

**Or drive the phases yourself** — useful for long runs, because `arbitrate` is free and you can
run it as often as you like against a search that is still in progress:

```bash
# Phase 1 - query Foundry, cache one raw JSON response per site, then check the cited links and
# locate each source's stated location. Resumable.
uv run waste-ai-search search --run-id pilot10 --iso3 NGA PHL --limit 10

# ...or sample a spread instead of the first N. --limit follows seed order, which is
# facility-id order and clusters by country; --pilot-size draws across countries, and adds
# generic-named hard cases only with --include-unsearchable-names.
uv run waste-ai-search search --run-id pilot10 --pilot-size 10

# Phase 2 - rebuild every output from the cached responses. No agent calls, re-runnable.
# With blob storage configured this pulls from blob first (VPN); add --local to stay fully offline.
uv run waste-ai-search arbitrate --run-id pilot10

# Phase 3 - `run` does this for you. By hand: fold found values, auto-approved or awaiting
# review, back into the seed, then search ONLY the sites where gas collection was discovered -
# refresh-seed prints the command.
uv run waste-ai-search refresh-seed --run-id pilot10
uv run waste-ai-search search --run-id pilot10_pass2 \
  --input-csv outputs/runs/pilot10/refreshed_seed.csv --site-ids <ids printed above>

# During or after review - apply the SMEs' Site_Review decisions: write the auto-approved and
# SME-approved values to accepted_values.csv, for ingestion, and the sources marked New site to
# new_facility_candidates.csv. Reads the SharePoint copy of the workbook, else the run folder's.
# Run it any time: undecided pairs load nothing until it runs again.
uv run waste-ai-search accept-reviews --run-id pilot10 [--workbook <reviewed .xlsx>]
```

**Never search `refreshed_seed.csv` without `--site-ids`.** It holds the whole seeded corpus
(19,492 facilities), not just the sites the run searched, so it would search all ~12,000
searchable sites again (about $3,300). By hand, the narrowed search also re-asks those sites every
attribute still missing; `run` asks only their gas-capture attributes.

Re-running `search` skips sites that already have a cached response unless `--force` is passed, so
an interrupted long run resumes where it stopped. Because `arbitrate` reads the cache rather than
the network, a change to the credibility rubric can be re-applied to a completed run at no cost.

**Sites are searched 4 at a time** (`--workers`, on `search` and `run`; 1 = one at a time). A search
mostly waits on Foundry, so the waits overlap: 10 sites took 1 min 36 s with 4 workers, against about
4 minutes one at a time, with no rate limiting. Progress prints as each site finishes, so the order is
not seed order.
Ctrl-C cancels the sites not yet started and waits for those in flight, which are still logged.

Each attempt at a site is cut off after `--hard-site-timeout-seconds` (240), retries included: a
call still running then is abandoned and not retried. The HTTP timeout
(`AZURE_FOUNDRY_TIMEOUT_SECONDS`) cannot do this - one site waited 15 minutes past it.

### What the agent returns

The agent answers **source first**: each source it used, with the facility that source describes
and the values it gives (build step 4 of
[the source-matching plan](docs/plans/2026-10-20-match-sources-by-location.md), and step 1 of
[the approve-sources plan](docs/plans/2026-10-21-approve-sources-site-by-site.md)).

```json
{
  "site_id": "13755",
  "sources": [
    {
      "url": "https://...", "source_title": "...", "publisher": "...", "source_type": "...",
      "publication_date": "...", "language": "...", "paywall_flag": false,
      "facility_name_in_source": "the facility's name, exactly as this source writes it",
      "facility_name_english": "that name in English",
      "stated_address": "the site's address, exactly as this source writes it",
      "stated_coordinates": "the site's coordinates or grid reference, exactly as written",
      "stated_municipality": "the town or municipality the source puts it in",
      "stated_state": "the state or province the source puts it in, in full",
      "values": [
        {"attribute_name": "operator", "value": "...", "unit": "", "value_basis": "Direct",
         "confidence_score": "High", "value_date": "2024", "evidence_summary": "...",
         "search_terms_used": "...", "quoted_evidence_short": "..."}
      ]
    }
  ],
  "unverified_leads": [
    {"url": "https://... or blank", "attribute_name": "...", "value": "...", "unit": "",
     "value_date": "...", "quoted_evidence_short": "...",
     "why_unverified": "identity / value rule / page not readable", "lead_summary": "..."}
  ],
  "search_notes": "..."
}
```

- `facility_name_in_source` is the name as the source writes it; `facility_name_english` is the
  agent's English rendering, since our names are in English. Either may be blank.
- `stated_address` is the site's own address, never the operator's office, and
  `stated_coordinates` its coordinates or grid reference. Both are copied as written and never
  worked out, and blank if the source gives none. The prompt adds that a page often gives the
  address in a contact box or footer.
- `stated_municipality` and `stated_state` are the town and the state or province the source puts
  the site in, the state written in full; where the source names only a town, the state it is in.
  A source in another state than ours is flagged on Site_Review.
- These six fields only check that the source is about our site (see
  [Locating each source](#locating-each-source)). They never replace our name or location.
- Each value keeps the fields arbitration has always read. Its `value_date` is the date the value
  is true for: a figure's own year, never a target year, else the source's date. A yearly value
  loads under that year.
- `unverified_leads` holds what the agent found but couldn't use, each in the one shape above:
  a page it couldn't tie to our site, a value a rule leaves out, or a page it couldn't read. The
  list may be empty. In the 31 October Mexico tests, without a shape, 174 leads came back in 23.

**The agent decides whether a source is about this site,** as batch 1's prompt had it. It returns
only sources about this facility, using the names and rounded coordinates the prompt gives it to
check, and an empty sources list, with the reason in `search_notes`, if it can't confirm it. From a
registry that lists many sites, it uses only this facility's entry. Agreeing on the translated name
alone is not confirmation. Where our name or coordinates came from a Tier 1–2 source, the prompt
says they are already confirmed by an authoritative source. Every source it returns is filed under
the site searched (see [Filing and approving each source](#filing-and-approving-each-source)).

**A site named after its operator is searched for by its place.** Where our name has a legal form
(S.A. de C.V., Sociedad Anónima, S. de R.L.) or an operator's name (SIMEPRODE, Promotora,
Proactiva, TECMED, Sistema Integral para el Manejo, Sistema Intermunicipal), accents and case aside,
the prompt says it looks like an operator's name, which may run sites elsewhere, and asks for the
landfill or dump at this place under the names local sources use. A source about it may name
another operator: the municipality, state and our other sites decide. Such a name is never called
confirmed. A bare "Intermunicipal" doesn't count: "Relleno Sanitario Intermunicipal de Atlixco" is a
site's own name. 38 of batch 3's 1,553 prompts get this, 15 of them among the 64 sites taking
100,000 t a year or more. In the 31 October Mexico tests, gpt-5.4 and Terra found only the company
at 4005 "RED RECOLECTOR SA. DE CV." and returned nothing, while Sol found the landfill there, La
Estación, in pages naming other operators.

**The prompt gives our coordinates rounded to 2 decimals (about 1 km),** with the municipality
and admin names where the seed has them: enough to check a source's place with (and, outside
Spanish-language sites, to search with), and nothing worth copying. With
the full point in the prompt, the agent once credited our own OSM centroid to a registry. The
reverse-geocoded `formatted_address` is left out for the same reason. The search cap
(`--max-web-searches`) is unchanged.

**A Spanish-language site is searched in Spanish.** That is every site whose `source_language` is
`es`: every Mexican site but five, and also Spain's 249 (batch 5) and 191 in Latin America (the
last batch), though only Mexico was measured. Its prompt says to search its names, municipality and
admin names in the local language, where we hold the original name to spend no searches on
`site_name`, our English translation, and to use our coordinates only to check a source's place,
never in a search: pages almost never print them. Where the gas attributes are asked, gas-project
records such as UNFCCC CDM's are still searched in English. In the 31 October Mexico tests none of
the 213 sources cited was in English but one bilingual PDF, yet Terra searched English names at 43
of 50 sites and our coordinates at 21, with six searches a site. Other languages are still searched
in English too, until their batches are measured.

Only Brazil's seed has place names (SINIR's municipality and state). Every other site's are
looked up from our coordinates when it is searched (see [Reverse Geocoding](#reverse-geocoding)),
from 30 October 2026. Batches 1 and 2 were searched before that, with no town in the prompt.

**Runs cached before this still arbitrate.** One reader,
[`payload.py`](src/waste_ai_search/payload.py), turns any cached response into this shape, and
everything that reads a response goes through it: the evidence, the link check, the run log's
counts, validation, the merge of the follow-up pass, retry detection and the agent's own leads.

- A source-first response from before the approve-sources plan gave one `stated_location`, with a
  kind for each of its parts (`a | b`, kinds `coordinates | street address`). Its parts are split
  by kind: coordinates and grid references become `stated_coordinates`; street addresses, place
  descriptions and unlabelled parts become `stated_address`.
- An older, attribute-first response's citations become one source per exact URL, each with the
  values of the attributes that cited it. Its found name becomes the `facility_name_in_source` of
  the sources cited for it, and its found coordinates the `stated_coordinates` of the sources cited
  for both; half a location is none. A found name or location is no longer a value, so neither
  loads. The agent's order is kept. Each value keeps how its own citation labelled the page (source
  type, date, publisher), so it is tiered as it was, even where the gas follow-up pass labelled the
  same page differently. Only a label its citation left blank comes from the page's other
  citations, so an undated citation of a dated page gets its date: on batch 1 that lifts 118
  citations from the undated cap to their type's tier, and lowers none.

### The full run, in six country batches

Run the batches in order, one run id each, so each gets its own review workbook and its own Bing
total. Batches 1-2 are English-speaking, so SMEs learn the review process on familiar sources;
3-5 follow the most blank core fields; 6 is every other country. The lists are `COUNTRY_BATCHES`
in `schema.py`. Sizes and costs are from the 3 Oct 2026 seed at about 19 Bing queries per site.

```bash
uv run waste-ai-search run --run-id batch1 --batch 1 --workers 16
```

| Batch | Countries | Sites | Est. Bing cost |
|---|---|---|---|
| 1 | CAN, GBR, AUS, NZL, IRL (main language English) | 1,931 | ~$525 |
| 2 | IND, PHL, NGA, ZAF and 21 smaller (English an official language) | 856 | ~$230 |
| 3 | MEX | 2,220 | ~$605 |
| 4 | DEU, RUS, TUR | 2,262 | ~$615 |
| 5 | CHN, FRA, ITA, POL, ESP, IDN | 2,279 | ~$620 |
| 6 | Every other country (110 today) | 2,574 | ~$700 |

The six cover all 12,122 searchable sites once each. USA and Brazil stay excluded, as everywhere.
These sizes and costs predate rule 4 below, which also skips sites whose name gives a search
nothing to look for, so each batch searches fewer sites than this. `measure_name_yield.py
--list-sites` counts how many per batch.

### A second model on the largest sites

Batch 3 is searched with GPT-5.6 Terra, then GPT-5.6 Sol searches the batch's largest sites again:
the 64 taking 100,000 t a year or more, which hold 63% of its incoming waste. In the October 2026
tests on 60 Mexican sites, adding Sol on such sites took good pages from 1.8 to 3.5 a site, for
about $21 across the 64; on smaller sites it mostly found what Terra had, or nothing. The second
pass goes into the same run, so its pages are on the same Site_Review and a page both models cite
is one pair:

```bash
uv run waste-ai-search search --run-id batch3 --batch 3      # Terra, the agent .env names
AZURE_AI_AGENT_NAME=waste-ai-search-gpt-5-6-sol uv run waste-ai-search search \
  --run-id batch3 --batch 3 --min-tonnes 100000 --pass-label sol
uv run waste-ai-search arbitrate --run-id batch3
```

- `--min-tonnes 100000` takes the sites whose seed gives 100,000 t a year or more of incoming
  waste. It applies after the other rules, like `--limit`, so with `--batch 3` it takes the large
  sites the first pass searched, and says how many. `--site-ids` works too, for a list of your own.
- `--pass-label sol` keeps each response beside the first, as `site_<id>__sol.json`, and
  arbitration merges every pass's sources. Retry a site that failed as the same pass, as the search
  prints: `--pass-label sol --force --site-ids ...`. Without the label, `--force` would replace the
  first pass's response.
- The search checks the new pages' links and locates their sources, as every search does. If it
  ran with `--no-link-check` or `--no-locate`, run `check-links` and `locate` before arbitrating.
- The run log has a row for each site and pass, each priced at its own model (see
  [Bing cost](#bing-cost)).
- Don't raise `--max-web-searches` instead. The pages one model missed were nearly all ones it
  never came across, not ones it ran out of searches for, and where no model found anything, each
  had confirmed the town and found no page about a dump there.

### What is actually searched — read this before a full run

Four rules narrow the search. The first three mean a full run queries **12,392 of 15,537
facilities** and makes **167,410 attribute-requests** instead of 22 x 15,537 = 341,814. The fourth
removes more, and so does no longer asking for a site's name and location; those figures predate
both.

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

**2. Brazil is excluded by default, and a search of it now has nothing to ask.**

Brazil is coordinates only: every other Brazilian attribute comes from a government source, so
searching it risks overwriting better data than the search can find. Its coordinates were the one
thing worth confirming, for the 3,913 of 4,225 sites, all from SINIR, whose location is flagged
inexact. But since [the approve-sources plan](docs/plans/2026-10-21-approve-sources-site-by-site.md)
a site's location is no longer asked for, and a source's coordinates never replace ours. So a
Brazilian site has nothing to ask, and is not searched even when Brazil is asked for: the run
counts it among the sites with no attributes left to search, and a search of Brazil alone
(`--iso3 BRA`) stops with an error saying so. A Brazilian response cached before then still
arbitrates, and loads nothing.

The rest of this rule describes how Brazil was searched before, for when it is taken up again (see
[docs/future_improvements.md](docs/future_improvements.md)).

SINIR's coordinates are the centre of a municipality, not the dumpsite, and most of its names are
generic ("Lixão", "Aterro Controlado"). So the seed carries the municipality and state SINIR
recorded (`facility_city_location`, an IBGE code, named from `raw_sinir_general_city_data`) into
`municipality` and `admin1`, and the prompt passes them on. Without them the agent guessed the town
from the city-centre point and, on the WP-543 test, returned dumps 140 and 237 km away.

With the municipality the agent found the right facilities, but in reports that publish no readable
coordinates: **0 of 20** sites got usable coordinates either way. So Brazil is excluded by default
like the US. The same three overrides (`--iso3 BRA`, `--site-ids`,
`--include-excluded-countries`) still lift the exclusion, but now find nothing to ask. Matching
against OSM/GPW landfills within the municipality is the likelier fix. Rule 4 applies to Brazil
too, and many of SINIR's names are generic labels such as `Dump`.

**3. Everywhere else: gap-fill, plus the gas-capture gate.**

- **A site's name and location are not asked for.** Every source states the name, address and
  coordinates it gives, which only check that it is about our site. Where our name or coordinates
  came from a Tier 1–2 source (the name for about 10,600 facilities, both coordinates for 4,063 of
  the 12,122 searchable sites: Mexico INEGI, E-PRTR, Canada GHGRP), the prompt says they are
  already confirmed. Coordinates flagged inexact are never called confirmed, and nor is a name
  that looks like an operator's (see [What the agent returns](#what-the-agent-returns)).
- Metadata attributes are requested **only where that facility's baseline is empty**.
- The seven `gccs_*` attributes are requested **only where gas collection is known present** —
  212 to 603 facilities each, not 13,000. See the two-pass note above for why this loses nothing.

Both country rules live in [`schema.py`](src/waste_ai_search/schema.py) as
`DEFAULT_EXCLUDED_ISO3` and `COORDINATES_ONLY_ISO3`, so changing policy is a one-line edit.

**Why these rules exist, in one line each:** the US is already Tier 1 covered by GHGRP and LMOP, so
searching it manufactures equal-tier conflicts rather than data; Brazil's non-coordinate attributes
are government-sourced and should not be overwritten by a web search, its coordinates were not
found by web search on the WP-543 test, and a source's coordinates no longer replace ours.

A site left with no attributes to search is dropped from selection entirely — querying it would
spend a request and a prompt to receive nothing.

**4. Sites whose name gives a search nothing to look for are skipped by default.**

The agent finds a facility by its name and location. A name that is filler, stray text or only a
kind of site gives it nothing to search for, so its queries are wasted or find a different
facility nearby. Four classes of name are skipped
([the plan](docs/plans/2026-10-15-skip-unsearchable-names.md);
[`searchable_names.py`](src/waste_ai_search/searchable_names.py) holds the test):

| Class | What it is | Examples |
|---|---|---|
| No name | An empty name, the filler ingestion gives an OSM polygon with no name, or a name saying there is none, unless the source's own spelling is a name | `OpenStreetMap Landfill`, `NO NAME` |
| Not a name | A URL, a bare number or a survey note that ended up as the name | `update the tag landuse after survey`, `4000` |
| Generic label | Only says what kind of site it is (`is_generic_site_name`), in English or Spanish. Initials and route codes count as a name: `K & W Landfill` and `SR 85 Landfill` are searched | `domestic landfill`, `Municipal Landfill`, `Landfill II`, `Basurero Municipal`, `Relleno Sanitario Tipo C` |
| Mining feature | A spoil heap or similar, matched in the English name or the original (отвал, террикон, відвал) | `Overburden Blade`, `Terrikon` |

Nothing in the database changes: a skipped site stays in WasteMAP and Climate TRACE. It just isn't
web-searched until it has a name worth searching for. Words that only describe the kind of site,
such as municipal, sanitary, controlled, open-air and final disposal, don't count as a name:
`Sanitary Landfill` and `Controlled Landfill` are skipped, `North Landfill` is searched. Spanish
words were added for Mexico, where 417 sites are named just `Basurero Municipal`; batch 3 skips 667
of its 2,220 sites. Names still in Cyrillic are searched.

The generic test knows only English and Spanish words, so it reads the English name, not the source's own
spelling. It can't tell a generic original such as `Полигон ТБО` ("MSW landfill") from a
distinctive one, so a site whose English name is a generic translation is skipped even when its
original, which the agent would search first, names it. How often that happens can't be measured
from data in this repo.

It works like the country rules. The run prints how many sites it skipped in each class, and an
explicit request wins:

```bash
uv run waste-ai-search search --run-id r --batch 2 --include-unsearchable-names  # search them
uv run waste-ai-search search --run-id r --site-ids 499                          # listing ids wins
```

Naming a country with `--iso3` does not lift it, and `--pilot-size` draws its generic-named hard
cases only with `--include-unsearchable-names`. The rule runs after the country rules and the
nothing-to-ask check, and before `--limit` and `--pilot-size`, so a limit or a sample takes sites
worth searching. Its count is therefore of the whole selection: a 10-site pilot of a batch lists
every site in the batch the rule skips, not only those the pilot would have reached. It only
removes sites from a batch and never moves one to another.

Every skipped site is listed in `skipped_sites.csv` in the run folder, with its class and the
rule it tripped. The list is the run's, not the last search's: each search under the run id adds
its skips, so resuming one country keeps the others on record, and a site the run searches later
(by id, or with the flag) comes off it. Run_Config records `unsearchable_names_skipped`, the
sites skipped and never searched, by class, and `unsearchable_names_searched`, the sites in those
classes that were searched anyway (listed by id or with the flag). A site is counted in one or the
other, never both. Runs from before the rule say "not recorded".

This read-only script measures what searching each class found in finished runs, against named
sites from the same kind of source, and forecasts what each batch would spend on each class
without the rule:

```bash
uv run python scripts/measure_name_yield.py                    # list the runs in blob
uv run python scripts/measure_name_yield.py --run-id <run id>  # measure them
uv run python scripts/measure_name_yield.py --list-sites       # every site each batch skips
```

| Scope | Sites | Attribute-requests |
|---|---|---|
| Everything, no rules | 15,537 | 341,814 |
| Gap-fill + GCCS gate only | 15,537 | 197,110 |
| **+ country rules (default)** | **12,089** | **166,804** |

At the measured 42s/site that is roughly **6.3 days** serial, down from 8.0. These figures predate
rule 4, which lowers them further.

### Attributes searched

**20 attributes are searched.** No site is asked for all 20 — the scoping rules above decide
which ones a given facility gets. Three more gas-capture volumes are no longer searched but are
still read from the seed and from older cached responses (see below).

**A site's name and location are no longer searched.** Until the approve-sources plan,
`found_facility_name` and `found_location` were values, asked for where a Tier 1–2 source hadn't
supplied ours, and reviewed as corrections to our name and coordinates. Now every source states the
name, address and coordinates it gives (see [What the agent returns](#what-the-agent-returns)),
and those only check that the source is about our site: they never replace ours. A name or
location an older cached response gave as a value is not loaded.

**Newer sources win.** The agent is told to prefer the most recent source when sources disagree,
and to base `facility_status` on the most recent source that shows whether the site still takes
waste (a closure notice, current opening hours, news) rather than an older permit or directory
listing. A fire doesn't show it, since closed dumps burn too: in the 31 October Mexico tests, 9
statuses came from fire reports. Arbitration follows the same rule: where approved values for one
site, attribute and year disagree, within a tier the newer `value_date` wins. Each year of a status
is its own value now, so an older status loads at its own year rather than losing to a newer one.

**A closure counts once it has happened.** A closure that is only ordered, recommended, planned,
funded or expected, a temporary one (*clausura temporal*), one later reversed, or a replacement
site's opening doesn't make a site Inactive or give it a closing year. `closing_year` is a year a
source gives for the closure, never the article's own date, and the agent is told to look for it
whenever it finds a site closed. In the 31 October Mexico tests, 15 statuses and closing years took
such a closure as done, among them a closure programme Profepa had approved and a seal with 15 days
to comply.

**The operator is the body a source says runs the site,** by its full name as the source writes it,
never a description ("la empresa", "autoridad municipal"). A body the source names only for owning,
regulating, inspecting or visiting the site, hauling waste to it, building, cleaning up or closing
it, or giving a figure is not its operator: in the 31 October Mexico tests, 12 operators were such
bodies, among them the police at a fire and a town lending machinery for a clean-up.

**Containment decides between a sanitary landfill and a controlled dumpsite.** Compaction, soil
cover and restricted access occur at both, so where no source says whether a site has a liner,
leachate collection or groundwater monitoring, the agent chooses neither and leaves `facility_type`
out. A page describing an open dump can still give `Dumpsite`. In a country bound by the EU
Landfill Directive, a landfill that took waste after July 2009 is still a Sanitary Landfill unless a
source says it lacks that engineering. In the 31 October Mexico tests, Sol gave Controlled Dumpsite 7
times from a fence, compaction or criticism, and 6 were wrong.

**One cell's area or cover is not the site's.** `area_square_meters` is the waste footprint, not
the whole property, nor one cell or extension, and `cover_types` the cover used across the site: in
the tests, a new cell's 6,000 m² of geomembrane was given as a site's area.

**Gap-fill — requested only where that facility's baseline value is empty.**

| Attribute | Type | Unit asked for | → standardized column |
|---|---|---|---|
| `facility_status` | enum | — | `facility_status` |
| `facility_type` | enum | — | `facility_type` |
| `operator` ‡ | text | — | *none yet — review layer only* |
| `opening_year` | integer year | `year` | `opening_year` |
| `closing_year` | integer year | `year` | `closing_year` |
| `has_landfill_gas_collection` | boolean | — | `has_landfill_gas_collection` |
| `annual_incoming_waste_metric_tonnes` | numeric, municipal waste only (see below) | `metric tonnes/year` | `annual_incoming_waste_metric_tonnes` |
| `waste_in_place_metric_tonnes` | numeric | `metric tonnes` | `waste_in_place_metric_tonnes` |
| `area_square_meters` | numeric | `square meters` | `area_square_meters` |
| `waste_depth` | enum (derived) | `meters` in, category out | `waste_depth` |
| `has_cover` | boolean | — | `has_cover` |
| `cover_types` | enum array | — | `cover_types` |
| `has_biocover` | boolean | — | `has_biocover` |

‡ **Review-layer only: `operator`, `has_flare`, `flare_efficiency`.** The upstream standardized spec has no column for them yet, so a found value is sorted and approved like any other, and can reach `accepted_values.csv`, but has no standardized column to load into. Loading them is a follow-up once upstream adds the columns. `consolidated_facility` has none of them either, so each is asked of every facility its scoping rules allow.

**Incoming waste counts municipal waste only,** as the standardized spec's "Municipal waste only"
rule asks of every source: household and similar waste, which decomposes and generates methane.

- Where the source doesn't say what an amount is made of, it is taken as municipal and loads as
  written. This is the usual case.
- Where the source splits it ("120,000 t, of which 20,000 t construction debris"), the agent
  returns the municipal part (100,000 t).
- Where the source says the amount is mainly other waste (construction and demolition rubble,
  soil, quarry or mining waste, industrial or hazardous waste, or sludge), the agent returns it
  with `not_municipal = true`. It is listed in `found_values.csv` as not used ("not municipal
  waste") and never loads.
- On Site_Review, an SME marks an amount of rubble, soil or tailings Wrong, so it loads nothing,
  or types just the municipal part in `corrected_value`.

Unlike the US GHGRP transform in `waste_data_ingestion_pipeline` #244, nothing estimates a
municipal share where the source gives none. A site taking mainly other waste is typed
`Non-Municipal Waste Landfill` (see [Landfills for other waste](#landfills-for-other-waste)).

`bulk_waste_type`, the waste that made up most of what a site receives, was retired on 30 October
2026 (`UNSEARCHED_ATTRIBUTES`): it is no longer asked, and a value from a response cached before is
listed as not used ("no longer asked"). The loader in `waste_data_ingestion_pipeline` #261 typed a
site Non-Municipal from it; with no waste type in new runs, that rule no longer fires.

`waste_depth` is the only **derived** categorical. The agent still reports a number and its
unit; the pipeline converts to metres, then bins on the spec's 5 m boundary. The metre value is
kept in `normalized_value` as evidence and the category lands in the column. A depth of zero or
less is not a measurement, so it is stored NULL rather than binned to `<=5m`.

`has_biocover` is **not** derived from `cover_types` — a plain soil, clay or sand cover is not a
biocover, and the prompt says so explicitly. `consolidated_facility` now carries the column but it
is entirely NULL today, so the gap-fill rule still fires for every facility.

An enum array is read however it is written: separated by semicolons or commas, or as a JSON list.
The prompt asks for `cover_types` as an "Array", so the agent often writes `["other soil
mixture"]`. Until 30 October 2026 that was split on its commas, brackets and all, and dropped as
unmappable: 8 times in the October 2026 test runs, while 3 cover types got through.



**Gas capture — additionally gated on `has_landfill_gas_collection` being known TRUE.** Asking a
facility with no capture system about methane flaring buys a certain "nothing". Nothing is lost by
the gate: `has_landfill_gas_collection` is itself gap-filled, so `refresh-seed` merges a
discovery into the baseline and the next pass picks up that facility's GCCS attributes.

Since 30 October 2026 the prompt says what each counts as. `has_landfill_gas_collection` is a
system collecting gas through wells and pipes, to a flare or an engine, that exists or existed: not
one ordered, required, planned or proposed, and not vents that only let gas escape.
`gccs_current_project_status` is the status of each gas energy project, never of the landfill
itself. In test10-mex-sample2-run2, an order to Naucalpan's operator to "implement ... biogas
management" was read as gas collection. That set off the follow-up search, which then gave the
landfill's own status as its gas project's: Operational because waste was arriving, and Closed
because the site had been sealed.

| Attribute | Type | Unit asked for | → standardized column |
|---|---|---|---|
| `gccs_ch4_flared_metric_tonnes` | numeric | `metric tonnes CH4` | `gccs_ch4_flared_metric_tonnes` |
| `has_flare` ‡ | boolean | — | *none yet — review layer only* |
| `flare_efficiency` ‡ | fraction 0–1 | `fraction between 0 and 1` | *none yet — review layer only* |
| `gccs_energy_project_type` | enum array | — | `gccs_energy_project_type` |
| `gccs_current_project_status` | enum array | — | `gccs_current_project_status` |
| `gccs_collection_efficiency` | fraction 0–1 | `fraction between 0 and 1` | `gccs_collection_efficiency` |

**No longer searched (F31):** `gccs_ch4_generated_metric_tonnes`, `gccs_ch4_collected_metric_tonnes`
and `gccs_ch4_flow_to_project_metric_tonnes`. They are still read from the seed, and a run cached
before the change still re-arbitrates them for review.

#### Units are converted, not demanded

The agent is asked for the source's **original** unit and the pipeline converts to the spec's
canonical unit. A unit it cannot convert produces a warning and the value is **not used**, so
an unconverted number can never land in a column whose name asserts a unit.

| Target | Accepted source units |
|---|---|
| `area_square_meters` → m² | m², **ft² / sq ft** (×0.092903), **acres** (×4046.856), hectares, km² |
| `waste_depth` → m, then binned | m, **feet** (×0.3048), yards → `<=5m` / `>5m` |
| mass → metric tonnes | metric tonnes, kg, US short tons, long tons, pounds |
| rates → per year | /day ×365, /week ×52, /month ×12, /hour, /minute |
| CH₄ → metric tonnes | t, kg, **ft³ CH₄** (×0.0192 kg), **m³ CH₄** (0.679 kg), **MMCFD** (×1e6×365) |
| `gccs_collection_efficiency`, `flare_efficiency` → 0–1 | fraction, or a percentage (÷100) |

Bold entries are the spec's own conversion table. Three deliberate refusals: an ambiguous
`"tons"` is **never** converted (metric vs short is a 10% error); a **landfill-gas** volume is
refused because the spec's formula needs a methane fraction the source did not give; and a value
whose unit cannot be identified is refused rather than assumed.

Units written in Spanish, Portuguese, French, Italian or German are read as their English names,
accents aside: "toneladas por día", "metros cuadrados", "Hectáreas", "Tonnen pro Jahr", "t/a",
"tonnellate all'anno". Until 31 October 2026 they were refused, which dropped Pro Limpieza's area
and waste in place in every Mexico test run. "ton" counts as tonnes only beside words in one of
those languages ("ton/día"), where it means the metric tonne; an English "tons" stays ambiguous.

Allowed categorical values come from the **live Postgres enums** — regenerate with
`scripts/generate_enums.py`. Anything outside them maps to NULL with a parse warning rather than
being forced into a wrong category.

### Not a waste facility (WP-525)

8,564 facilities (44% of the corpus) come only from OSM (6,848) or Global Plastic Watch (1,716),
with nothing but a name and a coordinate. A crowd-mapped `landuse=landfill` polygon or a satellite
detection says nothing about what a site is used for — it can be a quarry or a construction yard.

For those facilities only, `facility_type` has a fifth allowed value, **`Not a Waste Facility`**.
The agent returns it only when a source positively says the site is, and always was, something
else. It is a contradiction, not a verification: finding nothing leaves `facility_type` empty,
because small informal dumps routinely have no web presence at all.

**A closed, capped or redeveloped landfill is not a contradiction.** It keeps emitting methane for
decades, which is why WasteMAP tracks it. A landfill that is now a park keeps its landfill type,
with closure in `facility_status = Inactive` and `closing_year`. The question is *was this ever a
waste disposal site?*, not *is it operating today?*

The value is a `facility_type` like any other, except that **a source saying it always goes to an
SME**, whatever its tier: it is one of the three exceptions (see
[Filing and approving each source](#filing-and-approving-each-source)). On Site_Review its row is
flagged "says Not a Waste Facility". The verdict is used only where the site searched was offered
the check; at a site that wasn't, it is not used, and the rest of that source's values still go to
an SME. An SME's Different site loads it at the other site only where that site would have been
offered the check. It also reaches the next pass's seed, which `refresh-seed` builds from values
auto-approved or awaiting review. Where the same run also found the site closed, the likeliest
misreading, the flag asks whether it is a closed landfill misread as never a waste site.

> **Pending upstream.** The database's `facility_type` enum and `chk_facility_type` do not carry
> `Not a Waste Facility` yet, so loading an approved one will be rejected.
> `check_db_schema.py` warns about it. The value needs adding upstream in
> `waste_data_ingestion_pipeline`.

Facilities with any other source, or any corroborating attribute, are never offered the value, so
the other 10,928 prompts carry the ordinary values and none of this guidance.

**Rule 4 narrows this a lot.** It skips OSM polygons with no name (`OpenStreetMap Landfill`, all of
them OSM-only), many of the sites this check was aimed at, along with any OSM or GPW site whose name
is only a generic label. So the verdict can now come only from named OSM and GPW sites, and the
8,564 and 10,928 above predate the rule. Whether the skipped sites are landfills is left to the
Climate TRACE review.

### Transfer stations and recycling centres

`facility_type` also offers **`Transfer Station`** and **`Recycling Center`**, both pending upstream
like `Not a Waste Facility`. Neither disposes of waste, so a site that is only
one of these is out of scope for methane. A name scan of OSM sites found about 100 in the corpus,
52 of them in batch 1.

**A site that is both** (e.g. a "Waste Transfer & Recycling Centre") is a `Transfer Station` if it takes mixed waste, and a `Recycling Center` only if it takes nothing but separated recyclables.

**A transfer station or recycling centre built on a closed landfill is still the landfill.** The
agent is told to classify the disposal site (Sanitary Landfill, Controlled Dumpsite or Dumpsite)
whenever waste was ever landfilled or dumped there, because the buried waste still emits.

> **Pending upstream.** `Transfer Station` is in `chk_facility_type` on every `transformed.*` table,
> but not yet in the `facility_type` enum type or `consolidation.consolidated_facility`'s
> `chk_facility_type`; `Recycling Center` is in none of them. Until they are, consolidating a row
> with either is rejected, as with `Not a Waste Facility`. Taking a value off
> `PENDING_UPSTREAM_FACILITY_TYPES` is a manual step: do it only once `check_db_schema.py` reports
> it accepted by **both** the enum and `consolidated_facility`'s `chk_facility_type`, which roll out
> separately. Then regenerate `db_enums.py`; `generate_enums.py` does not edit the pending list.

### Landfills for other waste

`facility_type` also offers **`Non-Municipal Waste Landfill`**: a disposal site taking mainly waste
other than municipal solid waste, such as construction and demolition rubble, soil, or industrial,
hazardous or mining waste. It is the standardized spec's type for a site a model of municipal waste
leaves out (`waste_data_ingestion_pipeline` #238). What a site takes decides it, whatever its
engineering: an engineered hazardous-waste landfill is a Non-Municipal Waste Landfill, not a
Sanitary Landfill. "Inert landfill", "C&D landfill", "industrial landfill" and "hazardous waste
landfill" map to it. Since 30 October 2026 it is the only place a page saying a site is, say, an
inert-waste landfill is recorded: `bulk_waste_type` is retired, and an amount of other waste is
marked on the amount itself.

> **Pending upstream** like `Transfer Station`: it is in `consolidated_facility`'s
> `chk_facility_type` on `main`, but stays on `PENDING_UPSTREAM_FACILITY_TYPES` until
> `check_db_schema.py` reports the `facility_type` enum type accepts it too.

### Outputs

| File | Purpose |
|---|---|
| `<run_id>_review.xlsx` | **The SME artifact.** Its Site_Review tab lists the site–source pairs an SME needs to decide, grouped by our site (see [SME Review Workflow](#sme-review-workflow)). `accept-reviews` reads the decisions back from it. |
| `found_values.csv` | **Every value found**, one row per site, year, source and attribute, with its status (`auto-approved`, `SME review` or `not used`) and `status_reason` (see [Filing and approving each source](#filing-and-approving-each-source)). Each row gives our site, the source and its tier, the facility the source describes (`facility_name_in_source`, `facility_name_english`, `source_address`, `stated_coordinates`) and where that puts it (`source_coordinates`, `coordinates_from`, `distance_km`, and `location_note` where there is no point), the value as converted and as written, our value for that year, the agent's quote and whether it and the location are on the page, and the pair's `review_id`. `accept-reviews` fills `sme_decision` and `loaded`. The agent's unverified leads are listed too, as not used, with the link, value, date, quote and note each gives, however the agent named them (a lead written as a whole source gives a row for each of its values), and why it didn't stand behind it in `status_reason`. The audit trail. |
| `accepted_values.csv` | **For ingestion**, written by `accept-reviews`: one value per site, attribute and, for a yearly attribute, year, from the auto-approved pairs and those SMEs approve on Site_Review (Correct site, or Different site with `other_site_id`, where that site's field is empty). Where accepted values disagree, the best tier wins, then the newest, then the value most sources give. Columns: `run_id`, `site_id`, `site_name`, `country_iso3`, `attribute_name`, `value`, `unit`, `year`, `source_url`, `source_tier`, `approved_by` (`auto` or the reviewer), `reviewed_date`, `review_id`. Arbitrating again empties it to its header row; run `accept-reviews` again. |
| `sources.csv` | One row per exact URL, with a `source_id` derived from it (see below), its tier, and how many sites cited it. |
| `source_locations.json` | Each stated location's point, keyed by its text and country (see [Locating each source](#locating-each-source)). |
| `page_text/` | The text of each page that loaded, one gzip file per exact URL named by its `source_id` (see [The text of each page that loads](#the-text-of-each-page-that-loads)). `found_values.csv`'s `quote_on_page` and `location_on_page` say whether each source's quote and stated location are on it. |
| `parse_warnings.csv` | Dropped values, dropped map-site citations, unit failures, unmappable enums. |
| `sites_to_retry.csv` | Sites whose search tool failed, so nothing was searched (see [If a site could not be searched](#if-a-site-could-not-be-searched)). |
| `skipped_sites.csv` | Sites the run skipped because their name gives a search nothing to look for, with the class and rule. Each search adds to it. |
| `new_facility_candidates.csv` | For ingestion: landfills none of our sites is, from the sources SMEs mark New site on Site_Review (`found_by` = `SME`). One row per source, with its URL, name as written and in English, stated location, the point and where it came from, our nearest site and its distance, the searches that cited it, and a note of who marked it and the values it gives. `accept-reviews` adds each once; arbitrating again keeps them. |
| `possible_duplicates.csv` | For ingestion (entity linkage's #225 tackles E-PRTR re-registrations): pairs of our sites whose searches cited the same page under the same facility name (not a blank one), with their names, the distance between them, the pages, the names, and a `reason` that says when the sites are too far apart to be one facility. |
| `datasets_found.csv` | For ingestion: pages that look like datasets, such as a regulator's list of its landfills (see [What else is listed for ingestion](#what-else-is-listed-for-ingestion)). It gives the page, its publisher, type and tier, how many searches cited it and how many facilities it names and places, the searches, the names it gave, and why it is listed. |

**Gone since the approve-sources plan:** `evidence.csv`, `resolved.csv`, `review_queue.csv` and
`supplementary_leads.csv`, which `found_values.csv` replaces, and `outside_run_matches.csv`, since no
source is moved off the site searched now. Arbitrating a run again deletes the copies an earlier
arbitration left in its folder, and says so. A copy already pushed to blob is kept there, as the
record of that arbitration.

**Nothing is loaded from this repository.** It used to write a standardized table and a `.load.sql`
beside it, built before review. Ingestion builds the AI search's table itself
(`waste_data_ingestion_pipeline` #261), from `accepted_values.csv` once #261 is reworked for it.
Arbitrating a run again deletes the `std_facility_tbl_ai_search_<run_id>.csv` and `.load.sql` an
earlier arbitration left in its folder, as above.

**One source per exact URL.** The query string is part of the URL, so documents that one script
serves by id stay separate sources: dropping it once merged 103 different Natural Resources Wales
documents into one. Only spellings that never change the page are cleaned: the scheme and host's case,
a leading `www.`, a trailing slash and the `#fragment`. The path and query keep their case. The
`source_id` is derived from that URL, so it is the same in every run and for every site citing
it, and every `found_values.csv` row's `source_id` is in `sources.csv`.

### Source credibility

Tier is assigned **locally** from `source_type` and publisher, never taken from the agent — measured
against the pilot responses, the agent's self-assigned tier put news reports at Tier 2, 3 and 4
simultaneously. Its number is kept as `agent_proposed_tier` in `sources.csv`, for drift monitoring
only.

| Tier | Class | A value that fills an empty field |
|---|---|---|
| 1 | Regulator, permit/registry, operator disclosure | Approved automatically, unless an exception sends it to an SME |
| 2 | Peer-reviewed, IFI/UN dataset, govt statistics | Approved automatically, unless an exception sends it to an SME |
| 3 | News, trade press, NGO report | SME review |
| 4 | Aggregator, wiki, directory listing | SME review |
| 5 | Source type could not be established | SME review |

**Tier 1–2 fills of empty fields are approved automatically,** as in batch 1, unless one of the
three exceptions applies (see [Filing and approving each source](#filing-and-approving-each-source)).
That reverses "nothing is accepted automatically" (design doc F36), under which every value went to
SMEs. **A value we already hold is never changed** by the search, whatever the source's tier: a
value equal to ours, or a different one, is listed as not used. So the tier of our own value no
longer decides anything.

A source with no publication date is capped at Tier 3 whatever its publisher. A pair takes the tier
of its best-rated citation, so a page one search cites twice, once dated and once undated, takes
the better of the two.

Corroboration counts sources, not citations. A page cited twice for one attribute counts once when
the value most sources give breaks a tie.

**Two map sites are dropped.** Citations of `mapcarta.com` and `maptons.com`, any subdomain
included (`ca.maptons.com`), are dropped before arbitration, and so are the agent's own leads that
point at them. Mapcarta's landfill pages copy OpenStreetMap, where most of our sites came from: all
319 batch-1 citations traced to the OSM way in the URL. Maptons is an unattributed scrape of
business listings, and 27 of its 36 batch-1 coordinate pairs were more than 2 km from the site
searched. They are recognised by website, never by the agent's `source_type`, which it applies
unreliably. Each drop is counted in `parse_warnings.csv`, per site and website, and the URLs are
not link-checked. Nothing is read from a dropped page, its location included, and its values are
not listed in `found_values.csv`. Wikimapia, an independent crowd-sourced map, stays as a weak
source.

The prompt tells the agent not to cite the two, and to cite the government source where a
directory republishes one: `wastebook.co.uk` copies the Environment Agency register (its
coordinates are postcode centroids, and its "issued" date is a permit date, not a closure date),
and `landfills.ca` copies Statistics Canada's infrastructure database.

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

The reviewer opens **one file**: `<run_id>_review.xlsx`, in the run's SharePoint folder (see
[Review workbooks in SharePoint](#review-workbooks-in-sharepoint)), or else the run folder. Its
ReadMe tab gives the same steps as this section. Everything else in the run folder is audit
material you only need if a row raises a question.

**The question for each row is whether the source is about this site of ours.** We trust the agent
to read most values off a page: you check only those on ↳ rows (below), and the rest load with your
approval of the source.

### 1. Open the Site_Review tab

This is the worklist. Each row is one site–source pair: a source the search found for one of our
sites, whose values would fill fields that are empty in our database for that site. Rows are
grouped by our site, in site id order, with a rule above each new site. Under each site, sources
with flags come first, then the best tier. An empty tab means nothing needs you.

**↳ rows.** Under a Tier 3–5 source, an indented ↳ row for each key value it would load: site type,
closure year, incoming waste, area, gas collection and collection efficiency. Under a source of any
tier, one for an operator the search worked out rather than read, or found sources disagreeing on
(`value_basis` Inferred or Conflicting). Check each against the page and mark it Correct or Wrong: it
loads only once marked Correct, and only with its source.

Every source whose values would fill an empty field is here, whatever its tier: the flags (below)
say which need a closer look. Not here: values that confirm or would change one we hold (only fills
of empty fields load), dead links, and values that couldn't be converted or mapped.
`found_values.csv` lists every value, with its status and why.

### 2. Read the row

The columns run left to right in four groups: our site, the source, what the source says, and what
approving it would do.

| Columns | What they tell you |
|---|---|
| `site_id`, `site_name`, `original_site_name`, `source_language`, `country_iso3` | **Our site.** `site_name` is our database's name, machine-translated into English; `original_site_name` is the same name before translation, in `source_language`, and blank where it was never translated. The search uses the original, so a source about *EL HONGO* is the right facility even though `site_name` says *THE FUNGUS*. |
| `our_coordinates` | Where our database puts the site, as `lat, lon`. Paste it into Google Maps' search box. |
| `our_place` | The municipality and state our site is in, looked up from `our_coordinates`. |
| `source_url`, `source_title`, `publisher`, `source_tier`, `link_status` | **The source. Open it.** `link_status` says whether the link was checked and found working (`OK`) or couldn't be verified (`Unverified`, usually a site refusing automated checks, so it may still open for you). |
| `facility_name_in_source`, `facility_name_english`, `source_address`, `source_place`, `stated_coordinates` | The facility the source describes, as the source writes it: its name, address, town and state, and coordinates or grid reference; `facility_name_english` is the name in English. |
| `source_coordinates`, `coordinates_from`, `distance_km`, `location_note` | Where the source places the facility, as `lat, lon`: paste it into Google Maps beside `our_coordinates`. `coordinates_from` says what it came from: coordinates the source states, a grid reference, or its address placed on the building or only on the street. A street point can be a kilometre or more off. `distance_km` is how far it is from our site. Where the source states a location but `source_coordinates` is blank, `location_note` says why (too coarse, its address and coordinates disagree, copied from the prompt, a postcode centroid), and `distance_km`, if filled, is roughly how far it is. |
| `flags` | Why the row needs a second look, shaded where there are any (see below). |
| `would_load` | What approving loads for this site of ours, one line per value: `attribute = value`, with the year for a yearly attribute, such as `facility_status = Active (2024)`. One value loads per site, attribute and year, so a line can lose to a better-tier value for the same field: the flags say where another source's would. |
| `review_id`, `check` | Hidden, at the end. The pair's id, and on a ↳ row which value it checks: your decisions are read back by them. |

| Flag | What to do |
|---|---|
| quote not found on the page (attributes), or only partly found | Our automated check couldn't find the agent's quote for those values on the source's page. Look yourself: the check can't read images or maps, and it makes mistakes. |
| address or coordinates not found on the page, or only partly found | The same, for where the source places the facility. |
| same page and name also cited for site(s) X | Searches for other sites of ours cited this page under the same facility name. It is about one of them at most, unless they are one facility recorded twice. The row is listed under each of those sites too, and the pair of sites is on `possible_duplicates.csv`. |
| more than 5 km from our site | Compare `source_coordinates`, or where it is blank `stated_coordinates`, with `our_coordinates`. |
| in a different state from our site: the source says X | The source puts the facility in another state or province than `our_place`. States are compared as Nominatim names them, so "N.L." and "Nuevo León" are one state. A site near a border may be described from the next state, so look at the map before rejecting it. |
| the source puts the site in X; ours is in Y - check it is our site | In our state, or where the source's state is unknown, the source names a municipality that shares no word with ours or with our site's name. Accents, case and words such as *ciudad*, *villa*, *municipio*, *de*, *San* and *Santa* don't count, so San Juan Mixtepec and San Sebastián Tecomaxtlahuaca are two towns. It may be about the dump that town uses rather than ours: at 9233, four pages about El Guayabo in Medellín de Bravo were cited for our landfill in Veracruz. But a landfill can serve one town from the next, and our municipality is looked up from our coordinates, so it can be the city rather than the municipio ("Ciudad Obregón" for Cajeme). Check, don't reject on the flag alone. |
| the source names no facility | The source gives no name for the facility it describes. Check that it is about one facility, and that it is ours. |
| says Not a Waste Facility | Approving loads that verdict. Check the source says the site was never a waste site: a closed landfill still is one. Where the run also found the site closed, the flag says so. |
| a Tier N source here gives `attribute = value`; if both are approved, that one loads (or this one loads) | Another source on this tab gives a different value for the same field and year. One value loads, the best tier first, so the flag says which would if you approve both. If you think the one that loses is right, say so in `notes`. Two spellings of one operator's name are not different values: names are compared without small words, legal forms such as "S.A. de C.V.", a council's "H." and its years, so "H. Ayuntamiento de Tixkokob 2024-2027" and "Ayuntamiento de Tixkokob" are one operator. So are two names where one's words are all in the other, and a name and its bracketed acronym. |

**Source links.** `source_url` is clickable, and shows the URL exactly as the source gave it. The
link behind it is made safe (spaces and accented characters encoded), so it opens even when the
text has them. A URL that is not `http`/`https`, or longer than Excel can follow, is shown as plain
text rather than linked.

### 3. Decide

Pick `decision` from its dropdown:

| `decision` | When | What happens |
|---|---|---|
| **Correct site** | The source is about this site of ours. | Its values load here: everything in `would_load`, a line marked *(check independently)* once its ↳ row is Correct, unless a flag says a better-tier value for the same field loads instead. |
| **Different site** | It is about another site of ours. Put that site's id in `other_site_id`. | Its values load at that site, where that site's fields are empty. |
| **New site** | It is about a landfill that isn't in our database. | Nothing loads. The source is listed in `new_facility_candidates.csv` for ingestion. |
| **Junk** | The source is no use: it isn't about one facility, or you can't tell which. | Nothing loads. |
| *(blank)* | Not decided yet. | Nothing loads yet. |

- **A decision covers the whole row.** Correct site loads every line of `would_load` that wins its
  field (best tier, then newest, then most sources, among every value approved for that site,
  attribute and year), and the other decisions load none. Where an auto-approved source you don't
  see here would win, a flag says so.
- **If it is about another of our sites but you can't tell which,** leave `decision` blank and say so
  in `notes`. A Different site with no `other_site_id` can't be read back.
- **A renamed site is fine.** The question is whether it is the same facility, not the same name.
- **The same page under two sites** is decided under each on its own: Correct site under one, and
  Different site or Junk under the other, say.

### 4. Record the decision

Edit only these columns; everything else is read-only:

| Column | Enter |
|---|---|
| `decision` | `Correct site`, `Different site`, `New site` or `Junk` (dropdown) |
| `other_site_id` | Only for Different site: the id of the site of ours the source is about. Not this row's own site, and only a site in the run's seed |
| `reviewer` | Your name |
| `reviewed_date` | The date |
| `notes` | Anything worth knowing, especially where you left the decision blank |

### 5. Load what is approved: `accept-reviews`

```bash
uv run waste-ai-search accept-reviews --run-id <run_id> [--workbook <reviewed .xlsx>]
```

It reads the reviewed workbook from the SharePoint folder, else the run folder, or the one
`--workbook` names, and matches each decision to its pair by `review_id`. Case, spacing and dashes
in a decision don't matter, and a site id Excel stored as a number is read as an id
([`accept_reviews.py`](src/waste_ai_search/accept_reviews.py)).

| The pair | What loads |
|---|---|
| Auto-approved, no decision | Its values, at the site searched |
| Correct site | Its values, at the site searched |
| Different site, with `other_site_id` | Its values, at that site, where that site's fields are empty. Values not used only because they confirm or would change ours at the site searched are weighed again there, since that says nothing of the other site |
| New site | Nothing. The source is added to `new_facility_candidates.csv` once, with our nearest site where it gives a location |
| Junk | Nothing |
| For SME review, no decision | Nothing yet |

An SME's decision applies whatever the pair's status: a pair marked Junk loads nothing, even if a
later arbitration auto-approves it. Where the values accepted for one site, attribute and year
disagree, one is picked: the best tier, then the newest, then the value most sources give.

It writes `accepted_values.csv` for ingestion, and fills `found_values.csv`'s `sme_decision` (such as
`Different site (900)`) and `loaded` (`yes`, or `no` where another value was picked for the same
site, attribute and year). It prints what it did: the decisions by type, the pairs not yet decided,
the values to load (auto-approved and SME-approved), the accepted values not loaded, and the new
sites added.

**It stops, and writes nothing, on a decision it can't read back,** naming every row at fault: one
that isn't Correct site, Different site, New site or Junk; a Different site with no
`other_site_id`, one not in the run's seed, or the row's own site; an `other_site_id` beside
another decision; a filled row with no `review_id`; or two rows deciding one pair two ways.

**It applies, but points out, a Different site worth a second look:** one in another country than
the site searched, one the run didn't search, or one more than 5 km from where the source places
the facility. The seed holds our whole database, so a mistyped `other_site_id` is still a site of
ours, and its values would otherwise load there unremarked; and a site another run searched gets
values from two runs. Each is printed with its Site_Review row. Correct the id and run it again if
one is wrong.

**Run it any time.** A pair not yet decided loads nothing until it runs again, and running it again
on the same decisions writes the same files. Arbitrating a run again empties `accepted_values.csv`
to its header row and blanks `sme_decision` and `loaded`, since it sorts the values again. The file
is emptied, not deleted, because a push never deletes a blob: the emptied file replaces the old copy
in blob, so a later pull, here or on another machine, doesn't bring the old values back. `review_id`
stays the same, so run `accept-reviews` again on the same reviewed workbook. A decision on a pair the run no longer
has (the search no longer cites that page under that name) is reported, not applied.

**Re-arbitrating writes a new workbook with blank decisions.** Published to SharePoint, it goes
beside an SME's edited copy, never over it, and `accept-reviews` reads the edited copy. A pair that
appears only in the new workbook stays undecided until someone copies the decisions across: nothing
does that yet. So does a ↳ row that appears only in the new workbook, under a pair already approved:
its value loads nothing, and `accept-reviews` says how many values wait on a row the reviewed
workbook doesn't have. Without a SharePoint folder, arbitrating again overwrites the run folder's
workbook, decisions and all, so keep a copy of a reviewed workbook and pass it with `--workbook`.

A workbook from before Site_Review has a Review_Queue tab instead, and `accept-reviews` says it
can't read it. Batch 1's reviewed Review_Queue is for a one-off conversion script (build step 5 of
[the approve-sources plan](docs/plans/2026-10-21-approve-sources-site-by-site.md), not built yet).

### 6. Other tabs

- **ReadMe** — these steps, and what each file in the run folder is for.
- **Run_Config** — the run's settings and counts: pairs auto-approved and for review, values not
  used by reason, links checked, locations read.
- **Parse_Warnings** — values the pipeline dropped or could not map, and citations of the two
  dropped map sites, each attributed to its `site_id` and `site_name`. Worth scanning for
  systematic problems (a recurring unit or enum failure usually means an upstream fix, not 50
  individual reviews).
- **Definitions** — the four decisions, the tiers, and the allowed values, mirrored from the live
  database enums.

### Bing cost

Bing grounding bills per query, so the run log records the queries each site made in
`web_searches` (every retried attempt and resumed run included, since each is billed), and the
search prints the run's total. The log has a row for each site and pass: `pass_label` is blank for
the first pass, `gccs` for the gas follow-up, and a second pass's own label (see
[A second model on the largest sites](#a-second-model-on-the-largest-sites)).

**Model tokens are recorded too, and the run is priced.** Each row also gives the model that
answered (`model`) and its tokens: `input_tokens`, of which `cached_input_tokens` were read from the
prompt cache, `output_tokens`, and of those `reasoning_tokens`. Like `web_searches`, the token counts
add up across every attempt at the pass, and each pass is priced at its own model. A site searched in
two passes counts once among the searched sites. A search prints its queries, tokens and estimated
cost at list prices:

```text
Bing queries: 157 across 10 searched site(s), ~$2.20 at $14 per 1,000.
Model tokens on gpt-5.4-2026-03-05: 1,234,567 in (234,567 cached), 45,678 out, ~$3.21.
Estimated cost at list prices: ~$5.41, ~$0.54 a site.
```

The workbook's Run_Config records the same for the whole run, as `search_cost`. The prices are in
`costs.py` (`WEB_SEARCH_PER_1000`, `MODEL_PRICES`): OpenAI's list prices as of October 2026, which
Azure's GlobalStandard deployments match, and Bing's $14 per 1,000, which batch 1's bill bore out.
Azure Cost Management has the billed amount; check the table against it when prices change. A run
searched before tokens were recorded says so rather than show a cost of zero. Each site's row is written as it finishes, so an
interrupted run keeps its counts. One exception: a call abandoned at the time limit is billed for
any queries it made, but returns no response to count them from. One live site with 12 attributes made **39 queries**.
Retries are capped at 2 for both
Foundry errors (`AZURE_FOUNDRY_MAX_RETRIES`) and reported search-tool failures
(`--search-tool-retries`), so a site runs the agent at most 4 times, down from 9. A local `.env` that
still sets `AZURE_FOUNDRY_MAX_RETRIES=3` overrides the new default.

Searches per attempt are capped at 6 (`--max-web-searches`, 0 = no cap). The cap is sent as the
Responses API's `max_tool_calls`, which Foundry enforces; asking in the prompt alone was ignored. It
caps searches, not queries — each search sends about 4 queries. Each retry gets a fresh cap, so a
site that uses all 4 attempts can make up to 24 searches. On the same 10 sites:

| | No cap | Prompt only (12) | Cap of 6 |
|---|---|---|---|
| Bing queries | 235 | 233 | 189 |
| Values found | 21 | 23 | 18 |
| Review queue rows (before Site_Review) | 16 | 16 | 9 |

Repeat runs of one site vary widely (one used 12, 30 and 39 queries), so treat the last two rows as
noise-level.

**What each search did is kept.** The cap counts every call of the web search tool: its searches,
and the pages it opens or finds text in. The run log's `tool_calls` counts them, every attempt added
up like `web_searches`. The response saved for each site lists them in order, in `_search_actions`:
each search with its queries, and each page with its URL and any text it looked for. Nothing reads
them back. They show how often the cap binds, and which searches found the pages SMEs approve.

### If a site could not be searched

`sites_to_retry.csv` lists sites where the agent's own web-search tool failed. These are **not**
"no data found" — nothing was searched. They need a re-run, not review:

```bash
uv run waste-ai-search search --run-id <run_id> --force --site-ids <ids from that file>
uv run waste-ai-search arbitrate --run-id <run_id>
```

### Auditing a decision

`found_values.csv` holds every value the search found, one row per site, year, source and
attribute, with its status and `status_reason`, the source's tier and the rule that gave it
(`tier_rule_applied`), and our value for that year. So you can see why each value was approved,
sent to SMEs or not used. Once `accept-reviews` has run, `sme_decision` gives the pair's decision
and `loaded` whether the value is the one written to `accepted_values.csv`. Attributes not found
are not written anywhere. `sources.csv` holds one row per exact URL; a row's `source_id` finds its
URL, title and publisher there.

## Reverse Geocoding

Each prompt names the municipality and state our site is in, so the agent can search the right
town and reject a page about another one. Brazil's seed has them from SINIR; every other site's
are looked up from our coordinates when the site is searched, with Nominatim (the locate step's
geocoder, `WASTE_AI_SEARCH_GEOCODER=nominatim`), one request a second. Each answer goes into
`data/geocode_cache.jsonl`, so a site is looked up once whatever run it is in; Mexico's 1,553
sites take about half an hour the first time. `--no-geocode-cache` turns it off. Azure Maps can't
look points up here yet, so with it the prompt names no town.

A cache row holds a municipality and a state, never a street: a street address next to our site
would say more of our point than the prompt's rounded coordinates.

```json
{"site_id":"6086","coord_key":"17.38000,-98.02100","municipality":"San Sebastián Tecomaxtlahuaca","admin1":"Oaxaca","admin2":"","formatted_address":"","geocoder_provider":"nominatim","geocoder_confidence":"","geocoded_at":"2026-10-30"}
```

The run keeps the place each prompt named in `site_places.json`. The locate step names every state
it meets, ours and each source's `stated_state`, as Nominatim does ("N.L.", "Nuevo Leon" and
"Nuevo León" are all "Nuevo León"), in `state_names.json`; arbitration reads both, offline, to
show `our_place` beside `source_place` on Site_Review and flag a source in another state.

**Our other sites near each site.** Since 30 October 2026 each prompt also names our other sites
in the same country that are within 15 km, or within 60 km and in the same municipality and state,
nearest first. Las Varas, in Madera, gets "BASURERO NICOLAS BRAVO (Madera, 18 km away)" and two
more. They come from the whole seed, so a site skipped for its generic name is named too, but as
one: 'unnamed site, recorded as "BASURERO MUNICIPAL" (Madera, 31 km away)'. The ten nearest are
named and the rest counted.

- The 15 km is there because the geocoder names a site in a town by the town ("Texcoco de Mora")
  and one outside it by its municipio ("Texcoco"), so the names alone miss some.
- The 60 km is a cut-off for matching municipalities as well as for lookups: only our sites that
  close have their places looked up (236 for the Mexico sample, against 841 within 150 km), so a
  site farther off in one of the largest municipios is named only if it is within 15 km.
- Our records under 1 km away go in `our_records_within_1_km`, the rest in
  `our_other_sites_nearby`. Mexico's INEGI rows hold some dumps twice at the same point (8690 and
  8689 are 3 m apart), and the prompt gives our point only to about 1 km. So the prompt says such a
  record may be this site recorded twice, and a page about it may be cited for both, which is how
  `possible_duplicates.csv` finds a site recorded twice.

Every prompt also says that a town can have waste sites we don't hold, such as a new landfill that
replaced an old dump. A page about the town's waste, another of its sites, or one of our other
sites nearby is not about our site unless it also describes ours: its name, place or history. From
such a page the agent uses only what it says about ours. In test10-mex-sample2, the first search
gave Las Varas two NADBank pages and a Chihuahua state government page about Madera's new landfill.
None named Las Varas, and the second search declined pages like them.

A generic phrase such as "basurero municipal" doesn't by itself name a site. But a page about the
dump of the place our site is named after, or about the town's dump where we hold no other site in
that town, describes ours unless it speaks of a replacement or another location. The agent returns
it and says in `evidence_summary` that it names the site only by its place. In the 31 October Mexico
tests the judges called 34 of the 84 good pages ours on that basis ("basurero municipal de
Tolcayuca"), while Terra and Sol declined some, such as San Ignacio Cerro Gordo's, where we hold no
other site nearby. Where our name looks like an operator's, only the town's dump counts: 7 of the 8
sites named "Promotora Ambiental de la Laguna" are outside La Laguna.

The rule adds about 660 characters to every prompt (Thunder Bay's is 20,483, under the size test's
20,600). Where a site has others nearby, the first named adds about 210 more and each further one
about 65.

This is separate from the locate step's address geocoding, which places the addresses sources
state (see [Locating each source](#locating-each-source)).

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

`check_db_schema.py` checks the spec's columns, and the types the code expects of them, against
`transformed.transformed_osm`, a table built to the same spec. This repository no longer writes a
standardized table of its own, so there is no AI search table here to check.

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
to a coordinate-only search. Since the approve-sources plan a location is no longer asked for, so
those sites have nothing to ask (see rule 2 under
[What is actually searched](#what-is-actually-searched--read-this-before-a-full-run)).

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

For a Spanish-language site, neither the translation nor English is searched, except for
gas-project records (see [What the agent returns](#what-the-agent-returns)).

Every prompt also says our recorded name may be misspelt or garbled, and asks the agent to try
close spellings before it concludes a site is undocumented. At 5099, "RELLENO SANITARIO EL ASALTO",
Terra's six searches were all for El Asalto and found nothing; Sol tried El Salto, and found the one
page judged ours.

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
3) ~~Parallel processing~~ — done: sites are searched 4 at a time (`--workers`);
4) Saving search result json files to blob storage;
5) Process to promote SME validated data into a raw AI_discovery data source;
6) Process to shape AI_discovery data source into standardized facility schema, similar to other raw data sources.
7) Rerun consolidation;
8) Run through modeling pipeline with AI discovered activity data folded in;

