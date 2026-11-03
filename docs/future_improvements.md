# Future improvements

Ideas we agreed are worth doing, but not yet. Each says what is missing today and what doing it
would look like. When one is picked up, move it into a plan in `docs/plans/`.

What batch 3 showed, and what to do before the next batches, is in
[after_batch_3.md](after_batch_3.md).

## Brazil's coordinates could be corrected by a source that locates the dump

**Added:** 21 October 2026. Parked by Hugh until Brazil is searched again; it is excluded by default.
**Updated:** 21 October 2026, for the
[approve-sources plan](plans/2026-10-21-approve-sources-site-by-site.md).

Brazil's SINIR sites carry the centre of their municipality, not the dump, and most are named
generically ("Lixão", "Aterro Controlado"). Their coordinates were the one thing worth searching
for. But under the approve-sources plan a source's coordinates only check that it is about our
site, and never replace ours, so a coordinates-only search would load nothing. Brazil's sites
therefore have nothing to ask, and are not searched even when Brazil is asked for.

**What to do.** If Brazil's locations are to be fixed by search, it needs a path of its own for a
coordinate correction: ask for the location of a site flagged inexact, and send a source that
places it to an SME as a proposed correction, never approved automatically. Matching against
OSM and Global Plastic Watch landfills within the municipality may be the likelier fix. Brazil's
generic names are also skipped by the unsearchable-names rule unless
`--include-unsearchable-names` is passed, so both need deciding together.

## A source could correct a site's name or location, with an SME's approval

**Added:** 3 November 2026, at Hugh's ask, after batch 3.

Today a source's name and location are only used to check that it is about our site. They never
replace ours: the search doesn't send them as values, and ingestion leaves batch 1's out. But our
names and locations are often the weakest thing we hold:

- **Names.** Many sites carry a generic or wrong name ("OpenStreetMap Landfill", "Resource Recovery
  Centre", an operator's name). Of the 635 names batch 1's search proposed, 634 differed from ours,
  and SMEs validated 261 of them, such as "Resource Recovery Centre" → "Lobster Creek Resource
  Recovery Centre". Whether those are new names or confirmations is now with batch 1's SMEs; until
  they answer, ingestion leaves them out.
- **Locations.** Some of our points are far from the site. Site 9859 is named Ocozocoautla, a town
  in Chiapas, but our coordinates are hundreds of kilometres away in Veracruz. Batch 1's SMEs typed
  better coordinates for two sites into `corrected_value`, having nowhere else to put them. Some
  sources pin a site precisely: in the 3 November tests, two pages the agent doubted were pinned
  within 40 m of our point (see [after_batch_3.md](after_batch_3.md)).

**What to do.** Let a source propose a new name or location, and load it only once an SME approves
it, never automatically:

- The search would send the name and location it reads as proposals, as batch 1's did.
- Site_Review would show a proposed change on its own row, like a key value's ↳ row: our name or
  point beside the source's, with a map link for a location.
- An SME would mark it Correct or Wrong, or type the right one, and `accept-reviews` would hand
  the approved ones to ingestion.
- Ingestion would need a rule for whether an approved name or point beats a regulator's, and the
  ingestion repository's entity linkage keeps the search's locations out of its registry, so that
  would need agreeing with its owners.
- A location from a street address can be a kilometre or more off, so only a point a source
  states, or one placed on a building, should be offered.

This would also give [Brazil's coordinates](#brazils-coordinates-could-be-corrected-by-a-source-that-locates-the-dump)
the path they need.
