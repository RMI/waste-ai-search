# Future improvements

Ideas we agreed are worth doing, but not yet. Each says what is missing today and what doing it
would look like. When one is picked up, move it into a plan in `docs/plans/`.

## SMEs confirm sources that have no site before they are handed over

**Added:** 20 October 2026, from the
[source-matching plan](plans/2026-10-20-match-sources-by-location.md).

Under that plan, two kinds of source are kept but filed under none of our sites, and both skip
SME review:

- **New-landfill candidates** (`new_facility_candidates.csv`). These are sources with a usable
  location, more than 2 km from every site of ours, and with no name match to the site searched
  within 5 km. The file goes straight to the ingestion team.
- **Matches outside the run.** These are sources that match one of our sites in a country the run
  doesn't cover: another batch's country, or the excluded US and Brazil. They go on a list, and
  nothing yet says what happens to it.

**Why it matters.** A "new landfill" can be one of our own sites whose coordinates are badly wrong.
The West Edmonton Landfill and Thorhild records, for example, sit 100 m apart in our data, though
Thorhild is a town well north of Edmonton. Only a person reading the source can tell. The
new-landfill file also lacks the values each source gives (status, waste received, and so on), so
anyone adding the site would have to read every source again.

**What to do.** Add a tab to the review workbook (e.g. `New_Facilities`), with one row per
candidate and per outside-the-run match. It would show:

- the source URL, name and stated location;
- the derived coordinates;
- our nearest site and its distance;
- the search that found it;
- the values the source gives.

The SME confirms each one as new, as one of our sites (by ID), or as not a landfill. Only confirmed
candidates go to the ingestion team. A match outside the run is then handed to the run that covers
that country, or loaded once ingestion decides how one facility can take values from two runs.

## Brazil's coordinates-only search files its finds as new landfills

**Added:** 21 October 2026. Parked by Hugh until Brazil is searched again; it is excluded by default.

Brazil's SINIR sites carry the centre of their municipality, not the dump, and most are named
generically ("Lixão", "Aterro Controlado"). A source that does locate the dump is therefore usually
more than 2 km from our point, and its name rarely matches strongly enough for the 25 km name
rescue. So under the matching rules most of Brazil's found coordinates become new-landfill
candidates rather than corrections to our coordinates.

**What to do.** For a site whose location is flagged inexact, file a located source within 25 km
under the site searched, unless its name strongly fits another site of ours. The name would then
be a veto, not a requirement. Brazil's generic names are also skipped by the unsearchable-names
rule unless `--include-unsearchable-names` is passed, so both need deciding together.
