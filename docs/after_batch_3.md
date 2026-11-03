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
  They are in `sites_to_retry.csv`.

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

Two things below could recover some of these sites: telling Bing the searches are about Mexico, and
checking a sample of the leads the agent doubted.

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
- **Content filter.** About 1% of Mexican sites were unsearchable. Some countries may lose more.
  Azure lets a deployment use a less strict filter, which may need IT's agreement.
- **Tell Bing where the site is.** The agent's web search tool uses its defaults
  (`WebSearchTool()` in `waste-ai-search-infra/scripts/create_foundry_agent.py`). It can be given an
  approximate location (a country) and a larger search context, which might surface more local
  pages. Test it on about 40 small Mexican sites that found nothing (about $10-20) before batch 4.

## Ideas worth testing

- **A cheaper first look at small sites.** Bing's searches are about two-thirds of the cost, and
  sites under 5,000 t a year find pages 22% of the time. Three searches first, and more only when
  something turns up, could cut the cost of the biggest batches. Test it before batch 5.
- **Check a sample of the leads the agent doubted.** Of the 273 sites with only leads, 158 have one
  where the agent wasn't sure the page was about our site. If a sample shows many are, those pages could go to SMEs as
  low-confidence pages. It costs no searching.
- **A place for location fixes.** Batch 1's SMEs typed better coordinates into `corrected_value`,
  having nowhere else. Batch 3's workbook has no place to say "our location is wrong" either.
- **Check our names against our locations** before searching, to catch mismatches like site 9859's.
- **Run batches on Azure.** A container job and blob storage would free this Mac, keep the results
  off it, and publish the workbook to SharePoint without a manual upload.
