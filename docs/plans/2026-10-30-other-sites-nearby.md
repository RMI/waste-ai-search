# AI search: each prompt names our other sites nearby, and a town's page is not our site's

**Status:** built on branch `sme-value-checks`, not merged. Agreed with Hugh on 30 October 2026.
**Date:** 30 October 2026

## In short

Two searches of the same 10 Mexico sites (test10-mex-sample2 and its rerun, on gpt-5.4) gave 27
and 24 pages for review, only 9 of them in both and 42 in either. Some of the difference was
judgment, not missed pages. The first search gave Las Varas (Madera, Chihuahua) two NADBank pages
and a Chihuahua state government page about Madera's new landfill. None named Las Varas, and its
closing year was inferred. The second search declined pages like them.

So the agent now makes that call the same, cautious way every time:

- **Every prompt** says a town can have waste sites we don't hold, such as a new landfill that
  replaced an old dump. A page about the town's waste, another of its sites, or one of our other
  sites nearby is not about our site unless it also describes ours: its name, place or history.
  From such a page the agent uses only what it says about ours, and a generic phrase such as
  "basurero municipal" names no particular site.
- **Where we hold other sites nearby**, the prompt names them, nearest first. Las Varas gets
  "BASURERO NICOLAS BRAVO (Madera, 18 km away)", 'unnamed site, recorded as "BASURERO MUNICIPAL"
  (Madera, 31 km away)' and one more. The ten nearest are named and the rest counted.

## What "nearby" is

- Our other sites in the same country that are within 15 km, or within 60 km and in the same
  municipality and state. The list comes from the whole seed, so a site the run doesn't search,
  such as one skipped for its generic name, is named too, but as an unnamed site.
- Places come from the seed or `data/geocode_cache.jsonl`, as for our own site. Only our sites
  within 60 km of a site searched are looked up (Nominatim, once a second, cached): 236 for the
  Mexico sample, against 841 within 150 km. So 60 km is a cut-off for matching municipalities too:
  a site farther off in one of the largest municipios is named only if it is within 15 km.
- The geocoder names a site in a town by the town ("Texcoco de Mora") and one outside it by its
  municipio ("Texcoco"), so matching names alone would miss some of our sites in the same
  municipio. Within 15 km, a site is named whatever its place is called.
- Our records under 1 km away are named apart, in `our_records_within_1_km`, as possibly this site
  recorded twice: Mexico's INEGI rows hold some dumps twice at the same point (8690 and 8689 are
  3 m apart), and the prompt gives our point only to about 1 km. A page about one of them may be
  cited for both, which is how `possible_duplicates.csv` finds a site recorded twice. The rest go in
  `our_other_sites_nearby`.
- Without the geocode cache (`--no-geocode-cache`), only the distance counts.

A five-angle review with three skeptics per finding (30 October) found the records at one point,
generic labels read as names, and a second sentence that dropped the rule's exception. All three
are fixed as above.

## Not done

- **A second search pass**, merging two searches of a site into one review: it would find more (the
  rerun found 15 pages the first search missed), but costs a search per site. Not for now.
- **A search on the town without the site's name**, such as "relleno sanitario Madera Chihuahua".
  That is how the first search found the Madera pages, and it would bring pages about other sites
  into every search. Rejected.

## Prompt size

The rule adds about 390 characters to every prompt: Thunder Bay's, which names no other site, is
18,794, so the size test's limit goes from 18,500 to 19,000. Where a site has others nearby, the
first named adds about 175 more and each further one about 65, so ten add about 830.

This changes what the search asks, so it must be in place before batch 3.
