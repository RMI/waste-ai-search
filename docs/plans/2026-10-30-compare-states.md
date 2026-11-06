# AI search: compare the state a source puts the site in with ours

**Status:** built on branch `sme-value-checks`, not merged. Agreed with Hugh on 30 October 2026.
**Date:** 30 October 2026

## In short

In the 10-site Mexico test, site 6086 ("BASURERO MUNICIPAL", in San Sebastián Tecomaxtlahuaca,
Oaxaca) was matched to a Tier 1 state congress page about the dump in Tlapa de Comonfort,
Guerrero, about 60 km away. Nothing caught it: no source gave a location we could put on a map, so
the 5 km check never ran, and the prompt named no town, so the agent had only rough coordinates
to judge by.

Now:

1. **Each prompt names our site's municipality and state.** Brazil's seed already had them. Every
   other site's are looked up from our coordinates when it is searched, once, into
   `data/geocode_cache.jsonl`.
2. **Each source gives the town and state it puts the facility in** (`stated_municipality`,
   `stated_state`), and the prompt says a source in another state is very likely about a
   different facility.
3. **Site_Review shows both** (`our_place`, `source_place`) and **flags a source in another
   state**: "in a different state from our site: the source says Guerrero".

Steps 1 and 2 change what the search is given and returns, so they had to be built before batch 3.

## Why states, not a distance from the town

Placing a source's town on the map and measuring how far it is from our site was tried first, on
the same test's 11 addresses, and dropped:

- Mexican landfill addresses are rarely street addresses ("km 10.5 de la carretera a Colombia",
  "sobre la carretera Tlapa-Marquelia"), and Nominatim found none of the 11.
- Their town alone often found the wrong place ("Lago de Texcoco" is a street in Monterrey;
  "Castillo" a neighbourhood in Guanajuato, 279 km away), or a municipality whose centre is far
  from its landfill (Salinas Victoria's is 30 km from SIMEPRODE).
- It would not have caught Tlapa: the only match for "carretera Tlapa-Marquelia" was a point on
  the road 150 km away.

States, on the other hand, Nominatim names reliably from any spelling: "N.L.", "Nuevo Leon",
"Edo. Méx.", "State of Mexico", "CDMX", "Distrito Federal" and "Mexico City" all came back as the
right state. Municipalities are shown but not compared: a landfill often serves one municipality
and sits in the next (SIMEPRODE serves Monterrey and sits in Salinas Victoria).

## How it works

| Step | What happens | Kept in |
|---|---|---|
| Search | A site with no municipality or state in the seed is looked up from our point (Nominatim reverse, one a second). The prompt's site context gives `municipality` and `admin1`. | `data/geocode_cache.jsonl` (all runs); the run's `site_places.json` |
| Locate | Our state and every source's `stated_state` are named as Nominatim names them. | the run's `state_names.json` |
| Arbitrate | Offline: a pair whose source's state isn't ours is flagged, and sorted first under our site. | `found_values.csv` (`source_place`), Site_Review |

The cache holds a municipality and a state, never a street, so it says no more of our point than
the prompt's rounded coordinates. Azure Maps can't look points up here yet; with it, the prompt
names no town and nothing is compared.

## Accepted risks

- A site near a state border may be described from the next state. The flag only asks the SME to
  look; the README says to check the map before rejecting.
- A source that names no state, or one Nominatim doesn't know, isn't compared.
- Batch 3's first search spends about half an hour on lookups (1,553 sites at one a second).

## The Mexico test (`outputs/runs/test10-mex-place`, gpt-5.4, 30 October)

The same 10 sites as `test10-mex`, searched again with their municipality and state in the prompt.
About $2.40 in all (Puebla and Mérida hit connection errors and were searched again). All 10 sites'
places were looked up from our points, and all 8 states the run met were named.

| | First test | With places |
|---|---|---|
| Source rows | 32 | 31 |
| Value rows | 9 (5 without gas use) | 12 |
| Sites with a source | 7 of 10 | 9 of 10 |
| Sources cited with a town or state | - | 31 of 32 |
| Sources flagged in another state | - | 0 |

What it showed:

- **Site 6086 now found its own dump.** The first test matched it to a page about Tlapa,
  Guerrero. This time the agent searched "BASURERO MUNICIPAL" with San Sebastián Tecomaxtlahuaca,
  Oaxaca, and found an El Universal Oaxaca report of a fire at that town's dump. Its generic name
  means batch 3 won't search it, but the same holds for any site whose name alone fits many
  places.
- **Site 2456 ("Kitchen Corner Trash Can", Rincón de la Cocina) found two sources,** in
  Tepecoacuilco, Guerrero, where the first test found none.
- **No source was in another state,** so the flag didn't fire here; `tests/test_places.py`
  covers it. With the state in the prompt, the agent seems to leave out pages about other states
  before they reach us, which is the better outcome.
- **The agent gave a town or state for 31 of the 32 sources it cited,** and wrote states in full
  as asked.
- **One distance flag looks like a false alarm:** a Milenio page about Los Laureles gives
  "carretera Guadalajara-El Salto", which the geocoder placed on the highway 10.7 km from our
  point. That is the existing 5 km check working on a street-level point, as before.
- **Tiers vary from run to run** for the same publisher (Mota-Engil Tier 1 then Tier 3, CONAGUA
  Tier 1 then 3). That is the agent, not this change, but it decides which values SMEs check.
