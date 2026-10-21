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
outputs/runs/<run_id>/<run_id>_candidate.xlsx
outputs/runs/<run_id>/raw_foundry_responses/site_<site_id>.json
outputs/runs/<run_id>/status/site_<site_id>.json
```

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
| `Broken` | 404, 410 or a redirect loop | Not promoted; the source goes to Leads |
| `Unverified` | 403, 429, timeout, TLS error | Left alone — usually bot-blocking, not a dead page |

A broken URL is retried with a literal comma for `%2C` and with the trailing slash toggled; a
respelling that works replaces it. Existing escapes are never decoded otherwise.

**Only public addresses are ever contacted.** Cited URLs are untrusted and are fetched from a machine that is often on the VPN, so every hop — redirects included — is resolved first and refused if any address is private, loopback, link-local (including the cloud metadata address) or otherwise non-public. The connection then goes to the address that was checked, so a second DNS answer cannot redirect it. A refused URL is `Unverified` and never fetched; a malformed one is `Unverified` too, rather than stopping the check.

The fetching happens during **search**, which needs the network anyway; `arbitrate` only reads
`link_check.json`, so it stays offline. A value with one dead source and one working source is still
promoted from the working one.

```bash
uv run waste-ai-search check-links --run-id <run_id>   # backfill an older run, then arbitrate
uv run waste-ai-search run --no-link-check ...          # skip checking
uv run waste-ai-search arbitrate --keep-broken-link-evidence ...  # let dead sources stay promotable
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

`evidence.csv` records both results for every row, in `quote_on_page` and `location_on_page`. In the
review queue, a winning source whose quote is not on its page, or only partly, gets a **CHECK** note
in `evidence_summary`, and the cell is shaded. So does one whose stated location isn't on its page,
where that location counted: on a `found_location` row, and wherever the source was filed by it.
Nothing is dropped or downgraded for it, since a page can say something in a way the check can't
follow, in an image or a map, say. Run_Config counts `page_text_kept`, `quotes_on_page` and
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

The agent reports where each source puts the facility, copied as the source writes it (see
[What the agent returns](#what-the-agent-returns)). Right after the link check, the search phase
turns each of those into a point and says how far it can be trusted, as its `location_basis`
([the plan](docs/plans/2026-10-20-match-sources-by-location.md), step 3;
[`locate.py`](src/waste_ai_search/locate.py)):

| The source states | Becomes | Basis | Used for |
|---|---|---|---|
| Coordinates with 3 or more decimals (or seconds) | Parsed locally | `coordinates` | Matching, and a location value |
| A UK OS or Irish grid reference, 100 m or finer, ITM metres labelled ITM, or an easting and northing each labelled (`E 531090 N 180600`) in the UK or Ireland | Converted locally (`pyproj`) | `grid reference` | Matching, and a location value |
| A street address placed on its building or address point | Geocoded | `rooftop` | Matching, and a location value |
| A street address placed only on its street | Geocoded | `street` | Matching only |
| Coordinates with fewer decimals, or a coarser grid reference | Parsed locally | `hint` | Nothing yet: too approximate |
| Anything else: a town or postcode match, a place description, a PO box | — | `none` | — |

Coordinates are read as `lat, lon`, in decimal degrees or degrees, minutes and seconds, with
hemisphere letters or words (`North`, `West`). `lon, lat` is read only where that is unambiguous: a
hemisphere or a label says so, or the first number is beyond 90. A header naming both axes, such as
`Lat/Long:` or `(lat, long)`, gives their order, and a well-known-text `POINT(lon lat)` is read
longitude first. In prose, a number followed by a unit (km, m, ha,
%) is a distance or an area, not a coordinate, and the first pair in range is the one used.

A labelled easting and northing (`E`/`N`, `Easting`/`Northing` or `X`/`Y`, in metres) is read in
the country's grid: the British National Grid in the UK, and in Ireland the Irish Grid or ITM,
whose numbers never overlap. A UK source that says Irish Grid, as Northern Ireland's do, is read
in Ireland's grids. Elsewhere the grid is unknown, so such metres are not read. The `E` is never
taken for an Irish Grid square, and where a text holds a licence number and a grid reference, the
reference 100 m or finer wins.

"Matching" is filing each source under the site of ours it is about (see
[Matching each source to a site](#matching-each-source-to-a-site)).

Three guards throw a location out, with the reason in `evidence.csv`:

- **Copied from the prompt.** Coordinates equal to the rounded point the prompt gives. In batch 1,
  whose prompts carried our full point, the agent credited our exact OSM centroid to an Ontario
  registry six times. The prompt no longer carries that point, so a source stating it exactly is
  not a copy but a source that agrees with us, such as the regulator our seed came from, and it is
  matched and confirms our location. A response cached before build step 4, attribute-first, came
  from a prompt with our full point, so there a point within 10 m of ours is still copied: batch 1's
  six copies were each within a few metres. Left in, one search's copy of Thunder Bay's registry
  entry filed every other search's citation of it under the copier's site.
- **wastebook.co.uk.** Its coordinates are postcode centroids.
- **A doubtful pair.** Where a source gives coordinates and an address, the coordinates win; if the
  address lands more than 2 km from them, neither is used. Coarse coordinates (a hint) give way to
  a located address that agrees with them: within 2 km plus the hint's own coarseness, about
  0.8 km for 2 decimals. An address farther off, such as an operator's office, makes the pair
  doubtful too.

**A facility name is never geocoded.** Geocoders find landfills by name from OpenStreetMap, where
most of our sites came from, so it would hand our own point back. Only text the agent labelled a
street address is sent, with any leading facility name taken off, and text that is only a name is
not sent at all. The names are the source's own, any `found_facility_name` it gives, and our
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

### Geocoder

No geocoder is configured by default: addresses stay unlocated, with the reason "no geocoder
configured". Choose one with `WASTE_AI_SEARCH_GEOCODER` in `.env`, or with `--geocoder` on
`search`, `run` and `locate`. Each address is sent with its site's country.

| Geocoder | Settings | How a match is read |
|---|---|---|
| `azure-maps` | `AZURE_MAPS_SUBSCRIPTION_KEY`, or `AZURE_MAPS_CLIENT_ID` to sign in with Entra ID (`az login`) | [Get Geocoding](https://learn.microsoft.com/en-us/rest/api/maps/search/get-geocoding?view=rest-maps-2026-01-01), api-version 2026-01-01. An address matched to its rooftop or parcel is `rooftop`; an interpolated address, a block or an intersection is `street`. A match it had to broaden (`UpHierarchy`), an ambiguous one, or Low confidence is not used. The account is IT's to create. |
| `nominatim` | `NOMINATIM_URL` (the public instance if blank) and `NOMINATIM_USER_AGENT` (default `waste-ai-search (RMI)`, which names the application, never a person) | OpenStreetMap's geocoder, at most one request a second. An address point or building is `rooftop`; a road, or a house number Nominatim interpolated along its street (`place=house` on a way), is `street`, as with Azure Maps. A mapped feature that carries the address, such as the landfill itself, is not used: it would be OSM's own point. Enough for the live test. |
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

## Matching each source to a site

Everything the agent returns used to be filed under the site it was asked about, and nothing
checked that a source was about that site. Now arbitration files each source under the site of
ours it is about, by where the source places the facility, with its name as a check (build step 5
of [the plan](docs/plans/2026-10-20-match-sources-by-location.md);
[`match.py`](src/waste_ai_search/match.py)). It runs offline, like the rest of arbitration.

**Our sites** are every site in the run's `seed.csv`, in every country. A `--batch` run's seed holds
the whole corpus; a run seeded with `--iso3` holds only those countries, so a source about a site
across the border can't match it there.

**Distance** is to a site's approximate edge: the distance to its point, less the radius of a circle
with its `area_square_meters`, the radius capped at 2 km. A site with no area is its point. This is
how entity linkage's `precise_v1` measures it (RMI/waste_data_ingestion_pipeline#225).

**A location** is one the locate step read as coordinates, a grid reference, or an address placed
on its building or its street. A hint, a location copied from the prompt and a doubtful one count
as none.

| The source has | Rule | Filed under |
|---|---|---|
| A location | 1. Exactly one of our sites within 2 km | That site, whether or not it was the one searched |
| A location | 2. Two or more within 2 km | The one its name strongly matches, if exactly one does. Otherwise a **tie**: the site searched if it is one of them, else the nearest (among those its name fits, where it fits several). The pairs go on `possible_duplicates.csv`. |
| A location | 3. None within 2 km, but its name strongly matches the site searched, within 5 km of it (25 km where our location is flagged inexact) | The site searched. Its location is reviewed as a coordinate correction. Where we hold no location for the site searched, the name alone decides. A name with fewer than two distinctive words never matches strongly, which leaves most of Brazil's sites without this rule (see Brazil, under [What is actually searched](#what-is-actually-searched--read-this-before-a-full-run)). |
| A location | 4. Anything else | No site. Listed in `new_facility_candidates.csv`. |
| No location | 5. Its name strongly matches exactly one other site of ours within 200 km of the site searched, and not the site searched | That site, **moved by name** |
| No location | 6. Anything else | The site searched, flagged **no location**. The flag names any site of ours within 200 km that the name also fits, ties included, and says if the same page was cited for other searches. `corrected_site_id` offers those sites, and the sites whose searches cited the page. |

**Names** are scored by [`facility_names.py`](src/waste_ai_search/facility_names.py), which
started as a copy of entity linkage's name scoring (#225) and is this repository's own. Matching is
independent of entity linkage, since AI search rows join the facility named by the wastemap_id they
carry, so the two needn't agree. Case,
accents, punctuation, company suffixes and permit codes are ignored, and generic words are dropped
("landfill", "sanitary", "waste management", and more this corpus needs: words for a site's state,
such as closed, former and disused; tip, refuse, depot, grounds, nuisance, lieu, enfouissement, technique and LET; the words of other
kinds of site, such as Australia's "Resource Recovery Centre", Canada's "Integrated Waste Management
Facility" (IWMF), France's "Installation de Stockage de Déchets Non Dangereux" and Mexico's "tiradero
a cielo abierto"; and small words such as "for", "in"
and the French "à"). The score, from 0 to 1, is the higher of the share of the shorter name's words found in the longer one and how alike the
two read as strings, so "Seaton Meadows Landfil" still matches "Seaton Meadows". Conflicting
qualifiers (North and South, Cell I and Cell II) score 0.

- A **strong** match scores 0.8 or more and shares at least two distinctive words, counting a
  qualifier both names have: in "North Bay" it is part of the place's name. One shared word is never
  strong: "Eastview Landfill" is both a Brandon and a Guelph site, 1,716 km apart. So the SME's
  "Merrick Landfill - North Bay" moves to our North Bay Landfill, while a source naming only
  "Merrick Landfill" shares no word with it and stays with the site searched.
- A name with no distinctive words left, and our placeholders and generic labels ("OpenStreetMap
  Landfill", "domestic landfill"), can't be judged. Nor can our names from Global Plastic Watch,
  which are addresses looked up from its point ("Emu Mountain Road, Doonan, Noosa Shire,
  Queensland"), as #225 treats them: 662 of batch 2's 856 sites are named so, and two in one
  district share every word.
- A source's `facility_name_english` is compared with our `site_name`, and its
  `facility_name_in_source` with our `original_site_name` where we have one. The higher score
  counts. A response cached before build step 4 has no English name, so its name is compared as
  written.

**The same page counts once.** A source is its exact URL, query string included, with the facility
name it gives, so a registry page naming many sites is several sources, and one cited by several
searches is one. A page cited with no name is told apart by the location it states. A source with
a location is matched once, however many searches cited it, and all their citations are filed
with it. One with no location is matched for each search on its own, since rules 5 and 6 depend on
the site searched. Located sources that match none of our sites and lie within 2 km of each other
become one new-landfill candidate. Where two of our sites within 2 km of each other were both
searched and cited the same page, the pair goes on `possible_duplicates.csv`: E-PRTR re-registered
many facilities under new ids.

**Only the run's selection gets values.** The selection is every site in the countries the run
searched (those with a cached response), and in a default-excluded country (the US, Brazil) only
the sites the run itself searched. A source filed under a site outside it, in another batch's
country say, goes on `outside_run_matches.csv` instead of the review queue, since ingestion #261
stops on a facility with values from two runs.

**A page listing many landfills is read one entry at a time.** A search reports only the entry it
used, under the name the page gives it, so a regulator's list of twelve landfills can be several
sources, one per entry, each matched by its own location or name. A name the page gives to places
more than 2 km apart, a list of "Closed Landfill"s say, is one source per place, and a citation of
it with no location stays with its own search. The rest of such a page goes unread, so arbitration
lists it in `datasets_found.csv` for ingestion to load once, as a source of its own.

**A site's trust still decides what is asked of SMEs.** Where the name or coordinates of the site a
source is filed under come from a Tier 1–2 source, the source's name and location still match it,
but they are recorded in `evidence.csv`, not queued as `found_facility_name` or `found_location`
values. This is judged on the site the source is filed under, not the one searched.

**"Not a Waste Facility"** is filed only under the site searched, and only where that site was
offered the check (see [Not a waste facility](#not-a-waste-facility-wp-525)). A source that matching
would file anywhere else gives its verdict to Leads, and is never a new-landfill candidate: one
placed more than 2 km from every site of ours is counted as `match_not_a_waste_facility`, and its
other values are recorded in `evidence.csv` only.

The 5 km identity check, a dropped map page's coordinates read for it, and the note on a value
whose winning source named another facility are gone: matching replaces all three.

Run_Config counts each outcome, a source with a location once and one without once per search
that cited it: `match_located`, `match_named_among_several`, `match_tie`, `match_name_nearby`,
`match_new_facility`, `match_not_a_waste_facility`, `match_moved_by_name`, `match_no_location` and
`match_outside_run`, beside `new_facility_candidates` and `possible_duplicates`.

**On the batch-1 replay**, a plumbing check since batch 1's sources rarely give a location, 85
located sources matched one site, 2 tied, 8 matched none, 91 moved by name, and 4,650 citations had
no location. The queue has 6,414 rows (6,562 before), never two for one site, attribute and year.
80 of them are filed under a site other than the one searched, at 25 sites: Thunder Bay's 15 rows,
moved by name from searches of nearby "domestic landfill" sites, Fredericton, Otter Lake, Sault
Ste. Marie, North Bay (the SME's Merrick example), Prairie Green, Moose Jaw and Benaraby among
them. The six points batch 1's agent copied from our own count as no location. Rule 6 leaves some
known cases with the site searched, flagged: Prairie Green's pages at four sites 283-633 km from it,
West Perth's page at 14 unnamed sites, and Brandon's Eastview pages at 12 Manitoba sites and at
Guelph's Eastview; `corrected_site_id` offers the sites whose searches cited each page. 41 pairs are possible duplicates, Cowpen Bewley's and Foxhall's E-PRTR records among them.

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
reads the gas collection findings still waiting in the review queue. They are keyed by the site
each source is filed under, so the follow-up searches that site, and only a site in the run's
selection.

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

# Phase 3 - `run` does this for you. By hand: fold found values, still awaiting review, back into
# the seed, then search ONLY the sites where gas collection was discovered - refresh-seed prints
# the command.
uv run waste-ai-search refresh-seed --run-id pilot10
uv run waste-ai-search search --run-id pilot10_pass2 \
  --input-csv outputs/runs/pilot10/refreshed_seed.csv --site-ids <ids printed above>

# After review - add the values SMEs rejected as "New facility - not in our database" to the run's
# new_facility_candidates.csv. Reads the SharePoint copy of the workbook, else the run folder's.
uv run waste-ai-search collect-new-facilities --run-id pilot10 [--workbook <reviewed .xlsx>]
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

The agent answers **source first** (build step 4 of
[the source-matching plan](docs/plans/2026-10-20-match-sources-by-location.md)): each source it
used, with the facility that source describes and the values it gives.

```json
{
  "site_id": "13755",
  "sources": [
    {
      "url": "https://...", "source_title": "...", "publisher": "...", "source_type": "...",
      "publication_date": "...", "language": "...", "paywall_flag": false,
      "facility_name_in_source": "the facility's name, exactly as this source writes it",
      "facility_name_english": "that name in English",
      "stated_location": "the facility's location, exactly as this source writes it",
      "stated_location_kind": "coordinates",
      "values": [
        {"attribute_name": "operator", "value": "...", "unit": "", "value_basis": "Direct",
         "confidence_score": "High", "value_date": "2024", "evidence_summary": "...",
         "search_terms_used": "...", "quoted_evidence_short": "..."}
      ]
    }
  ],
  "unverified_leads": [],
  "search_notes": "..."
}
```

- `facility_name_in_source` is the name as the source writes it; `facility_name_english` is the
  agent's English rendering, since our names are in English. Either may be blank.
- `stated_location` is the source's coordinates, grid reference or the site's street address,
  copied as written and never worked out, and blank if the source gives none. Its kind is one of
  `coordinates`, `grid reference`, `street address`, `place description` or `none`. A source that
  gives coordinates and an address writes them `a | b`, kinds `coordinates | street address`.
- Each value keeps the fields arbitration has always read.

**The agent reports what each source says, and code decides.** It still researches only the site
asked about. A source that turns out to describe a different facility is reported anyway, with that
facility's name and location, but the agent does not go on researching it; from a registry that
lists many sites, only the entry it used. A source that names or locates the facility but gives no
value is reported too. Arbitration then files each source under the site of ours it is about,
which may not be the site searched (see [Matching each source to a site](#matching-each-source-to-a-site)).

**The prompt gives our coordinates rounded to 2 decimals (about 1 km),** with the municipality
and admin names where the seed has them: enough to search with, and nothing worth copying. With
the full point in the prompt, the agent once credited our own OSM centroid to a registry. The
reverse-geocoded `formatted_address` is left out for the same reason. The search cap
(`--max-web-searches`) is unchanged.

Only Brazil's seed has place names (SINIR's municipality and state). Every other country's come
only from a reverse-geocode cache passed with `--use-geocode-cache` (see
[Reverse Geocoding](#reverse-geocoding)), and no such cache exists yet. So outside Brazil the
prompt names the site, its country and the rounded point, and no town. Batch 1's prompts had the
full point and no town either, so a comparison with batch 1's yield measures the rounding, not the
loss of place names.

**Runs cached before this still arbitrate.** One reader,
[`payload.py`](src/waste_ai_search/payload.py), turns any cached response into this shape, and
everything that reads a response goes through it: the evidence, the link check, the run log's
counts, validation, the merge of the follow-up pass, retry detection and the agent's own leads. An
older, attribute-first response's citations become one source per exact URL, each with the values
of the attributes that cited it. Its found name is the in-source name of the sources cited for it,
and its found coordinates the stated location of the sources cited for both; half a location is
none. Evidence ids and the agent's order are kept. Each value keeps how its own citation labelled
the page (source type, date, publisher), so it is tiered as it was, even where the gas follow-up
pass labelled the same page differently. Only a label its citation left blank comes from the
page's other citations, so an undated citation of a dated page gets its date: on batch 1 that
lifts 118 citations from the undated cap to their type's tier, and lowers none.

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

### What is actually searched — read this before a full run

Four rules narrow the search. The first three mean a full run queries **12,392 of 15,537
facilities** and makes **167,410 attribute-requests** instead of 22 x 15,537 = 341,814. The fourth
removes more; those figures predate it.

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

**2. Brazil is excluded by default. When searched, it is coordinates only, and only where the
location is flagged inexact — 3,913 of 4,225 sites, all from SINIR.**

For a Brazilian facility, the search asks only where the facility is (`found_location`): it looks
for sources that state its location, and asks for no values — **and only when `is_location_exact`
is FALSE.** Every other Brazilian attribute comes from a
government source, so searching it risks overwriting better data than the search can find.

SINIR's coordinates are the centre of a municipality, not the dumpsite, and most of its names are
generic ("Lixão", "Aterro Controlado"). So the seed carries the municipality and state SINIR
recorded (`facility_city_location`, an IBGE code, named from `raw_sinir_general_city_data`) into
`municipality` and `admin1`, and the prompt passes them on. Without them the agent guessed the town
from the city-centre point and, on the WP-543 test, returned dumps 140 and 237 km away.

With the municipality the agent found the right facilities, but in reports that publish no readable
coordinates: **0 of 20** sites got usable coordinates either way. So Brazil is excluded by default
like the US, and searching it is opt-in with the same three overrides (`--iso3 BRA`, `--site-ids`,
`--include-excluded-countries`). Matching against OSM/GPW landfills within the municipality is the
likelier fix. Opting in to Brazil does not lift rule 4, and many of SINIR's names are generic
labels such as `Dump`, so pass `--include-unsearchable-names` too to search those.

Unknown is **not** treated as inexact: only an explicit FALSE triggers a coordinate search.

**Since build step 5, few of Brazil's found coordinates reach review.** A source's location is
filed under the site searched only where it falls within 2 km of it, or its name strongly matches
the site's within 25 km (rules 1 and 3 of [matching](#matching-each-source-to-a-site)). SINIR's
point is the municipality's centre, so a dump's own coordinates usually lie farther than 2 km
from it, and 1,484 of the 2,657 searchable inexact sites have names with fewer than two
distinctive words ("Aterro Controlado", "Lixão de Itapipoca", "Jeremoabo dump"), which can never
match strongly. A source placing such a dump 8 km from the municipality's centre goes to
`new_facility_candidates.csv`, not to review as the site's `found_location`, as it did before. This
is open: one option is to file a located source within 25 km of a site with an inexact location
under it unless its name strongly fits another of our sites, so that the name vetoes a match
rather than being needed for one.

**3. Everywhere else: gap-fill, plus the gas-capture gate.**

- Identity (`found_facility_name`, `found_location`) is requested **except** where the seed's value came from a Tier 1–2 source: the name for about 10,600 facilities, and both coordinates for 4,063 of the 12,122 searchable sites (Mexico INEGI, E-PRTR, Canada GHGRP). This needs per-attribute provenance; with none, the attribute is still asked. Coordinates flagged inexact are still searched.
- **Where our coordinates are trusted, the sources' locations are recorded, not reviewed.** Every source still states where it puts the facility, so the wrong-facility check runs for these sites too; what is skipped is the push to find a location, and a `found_location` row for SMEs (design doc F29).
- Metadata attributes are requested **only where that facility's baseline is empty**.
- The seven `gccs_*` attributes are requested **only where gas collection is known present** —
  212 to 603 facilities each, not 13,000. See the two-pass note above for why this loses nothing.

Both country rules live in [`schema.py`](src/waste_ai_search/schema.py) as
`DEFAULT_EXCLUDED_ISO3` and `COORDINATES_ONLY_ISO3`, so changing policy is a one-line edit.

**Why these rules exist, in one line each:** the US is already Tier 1 covered by GHGRP and LMOP, so
searching it manufactures equal-tier conflicts rather than data; Brazil's non-coordinate attributes
are government-sourced and should not be overwritten by a web search, and its coordinates were not
found by web search on the WP-543 test.

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
| No name | An empty name, or the filler ingestion gives an OSM polygon with no name, unless the source's own spelling is a name | `OpenStreetMap Landfill` |
| Not a name | A URL, a bare number or a survey note that ended up as the name | `update the tag landuse after survey`, `4000` |
| Generic label | Only says what kind of site it is (`is_generic_site_name`). Initials and route codes count as a name: `K & W Landfill` and `SR 85 Landfill` are searched | `domestic landfill`, `Landfill`, `Municipal Dump`, `Landfill II` |
| Mining feature | A spoil heap or similar, matched in the English name or the original (отвал, террикон, відвал) | `Overburden Blade`, `Terrikon` |

Nothing in the database changes: a skipped site stays in WasteMAP and Climate TRACE. It just isn't
web-searched until it has a name worth searching for. The test leaves some description-only names
in, such as `Sanitary Landfill` and `Controlled Landfill`, and names still in Cyrillic.

The generic test knows only English words, so it reads the English name, not the source's own
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

**22 attributes are searched.** No site is asked for all 22 — the scoping rules above decide
which ones a given facility gets. Three more gas-capture volumes are no longer searched but are
still read from the seed and from older cached responses (see below).

**Identity — requested unless a Tier 1–2 source already supplied it.** These are the evidence that
the agent researched the *right* facility.

| Attribute | Type | Unit asked for | → standardized column |
|---|---|---|---|
| `found_facility_name` | text | — | `facility_name` |
| `found_location` | `lat, lon`, from the sources' stated locations | — | `latitude`, `longitude` |

`found_location` is not a value the agent returns. Every source states where it puts the
facility, the locate step turns that into a point (see [Locating each source](#locating-each-source)),
and a site's found location is resolved from the usable points of the sources filed under it —
coordinates, a grid reference or a rooftop address — by the usual rules: the most credible source,
then the newest by publication date. It is one value, `lat, lon`, so it is reviewed and loaded as a point or not at
all. A street-level address or a hint is never a value. A map directory is Tier 3 for a location,
as it was for coordinates. Asking for it, where our coordinates are not trusted, means the prompt
stresses finding a source that states the location, and its row reaches the review queue; where
the coordinates of the site a source is filed under are trusted, its location is still in
`evidence.csv`, and nothing is queued.

**Which site a value belongs to** is decided by matching each source to the site it is about,
by where the source places the facility and with its name as a check (see
[Matching each source to a site](#matching-each-source-to-a-site)). It replaced the 5 km identity
check, which measured how far a site's sources put it from the seed and only added a note, and the
note on a value whose winning source named another facility. A source's location is reviewed as
the `found_location` of the site it is filed under, where that site's coordinates are asked for.

A location that matches the seed's to comparison precision (six significant digits) confirms it,
and like any confirmation it is not queued, so nothing moves. One equal to the prompt's rounded
point is copied from the prompt and is no location at all. One that differs by more, even by a few
hundred metres, goes to review like any other value. There is no "same place" distance any more. The 1 km rule, and the 0.5 km rule that replaced it, counted a
permit's coordinates near OSM's as confirming OSM's, when the permit's point is the one worth
having.

**Newer sources win.** The agent is told to prefer the most recent source when sources disagree, and to base `facility_status` on the latest evidence it can find (a closure notice, current opening hours, recent news) rather than an older permit or directory listing. Arbitration follows the same rule: within a tier the newer `value_date` wins, and for `facility_status` only, a lower-tier source down to Tier 3 may win when it is at least 3 years newer than the source or baseline it replaces. Such a win goes to review like any change, and `resolution_rule` states the dates.

`found_facility_name` must be **specific**: a name that identifies the site on its own ("Wonthaggi Transfer Station"), not a generic label ("Waste Management Facility"). If a source gives only a generic name, the agent adds the municipality, town or operator that the same source names ("Bass Coast Shire Waste Management Facility"), never a place the source doesn't mention.

**Gap-fill — requested only where that facility's baseline value is empty.**

| Attribute | Type | Unit asked for | → standardized column |
|---|---|---|---|
| `facility_status` | enum | — | `facility_status` |
| `facility_type` | enum | — | `facility_type` |
| `bulk_waste_type` ‡ | enum: `municipal solid waste`, `inert waste`, `others` | — | *none yet — review layer only* |
| `operator` ‡ | text | — | *none yet — review layer only* |
| `opening_year` | integer year | `year` | `opening_year` |
| `closing_year` | integer year | `year` | `closing_year` |
| `has_landfill_gas_collection` | boolean | — | `has_landfill_gas_collection` |
| `annual_incoming_waste_metric_tonnes` | numeric | `metric tonnes/year` | `annual_incoming_waste_metric_tonnes` |
| `waste_in_place_metric_tonnes` | numeric | `metric tonnes` | `waste_in_place_metric_tonnes` |
| `area_square_meters` | numeric | `square meters` | `area_square_meters` |
| `waste_depth` | enum (derived) | `meters` in, category out | `waste_depth` |
| `has_cover` | boolean | — | `has_cover` |
| `cover_types` | enum array | — | `cover_types` |
| `has_biocover` | boolean | — | `has_biocover` |

‡ **Review-layer only: `operator`, `bulk_waste_type`, `has_flare`, `flare_efficiency`.** The upstream standardized spec has no column for them yet, so a found value reaches the review queue but has no standardized column to load into. Like every attribute, a found value goes to review whatever its tier. Loading them is a follow-up once upstream adds the columns. `consolidated_facility` has none of them either, so each is asked of every facility its scoping rules allow.

`bulk_waste_type` is the waste that makes up **most** of what a site receives. It separates
municipal solid waste (household and similar waste, which decomposes and generates methane) from
inert waste (construction and demolition rubble, soil, stones); `others` is mainly industrial,
hazardous or mining waste or sludge. A site taking mostly municipal waste plus some rubble is
municipal solid waste. There is no `unknown`: with no source on the waste received, or no one
main type, the value stays NULL, so the site is asked again on a later pass.

`waste_depth` is the only **derived** categorical. The agent still reports a number and its
unit; the pipeline converts to metres, then bins on the spec's 5 m boundary. The metre value is
kept in `normalized_value` as evidence and the category lands in the column. A depth of zero or
less is not a measurement, so it is stored NULL rather than binned to `<=5m`.

`has_biocover` is **not** derived from `cover_types` — a plain soil, clay or sand cover is not a
biocover, and the prompt says so explicitly. `consolidated_facility` now carries the column but it
is entirely NULL today, so the gap-fill rule still fires for every facility.



**Gas capture — additionally gated on `has_landfill_gas_collection` being known TRUE.** Asking a
facility with no capture system about methane flaring buys a certain "nothing". Nothing is lost by
the gate: `has_landfill_gas_collection` is itself gap-filled, so `refresh-seed` promotes a
discovery into the baseline and the next pass picks up that facility's GCCS attributes.

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
canonical unit. A unit it cannot convert produces a warning and the value is **not promoted**, so
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

The value is **handled like any facility_type**. It goes to review whatever its tier, as every found
value does, on the site's own `facility_type` row in Review_Queue — there is no separate tab. It is
kept only for the site searched, and only where that site was offered the check: a source that
matching files under another site, or under none, gives its verdict to Leads, and a place that is
no landfill is never listed as a new one. It also
reaches the next pass's seed, which `refresh-seed` builds from values still awaiting review. Where
the same run also found the site closed, the likeliest misreading, the row's `evidence_summary` says
so.

> **Pending upstream.** The database's `facility_type` enum and `chk_facility_type` do not carry
> `Not a Waste Facility` yet, so consolidating a validated one will be rejected.
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

### Outputs

| File | Purpose |
|---|---|
| `<run_id>_review.xlsx` | **The SME artifact.** Every found value for an empty field, whatever its tier, and every disagreement with our value from a Tier 1–3 source at least as credible as ours, with dropdowns: one row per site, attribute and, for a yearly attribute, year, under the site each source is about. Weaker disagreements are under Leads. Ingestion reads the SMEs' decisions from it. |
| `resolved.csv` | A header and no rows. Nothing is accepted automatically, and ingestion accepts it empty. Runs from before (batch 1) wrote the values they accepted here. |
| `evidence.csv` | One row per source-backed claim, confirmations of our values included. The audit trail. Its `source_id` finds the source in `sources.csv`. Every row also gives the facility its source describes (`facility_name_in_source`, `facility_name_english`), the location it states (`stated_location`, `stated_location_kind`) and the point read from it (`location_basis`, `located_latitude`, `located_longitude`, and `location_reason` when it is not usable). A source that states a location has a `found_location` row of its own. Its `site_id` is the site the source is filed under, blank for a likely new facility; `searched_site_id` is the search that found it, and `match` says why. |
| `source_locations.json` | Each stated location's point, keyed by its text and country (see [Locating each source](#locating-each-source)). |
| `page_text/` | The text of each page that loaded, one gzip file per exact URL named by its `source_id` (see [The text of each page that loads](#the-text-of-each-page-that-loads)). `evidence.csv`'s `quote_on_page` and `location_on_page` say whether each source's quote and stated location are on it. |
| `sources.csv` | One row per exact URL, with a `source_id` derived from it (see below). |
| `supplementary_leads.csv` | Findings below the credibility floor, plus the agent's own leads. `searched_site_id` is the search that found each; a weaker disagreement weighed several sources, and names every search behind them. |
| `parse_warnings.csv` | Dropped values, dropped map-site citations, unit failures, unmappable enums. |
| `skipped_sites.csv` | Sites the run skipped because their name gives a search nothing to look for, with the class and rule. Each search adds to it. |
| `new_facility_candidates.csv` | For ingestion: landfills none of our sites is. One row per group of located sources more than 2 km from every site of ours (grouped within 2 km of each other), with their URLs, names as written and in English, stated locations, the point and its basis, our nearest site and its distance, and the searches that found them. `collect-new-facilities` adds the values SMEs rejected as "New facility - not in our database" (`found_by` = `SME`); arbitrating again keeps those. |
| `possible_duplicates.csv` | For ingestion (entity linkage's #225 tackles E-PRTR re-registrations): pairs of our sites, with names, distance apart, the sources that hit both and why: one source placed within 2 km of both that its name couldn't settle, or one page cited by both sites' searches where they lie within 2 km. |
| `outside_run_matches.csv` | Sources filed under a site outside the run's selection: the source, the site, its country, and the search that found it. Not reviewed. |
| `datasets_found.csv` | For ingestion: pages that look like datasets, such as a regulator's list of its landfills. A search reports only the entry it used, so the rest of such a page goes unread, and every site on it pays its own search to find it again; loaded once through ingestion, as E-PRTR is, it would cover them all. Listed when it names or places 2 or more facilities, or when a regulator, registry or statistics publisher's page is found by 3 or more searches (a town's landfill page is found by every nearby search too). Names count apart by their distinctive words, and a facility placed being a site of ours the page was filed under by its own location or name, or one of its new-landfill entries. It gives the page, its publisher, type and tier, the counts, the searches, the sites it placed, the names it gave, and why it is listed. |

**Nothing is loaded from this repository.** It used to write a standardized table and a `.load.sql`
beside it, built before review. Ingestion now builds the AI search's table itself, from the values
SMEs validate (`waste_data_ingestion_pipeline` #261). Arbitrating a run again deletes the
`std_facility_tbl_ai_search_<run_id>.csv` and `.load.sql` an earlier arbitration left in its folder,
and says so. A copy already pushed to blob is kept there, as the record of that arbitration.

**One source per exact URL.** The query string is part of the URL, so documents that one script
serves by id stay separate sources: dropping it once merged 103 different Natural Resources Wales
documents into one. Only spellings that never change the page are cleaned: the scheme and host's case,
a leading `www.`, a trailing slash and the `#fragment`. The path and query keep their case. The
`source_id` is derived from that URL, so it is the same in every run and for every site citing
it, and every `evidence.csv` row's `source_id` is in `sources.csv`. `evidence_id` stays one per
citation.

### Source credibility

Tier is assigned **locally** from `source_type` and publisher, never taken from the agent — measured
against the pilot responses, the agent's self-assigned tier put news reports at Tier 2, 3 and 4
simultaneously. Its number is kept as `agent_proposed_tier` for drift monitoring only.

| Tier | Class | Can fill an empty baseline | Can override a baseline |
|---|---|---|---|
| 1 | Regulator, permit/registry, operator disclosure | yes | yes |
| 2 | Peer-reviewed, IFI/UN dataset, govt statistics | yes | yes |
| 3 | News, trade press, NGO report | yes | no |
| 4 | Aggregator, wiki, directory listing | yes | no |
| 5 | Source type could not be established | yes | no |

**Nothing is accepted automatically.** A found value for an empty field goes to the review queue
whatever its tier. One that disagrees with ours goes there from a Tier 1–3 source at least as
credible as ours, and to Leads from one below the floor or weaker than ours. A value equal to ours
is a confirmation: its evidence stays in `evidence.csv`, but it is not queued and not loaded, so the
AI search never becomes the credited source for a value nobody reviewed. Tier still decides between
the queue and Leads, and is shown beside each value. Until build step 3 of
[the source-matching plan](docs/plans/2026-10-20-match-sources-by-location.md), Tier 1–2 fills and
confirmations were accepted without review: 2,932 values in batch 1.

Baseline credibility is judged **per attribute**, from
`consolidation.value_resolution_ledger`, not from the facility's composite
`contributing_data_sources` — so an `osm_2022` site name is tiered as OSM even when the same
facility also has a GHGRP filing. The dataset is shown as `baseline_source`.

A source with no publication date is capped at Tier 3 whatever its publisher. An **empty**
baseline is filled at any tier — a sourced value beats no value. Once a baseline value exists, only
Tier 1-2 may override it.

Corroboration counts sources, not citations. A page cited twice for one attribute counts once in
`agreeing_source_count`, once when the most-corroborated value breaks a tie, and once in the
"N source(s) found" a weaker finding carries to Leads.

**Two map sites are dropped.** Citations of `mapcarta.com` and `maptons.com`, any subdomain
included (`ca.maptons.com`), are dropped before arbitration, and so are the agent's own leads that
point at them. Mapcarta's landfill pages copy OpenStreetMap, where most of our sites came from: all
319 batch-1 citations traced to the OSM way in the URL. Maptons is an unattributed scrape of
business listings, and 27 of its 36 batch-1 coordinate pairs were more than 2 km from the site
searched. They are recognised by website, never by the agent's `source_type`, which it applies
unreliably. Each drop is counted in `parse_warnings.csv`, per site and website, and the URLs are
not link-checked. A value whose only sources were these two is not promoted, like a value given
with no source, and neither appears in Leads. Wikimapia, an independent crowd-sourced map, stays as
a weak source.

Nothing is read from a dropped page. Until build step 5, its stated location still fed the 5 km
identity check; matching each source to the site it is about replaced that check.

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

The reviewer opens **one file**: `outputs/runs/<run_id>/<run_id>_review.xlsx`. Everything else in
the run directory is audit material you only need if a row raises a question.

### 1. Open the Review_Queue tab

This is the worklist: every value the search found for an empty field, and every disagreement
with what the database holds from a source credible enough to challenge it. **Nothing is accepted
without review**, whatever the source's tier. An empty queue means nothing needs you.

Rows arrive here for one of two reasons:

- **A value fills a field that was empty.** It may come from a regulator or from a directory; the
  tier is beside it.
- **A value disagrees with what the database already holds**, from a Tier 1–3 source at equal or
  better credibility. For status, a Tier 1–3 source at least 3 years newer than ours counts too.

Not shown: a source that only confirms what the database holds (it stays in `evidence.csv`, and
nothing from it is loaded), a search that found nothing, and a disagreement from a source below the
floor or weaker than what we hold, which is under Leads.

**One row per site, attribute and year.** Each source is filed under the site of ours it is about
(see [Matching each source to a site](#matching-each-source-to-a-site)), and everything filed under
a site, from any search, is decided together: one row per site and attribute, the key ingestion
loads by. A yearly attribute — status, waste received, waste in place, waste depth, cover types and
the gas figures, as consolidation keeps them by year — has a row for each year a source reports,
the year of its `value_date`, or the run's year where it has none. Its `baseline_value` is ours for
that year: the seed holds one row per site, at its reference year, so for any other year our latest
value stands in, and `evidence_summary` says so. Where our value for the reference year was carried
forward from an earlier one, we hold nothing for that year, so a value found for it fills an empty
field.

### 2. Read the row

The five columns that decide your judgement:

| Column | What it tells you |
|---|---|
| `resolution` | Why this row needs you (see the table below) |
| `baseline_value` vs `resolved_value` | What we had vs what the search found |
| `winning_source_tier` vs `baseline_tier` | Whose claim is more credible |
| `baseline_source` | Which dataset supplied the value we already had (blank if there was none) |
| `winning_source_url` | The actual source. **Open it.** |
| `evidence_summary` | The pipeline's one-line explanation of the decision. A shaded **CHECK** note says the winning source's quote, or the location it states, isn't on its page, as the link check read it: open the page and look |
| `match` | Why the value is filed under this site: where its source places the facility, and how far from the site searched |

Every site column (`site_id`, the names, `country_iso3`, `baseline_coordinates` and the `baseline_`
columns) describes the site the value is filed under. `searched_site_id` is the one column about the
search that found it. `facility_name_in_source` and `stated_location` are the facility as the
winning source writes it, and `location_basis`, on a `found_location` row, how its point was read.

Rows are colour-coded by `resolution`, and the tier scale is on the `Definitions` tab. The `match`
cell has a colour of its own where the value needs a second look at its site: orange where it is
filed under a site other than the one searched, blue where its source gives no location, and
yellow where several of our sites tied.

**Find the site.** `baseline_coordinates` is where the database puts the facility, as `lat, lon`. Paste it into Google Maps' search box to see the site, and compare it with what the source describes. A `found_location` row is where its source says the site is — coordinates, a grid reference, or an address placed on its building — in the same form: accept or reject it as one point. `evidence.csv` shows what the source wrote, in `stated_location`.

**Check the name first.** `site_name` is machine-translated; `original_site_name` beside it is the source's own spelling, in `source_language`. The search uses the original, so a source about *EL HONGO* is the right facility even though the row says *THE FUNGUS*. If the translation is wrong or misleading, say so in `translation_note`, which sits next to the names, rather than in `researcher_notes`. The same three name columns appear in every tab and in `evidence.csv`; they are blank where the source was never translated.

**Source links.** `winning_link_status` beside the URL says whether it was checked and found working (`OK`) or could not be verified (`Unverified` — usually a site refusing automated checks, so it may still open for you). A source whose link is dead never wins: it is routed to Leads instead. `winning_source_url` is clickable, and shows the URL exactly as the source gave it. The link behind it is made safe (spaces and accented characters encoded), so it opens even when the text has them. A URL that is not `http`/`https`, or longer than Excel can follow, is shown as plain text rather than linked.

### 3. Decide

| `resolution` | What to check |
|---|---|
| **Filled empty baseline** | The field was empty and now has a value. Open the URL: does the source actually say this, and is it about *this* facility? |
| **Overrides baseline** | A more credible source contradicts existing data. Confirm the new value is right before it replaces a database value. |
| **Conflict - needs review** | Two sources of equal or near-equal credibility disagree, and the pipeline will not pick a winner. You pick, or reject both. |

**The single highest-value check: is the source about this site?** Tier cannot detect a perfectly
credible document that describes the *wrong landfill*. In the pilot, one search's findings were
238 km from the facility. `match` starts with the rule that filed the value here:

| `match` starts | What it means |
|---|---|
| **Located** | The source places the facility within 2 km of this site and no other. If `searched_site_id` is another site, that search found it. |
| **Named among several** | Several of our sites are within 2 km, and the source's name picked this one. |
| **Tie** | Several of our sites are within 2 km and the name couldn't pick. `corrected_site_id` offers them. |
| **Name nearby** | The source places it just beyond 2 km, but its name strongly matches this site: its location is a coordinate correction. |
| **Moved by name** | The source gives no location, but its name strongly matches this site and not the one searched. |
| **No location** | The source gives no location, so the value stays with the site searched, and only you can tell whether it is about this facility. The note names any of our sites nearby that the name also fits, and says if the same page was cited for other searches; `corrected_site_id` offers all of them. |

A renamed site is fine. If the value is right but about another of our sites, put that site's id
in `corrected_site_id` (a dropdown where there were several candidates; any site id may be typed)
and validate it. If it is about another of our sites and you can't tell which, reject it as
`Wrong facility - another site in our database`; if it is about a landfill we don't hold, as
`New facility - not in our database`.

### 4. Record the decision

Edit only these columns; everything else is read-only:

| Column | Enter |
|---|---|
| `validation_status` | `Validated` or `Rejected` (dropdown) |
| `rejection_reason` | For a rejection: `Wrong facility - another site in our database`, `New facility - not in our database`, `Not in source`, `Wrong value`, `Outdated` or `Other` (dropdown). Plain `Wrong facility` stays for batch 1's workbook. Counted by cause to find where the AI errs |
| `corrected_value` | If you know the right value: enter it here. For categorical fields (site type, status, waste type, depth, yes/no fields, cover and gas-project types) it is a dropdown of the allowed values for that row's attribute; multi-value fields also accept a typed combination. A location is `lat, lon`. Never edit `resolved_value` — it keeps the AI's own answer, which is how its error rate is measured |
| `corrected_value_date` | Only if your corrected value is for a different date than `value_date`: that date, as `YYYY`, `YYYY-MM` or `YYYY-MM-DD`. Never a year that has its own row for the site and attribute (see below) |
| `corrected_site_id` | Only if the value belongs to another of our sites: that site's id. A dropdown of the candidates where there were several. First filter `site_id` to that site: if it already has a row for the same attribute and year, reject one of the two |
| `reviewer` | Your name |
| `reviewed_date` | The date |
| `researcher_notes` | Why, especially for a rejection |

`Validated` means the value is correct and may be merged. `Rejected` means the search's value must not be; a `corrected_value` you give is merged instead. Ingestion reads these decisions straight from this tab.

**One value per site, attribute and year.** Ingestion takes one value for each, and stops on two.
Each row of a yearly attribute (status, waste received, waste in place, depth, cover types, gas
figures) is that year's value: the year of `value_date`, or the run's year where `value_date` is
blank. So an older year's value is not `Outdated`: validate it if it was right for its year, and
put a current figure on the newest year's row instead. A 2024 status rejected as `Outdated` with a
current `Active` and no `corrected_value_date` lands in the run's year, and where the run's year
has a status row of its own, ingestion stops on the two. For the same reason, a
`corrected_value_date` never names a year that has its own row, and before re-filing a value with
`corrected_site_id`, check that site has no row of its own for the same attribute and year; if it
has, reject one of the two.

Once the review is done, `collect-new-facilities --run-id <run_id>` adds the values rejected as `New facility - not in our database` to the run's `new_facility_candidates.csv`, once each, for the ingestion team. It reads the workbook in the SharePoint folder, else the run folder's, or the one `--workbook` names.

### 5. Optional tabs

- **Leads** — findings that lost: below the credibility floor, weaker than the baseline, a dead
  link, or unconvertible units. Read-only. If you judge one good enough, promote it by hand.
- **Parse_Warnings** — values the pipeline dropped or could not map, and citations of the two
  dropped map sites, each attributed to its `site_id` and `site_name`. Worth scanning for
  systematic problems (a recurring unit or enum failure usually means an upstream fix, not 50
  individual reviews).
- **Definitions** — the allowed values, mirrored from the live database enums.

### Bing cost

Bing grounding bills per query, so the run log records the queries each site made in
`web_searches` (every retried attempt, resumed run and follow-up pass included, since each is
billed), and the search prints the run's total.

**Model tokens are recorded too, and the run is priced.** Each site's row also gives the model that
answered (`model`) and its tokens: `input_tokens`, of which `cached_input_tokens` were read from the
prompt cache, `output_tokens`, and of those `reasoning_tokens`. Like `web_searches`, the token counts
add up across every attempt and pass. A search prints its queries, tokens and estimated cost at list
prices:

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
| SME review queue | 16 | 16 | 9 |

Repeat runs of one site vary widely (one used 12, 30 and 39 queries), so treat the last two rows as
noise-level.

### If a site could not be searched

`sites_to_retry.csv` lists sites where the agent's own web-search tool failed. These are **not**
"no data found" — nothing was searched. They need a re-run, not review:

```bash
uv run waste-ai-search search --run-id <run_id> --force --site-ids <ids from that file>
uv run waste-ai-search arbitrate --run-id <run_id>
```

### Auditing a decision

Nothing is accepted automatically, so `resolved.csv` has a header and no rows. Every value awaiting
a decision is in the review queue. A source that confirms what we hold is in `evidence.csv` but not
in the queue, and attributes not found are not written anywhere. `evidence.csv` holds one row per
source-backed claim with the tier rule applied to each source, so you can see exactly why a value
won or lost.
`sources.csv` holds one row per exact URL; a row's `source_id` in `evidence.csv` finds its URL,
title and publisher there.

## Reverse Geocoding

Reverse geocoding is optional but strongly recommended for live pilot runs. The
pipeline supports cache-only geocoding through JSONL records with either a
`site_id` or a rounded coordinate key.

Example cache row:

```json
{"site_id":"11489","municipality":"Lagos","admin1":"Lagos","admin2":"","formatted_address":"Olushosun, Lagos, Nigeria","geocoder_provider":"manual","geocoder_confidence":"High"}
```

Pass the cache to the search phase so the prompt can use municipality and admin names, which
matters most for local-language searching. Outside Brazil this cache is the only source of them,
and since the prompt's point is rounded to about 1 km, the town is the most precise place it can
name:

```bash
uv run waste-ai-search search --run-id pilot10 --use-geocode-cache \
  --geocode-cache data/geocode_cache.jsonl
```

Without `--use-geocode-cache` those fields stay empty and every site records
`geocode_status = disabled`.

Live Azure Maps batch reverse geocoding should be added later as a separate
pre-processing command that writes this cache.

This is separate from the locate step's geocoder, which places the addresses sources state (see
[Locating each source](#locating-each-source)). The prompt passes the municipality and admin names
on, but no longer `formatted_address`: a street address next to our site would leak our point.

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
to a coordinate-only search.

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

