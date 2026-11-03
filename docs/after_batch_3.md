# After batch 3: what to do next

**Written:** 2 November 2026, the day batch 3 (Mexico) was searched. Its SMEs expect to take a week
or two to review it.

This note lists what can happen while they review, what to settle before the next batch, and ideas
worth testing. It also records what batch 3 showed, so the numbers are at hand when deciding. When an
item is picked up, give it a plan in [`docs/plans/`](plans/).

## Batch 3 at a glance

- **Searched:** 1,553 Mexican sites, with GPT-5.6 Terra, and again with GPT-5.6 Sol on the 64 taking
  100,000 t a year or more. 667 more were skipped for generic names ("BASURERO MUNICIPAL").
- **Cost and time:** about $400 at list price ($0.26 a site), and 5 hours at 3 sites at a time.
- **For SMEs:** 1,066 pages at 561 sites, with 309 ↳ rows and 174 one-value pages to check: roughly
  50-60 hours at 3 minutes a page.
- **Not searched:** 15 searches, 14 by Terra and 1 by Sol (site 5345, which Terra did answer).
  Azure's content filter stopped the agent's answer, probably over violent local news it had read.
  They are in `sites_to_retry.csv`, and running them again can succeed (see the tests below).

## Why only 561 of the 1,539 sites searched have pages

They were not failed searches. The 978 sites without pages were searched normally and found nothing
usable.

- **The searches ran.** Every site's search made at least 6 queries (8 for the median site), and the
  agent opened 1,268 pages across the batch. Only 4 of its 4,719 searches came back without a query.
- **What the 978 found:**
  - 690 found nothing at all;
  - 273 found only leads, values the agent itself marked unverified: 158 where it doubted the page
    was about our site, and 115 where a value rule or an unreadable page set them aside;
  - 15 cited a source whose values could not be used.
- **Size explains most of it.** 70% of the batch are small rural dumps, and most leave no trace on
  the web:

  | Site size (t a year) | Sites with pages |
  |---|---|
  | Under 5,000 (1,085 sites) | 22% |
  | 5,000 to 20,000 | 57% |
  | 20,000 to 100,000 | 90% |
  | 100,000 or more | 98% |

- **Searching only in Spanish was not the cause.** The 1 November retest searched the same 25 sites
  with the old prompt, which searched in English and Spanish, and with the new one, Spanish only.
  The new one did slightly better: pages at 18 sites against 17, and 34 pages against 31, of which
  blind judges called 28 and 26 about our site. In batch 3 itself, 20 of the 1,164 sources cited
  were in English, most of them landfill-gas project documents (the UN's Clean Development
  Mechanism, the Global Methane Initiative), which the prompt still searches in English, and one
  from the North American Development Bank.
- **The agent isn't missing obvious pages.** For five sites where it found nothing, a search by hand
  with another search engine found nothing it had missed. Its queries were sensible: the exact
  name, variants, the town, government websites and PDFs.
- **Our own data can be wrong.** Site 9859 is named OCOZOCOAUTLA, a town in Chiapas, and takes
  164,250 t a year, but our coordinates put it in Moloacán, Veracruz, hundreds of kilometres away.
  The agent found pages about Ocozocoautla and rightly refused them.

The tests below found pages for some of these sites after all: a second search finds pages that
the first missed, and about 1 in 5 of the pages the agent doubted are about our site.

## Tests on the sites without pages (3 November)

Run the day after batch 3 to see what was being missed. They cost about $17 of searching.

- **A second search, with and without telling Bing the site is in Mexico.** 40 sites that found
  nothing in batch 3 (20 under 5,000 t a year, 20 from 5,000 to 20,000) were searched twice more:
  once by the same Terra agent, and once by a copy whose web search tool is told the user is in
  Mexico (`waste-ai-search-gpt-5-6-terra-mx`, still in the Foundry project).
  - Between them they found pages about 6 of the 40 sites: 5 of the 20 mid-sized ones (Jerécuaro,
    Rancho Guadalupe, Cerro Colorado, La Caliza, Barrancas) and 1 of the 20 small ones (El Raicero),
    and one page about another town's dump. Batch 3 had found none.
  - Telling Bing the country made no clear difference: on the 29 sites both searches answered, the
    copy found pages for 4 and the same agent for 2. That's too few to tell.
  - What matters is that searches vary: a second search of an empty mid-sized site found pages a
    quarter of the time.
- **Azure's content filter is chance, not the site.** It stopped 13 of the 80 searches, but only 2
  sites both times. In batch 3, 98 answers came only on a retry, 34 of them at sites now on
  Site_Review. A change on 2 November had stopped retrying a filtered search; it was undone on 3
  November, so a filtered search is tried again.
- **Another search engine found no more.** The same 40 sites, searched by hand with a different
  engine (3 queries each), gave pages about 2 sites: Rancho Guadalupe, which Bing's second search
  also found, and Cuautolanico, which it didn't. Bing's second searches found 5 that it missed.
  Bing's results are not the problem.
- **About 1 in 5 doubted pages are ours.** 30 pages the agent set aside because it doubted they were
  about our site (one each from 30 of the 158 sites) were read without its reasons: 4 are ours,
  13 aren't, and 13 couldn't be settled, 10 of them because the page wouldn't open. The 4 it got
  wrong carry useful facts:
  - 10595 Cuauhtémoc (Zacatecas): a 2020 study's map puts its active dump within 40 m of our point.
    It opened in 2008, covers 2.5 ha and takes 13 t a day.
  - 20 (ADDO, Tijuana): closed in full by the state in 2024.
  - 288 El Cebollín (Guadalupe y Calvo): a business-register entry pinned 10 m from our point.
  - 7121 La Estación (Peñamiller): closed temporarily by the state in October 2025.

  Two of the four give a location next to ours, which the agent didn't weigh: a doubted page whose
  stated location is close to our site could go to SMEs instead of being set aside.

## While the SMEs review batch 3

1. **Get the ingestion PRs reviewed.** Nothing from any batch reaches the database until
   [#261](https://github.com/RMI/waste_data_ingestion_pipeline/pull/261) (raw tables and transforms,
   batch 1 and `accepted_values.csv`),
   [#264](https://github.com/RMI/waste_data_ingestion_pipeline/pull/264) (entity linkage and
   consolidation) and [#266](https://github.com/RMI/waste_data_ingestion_pipeline/pull/266) (status
   years) are reviewed and merged. All three are still drafts.
2. **Finish batch 1's hand-off.**
   - Re-key its files to `wastemap_id`: `scripts/relabel_to_wastemap_id.py <batch 1's run folder>
     --from-db --workbook <the SMEs' finished workbook>`, on the VPN, from
     [#26](https://github.com/RMI/waste-ai-search/pull/26). This Mac has no database login.
   - Snapshot the two re-keyed files in the ingestion repository and pin the snapshot's date
     (`BATCH1_SNAPSHOT_DATE`).
   - Ask batch 1's SMEs about the names it found, which are left out for now. 634 of the 635 in its
     review queue differ from our name. They validated 261, rejected 229 and left 142 undecided, for
     example "Resource Recovery Centre" → "Lobster Creek Resource Recovery Centre". The 89 in
     `resolved.csv` are confirmations, identical to ours. The SMEs also typed better coordinates for
     two sites into `corrected_value`.
3. **Check the SMEs' workbook partway through.** Batch 1's finished copy had free text in dropdown
   columns, a tab in one header, coordinates typed into `corrected_value`, and 480 rows never
   decided. Running `accept-reviews` on a copy of the SMEs' file halfway through catches mistakes
   like these while the SMEs can still fix them. It only reads the workbook.
4. **When the SMEs finish batch 3:** run `accept-reviews` on their workbook, check what it says
   (undecided rows, conflicting approvals), then put `accepted_values.csv` in the ingestion
   repository's snapshot and pin it in `AI_SEARCH_SNAPSHOTS`.

## Before the next batch

Batch 4 is Germany, Russia and Turkey. Batch 5 is China, France, Italy, Poland, Spain and Indonesia.
The last batch takes every other country.

### Languages

The tuning before batch 3 was built for Mexico, so each new language needs the same checks:

- **Generic names.** The list of names that give a search nothing to look for knows English and
  Spanish only. Batch 4 needs German, Russian and Turkish equivalents of "BASURERO MUNICIPAL"
  (Deponie, свалка, çöplük and so on), found from the names in our data.
- **Units and magnitudes.** Values are read in Spanish, Portuguese, French, Italian and German, but
  not in Russian ("тыс. т", "млн") or Turkish ("bin ton", "milyon").
- **Search in English as well as the local language.** In many countries useful pages are in
  English as well as the local language. Hugh expects this of many countries whose language is less
  widely used online, though which ones isn't known yet. So:
  - **Keep searching in both** where it isn't measured. That is what the prompt does today for every
    language but Spanish.
  - **Measure it per country in each practice run** before writing a language rule. Each source the
    agent cites records its language, so counting them is easy. Mexico's share was under 2%.
  - **English pages exist even where the local language is large.** Think EU documents and
    registries, development-bank project documents (World Bank, EBRD, ADB and IFC fund landfills in
    Turkey, Eastern Europe, Central Asia and Indonesia), academic papers, company reports and
    international news.
  - **The Spanish-only rule needs checking before batch 5.** It applies to every Spanish-language
    site, but it was measured only in Mexico. Spain (249 sites, batch 5) and Latin America (191,
    last batch) should be measured before keeping it there, especially Spain, where EU documents are
    often in English.

### Running the batch

- **Speed.** Terra's deployment allows 1M tokens a minute, but refused requests at a fifth of that,
  probably because Azure counts the largest possible reply. Batch 3 ran 3 sites at a time (the
  README's commands say so). Capping the agent's reply length, or asking Azure for more quota, might
  allow 6 or more at once; a 50-site test would show it.
- **Content filter.** It stops searches by chance, depending on the pages an attempt reads, so
  retries rescue most: in batch 3, 98 answers needed a retry and 15 searches still failed after
  it. Some countries may lose more. Azure lets a deployment use a less strict filter, which would
  cut both the failures and the cost of retrying; that may need IT's agreement.
- **Search the empty mid-sized sites again.** A second search of an empty site taking 5,000 t a
  year or more found pages a quarter of the time (above). For batch 3 that is 48 sites, about $12,
  and could run as a small run of its own with its own workbook, so it doesn't disturb the SMEs'
  copy. Do the same in each later batch, with the searches the content filter stopped.
- **Telling Bing the country** (`user_location` on the agent's web search tool) made no clear
  difference in the test above. It costs nothing to leave on, but it isn't worth a separate agent
  per country. The tool can also be given a larger search context, which wasn't tested.

## Ideas worth testing

- **A cheaper first look at small sites.** Bing's searches are about two-thirds of the cost, and
  sites under 5,000 t a year find pages 22% of the time. Three searches first, and more only when
  something turns up, could cut the cost of the biggest batches. Test it before batch 5.
- **Send some doubted pages to SMEs.** About 1 in 5 of the pages the agent doubted are about our
  site (above). Sending all of them would cost SMEs far more time than it saves, but a doubted
  page whose stated location is near our site, or that names our site's operator, is worth a
  look. Locations are already read from cited pages; they would need reading from leads too.
- **A place for location fixes.** Batch 1's SMEs typed better coordinates into `corrected_value`,
  having nowhere else. Batch 3's workbook has no place to say "our location is wrong" either.
- **Check our names against our locations** before searching, to catch mismatches like site 9859's.
- **Run batches on Azure.** A container job and blob storage would free this Mac, keep the results
  off it, and publish the workbook to SharePoint without a manual upload.
