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
