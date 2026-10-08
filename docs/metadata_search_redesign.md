# Metadata Search Redesign — Proposed Plan

Status: **proposal, not implemented.** Decisions Q1–Q6 settled; open items in the last section.

## 1. What changes

| Area | Today | Proposed |
|---|---|---|
| Input | `inputs/pilot_sites.csv` (150 pre-filtered rows, old column names) | `inputs/consolidated_sites.csv` (15,537 folded facilities from `consolidation.consolidated_facility`) |
| Output grain | 1 row per candidate, source flattened onto it; conflicts exiled to a second sheet | 3 levels: **evidence** → **resolved** → **sources** |
| Source credibility | 4 bare labels, self-assigned, never used | Written rubric, rule-capped, gates promotion |
| Categorical values | Lowercase, 7-value vocabularies, unvalidated | Live DB enums, Title Case, validated (see §10) |
| Baseline authority | Baseline always wins ("do not overwrite") | Baseline tiered by its own datasets; relative credibility arbitrates |
| Agreement | Unrepresentable | First-class: `Confirmed baseline`, plus `agreeing_source_count` in the review queue |

Verification scope stays the 8 substantive attributes (Q1). The other ~32 seed columns are **not** sent to the agent; `contributing_data_sources` is read by the pipeline only, for baseline tiering.

## 2. The three-level schema (Q5)

### Level 1 — `Evidence` (one row per source-backed claim)

`evidence_id`, `run_id`, `dataset_version`, `site_id`, `internal_facility_id`, `attribute_name`,
`claimed_value`, `claimed_unit`, `normalized_value`, `normalized_unit`, `unit_conversion_note`,
`value_basis`, `value_date`, `agent_confidence`, `source_id`, `source_tier`, `agent_proposed_tier`,
`tier_rule_applied`, `evidence_summary`, `quoted_evidence_short`, `search_terms_used`,
`promotion_eligible`, `exclusion_reason`, `record_created_date`

One claim, one source, one row. A claim backed by three sources becomes three rows sharing an
`attribute_name` — which is what makes agreement countable.

### Level 2 — `Resolved` (one row per site × attribute)

`site_id`, `internal_facility_id`, `site_name`, `country_iso3`, `attribute_name`,
`baseline_value`, `baseline_source_datasets`, `baseline_tier`, `resolved_value`, `resolved_unit`,
`resolution`, `resolution_rule`, `winning_evidence_id`, `winning_source_tier`,
`winning_source_url`, `best_tier_available`, `value_date`, `confidence_score`,
`validation_status`, `reviewer`, `reviewed_date`, `researcher_notes`

Five columns proposed during design were later dropped as unhelpful in review
(`baseline_carried_from_earlier_year`, `baseline_staleness_note`, `agreeing_source_count`,
`dissenting_source_count`, `credibility_margin`). The staleness penalty and the margin are
still computed; they just no longer report themselves on every row.

`resolution` ∈ `Confirmed baseline` · `Filled empty baseline` · `Overrides baseline` ·
`Conflict - needs review` · `Conflict - lower credibility` · `Calculated` · `Not found`

This absorbs today's `Conflicting_Values` sheet — a conflict becomes a `resolution` state, not a
separate file. One place to answer "what do we now believe about this field, and why."

### Level 3 — `Sources` (deduped registry, keyed by normalized URL)

`source_id`, `url_normalized`, `url`, `source_title`, `publisher`, `publisher_class`,
`source_tier`, `tier_rule_applied`, `publication_date`, `has_publication_date`, `language`,
`source_type`, `paywall_flag`, `subscription_followup_needed`, `first_seen_run_id`,
`times_cited`, `cited_site_count`

Today the same regulator PDF cited by 40 sites produces 40 unrelated `Source_Log` rows. Dedup by
normalized URL makes a source a reusable entity whose tier is assigned once.

Retained as-is: `ReadMe`, `Run_Config`, `WasteSite_Base`, `Supplementary_Leads`,
`Foundry_Run_Log`, `Parse_Warnings`, `Definitions`, `WasteSite_Aliases`.

## 3. Credibility model (Q2)

Tier is assigned locally from `source_type` + `publisher` (see §3.1), not taken from the agent.

| Tier | Publisher class |
|---|---|
| Tier 1 | Regulator, environmental agency, official permit or registry, operator's own disclosure |
| Tier 2 | Peer-reviewed literature, IFI / UN / development-bank dataset, government statistical office |
| Tier 3 | Established news outlet, industry trade press, established NGO field report |
| Tier 4 | Aggregator, wiki, blog, undated or unattributed page |
| **Tier 5** | Source type could not be established (`source_type: Other`, or absent) |

Rules applied locally, after the agent answers, and recorded in `tier_rule_applied`:

- **Undated cap** — no `publication_date` caps a source at Tier 3 regardless of publisher.
- **No silent defaults.** Delete `psource.get("source_tier", "Tier 3")`; absent tier → Tier 5.
- **No coercion to Tier 4.** `validate_category` mapped unrecognized values to `allowed[-1]`;
  source_tier gets an explicit Tier 5 path instead, so "unparseable" never reads as "assessed".
- **Tier is a local decision.** The agent proposes `publisher_class`; the rubric assigns the tier.

## 3.1 Why the agent's tier cannot be trusted (measured)

Re-scored the 72 source entries in `outputs/runs/*/raw_foundry_responses/`. The agent fills
`source_tier` on every source — nothing is missing — but assigns the **same source class to
different tiers**:

| `source_type` | T1 | T2 | T3 | T4 |
|---|---|---|---|---|
| News report | 0 | **21** | **4** | **1** |
| Journal article | **1** | **3** | 0 | 0 |
| Operator website | **1** | **2** | 0 | 0 |
| Operator website / company profile | 0 | 0 | **3** | 0 |
| Other | 0 | **5** | **4** | **1** |
| Regulator | 12 | 0 | 0 | 0 |
| Regulator / Court decision | 4 | 0 | 0 | 0 |
| Municipal document | 3 | 0 | 0 | 0 |
| EIA | 2 | 0 | 0 | 0 |
| Map / coordinate directory | 0 | 0 | 0 | 3 |
| Newswire | **1** | 0 | 0 | 0 |
| Academic journal | 0 | **1** | 0 | 0 |

Regulatory sources are self-consistent. Everything else is not:

- **News spans three tiers.** 21 news reports sit at Tier 2 — above the Tier 3 the rubric assigns.
  Under §4 gating that is the difference between overriding a baseline and not.
- **Peer-reviewed journals span Tier 1–2** across four publisher spellings.
- **Operator sites span Tier 1–3**, and "Operator website / company profile" is a third-party
  directory listing, not operator disclosure — the label overstates its own authority.
- **`Other` is a 10-entry dumping ground** spread across Tier 2–4 with no discernible rule.
- **18 of 72 sources (25%) carry no publication date**, so the undated cap is load-bearing, not
  a corner case.

**Consequence for the design:** `source_type` and `publisher` are populated reliably; `source_tier`
is not. So the rubric derives tier **locally** from `source_type` + `publisher`, and the agent's
number is retained only as `agent_proposed_tier` for audit and drift monitoring. This also requires
`source_type` to become a **closed vocabulary** in `DEFINITION_VALUES` — today it is free text, which
is how composites like `Regulator / Court decision` and `ScienceDirect / academic journal` appear.

Separately, this scan found existing schema drift: responses contain `found_latitude` and
`found_longitude`, neither of which is in `TARGET_ATTRIBUTES` (it defines `found_coordinates`), so
`validate_foundry_payload` flags them as unsupported and the pipeline drops them.

## 4. Promotion gating (Q3)

| Candidate tier | Empty baseline | Non-empty baseline |
|---|---|---|
| Tier 1–2 | Promote, auto-validated | May override, subject to §5 |
| Tier 3 | Promote, needs review | Cannot override → `Conflict - needs review` |
| Tier 4 | Promote, needs review | → leads |
| Tier 5 | Promote, needs review | → leads |

**This table reflects the final rules, including the later change (F1) that lets any tier fill an
*empty* baseline.** The original proposal routed Tier 4 and below to leads in both columns. Tier
still governs auto-validation, so a Tier 3–5 fill always reaches a human. Once a baseline value
exists the floor applies: only Tier 1–2 may override it, and weaker contradictions go to leads
rather than consuming review time.

## 5. Baseline tiering and arbitration (Q6)

Seed `contributing_data_sources` is a `+`-joined token list. All 9 distinct tokens across the
15,537 seeded facilities, with counts (**Q7 settled except the two marked**):

| Token | Facilities | Tier | Rationale |
|---|---|---|---|
| `osm_2022` | 7,356 | 4 | Crowdsourced, unattributed |
| `lmop_2024` | 2,625 | 1 | US EPA Landfill Methane Outreach Program |
| `mexico_inegi_2016` | 2,202 | 1 | National statistics institute (raised from T2 per Q7) |
| `eprtr_2022` | 1,731 | 1 | EU industrial emissions registry |
| `gpw_2021` | 1,720 | 4 | Gridded population model — not a facility observation |
| `usa_ghgrp_2026` | 1,328 | 1 | US EPA mandatory GHG reporting |
| `waste_atlas_landfills_2013` | 560 | 3 | Compiled secondary dataset, aging |
| `canada_ghgrp_2021` | 132 | **1 — proposed** | Canadian federal GHG reporting, analogous to `usa_ghgrp` |
| `waste_atlas_dumpsites_2013` | 91 | **3 — proposed** | Same family as `waste_atlas_landfills_2013` |

Resulting `baseline_tier` distribution: **T1 6,566 · T3 224 · T4 8,584 · unassigned 163**
(the 163 carry only the two unmapped tokens). Every facility has at least one token, so there is
no empty-provenance case to handle.

The distribution is bimodal by design consequence: **55% of facilities sit at T4** (`osm_2022` /
`gpw_2021`) and are freely correctable by any credible source, while **42% sit at T1** and can only
be corrected by equal-tier evidence routed through human review.


`baseline_tier` = best tier among contributing tokens. Arbitration:

| Comparison | Resolution |
|---|---|
| Candidate agrees with baseline | `Confirmed baseline` |
| Baseline empty, candidate ≤ Tier 3 | `Filled empty baseline` |
| Candidate tier **better** than baseline tier | `Overrides baseline` |
| Candidate tier **equal** to baseline | `Conflict - needs review` |
| Candidate tier **worse** than baseline | `Conflict - needs review`, baseline retained, low review priority |

`credibility_margin` = `baseline_tier − winning_source_tier` records how decisively the evidence
beat the incumbent. It is computed but no longer emitted to `resolved.csv`. This is the rule that lets an EPA filing correct an `osm_2022`
name while the same filing against `usa_ghgrp_2026` goes to a human.

## 6. Prompt changes

- Ship tier **definitions**, not bare labels.
- Require `publisher_class` and `publication_date`; state the undated→Tier 3 cap explicitly.
- Fix site context for the new seed: drop `point` and `data_source`; keep `site_id`, `site_name`,
  `country_iso3`, `latitude`, `longitude`, `input_area_square_meters`, `municipality`, `admin1`,
  `admin2`, `formatted_address`.
- Replace *"Do not overwrite non-empty baseline values"* with *report what sources say and cite it;
  the pipeline arbitrates.* The current wording suppresses exactly the high-credibility corrections
  Q6 is meant to capture.
- Attach **all** sources to a claim rather than relying on first-listed. `primary_source()` is
  replaced by best-tier selection.

## 7. Module plan

| File | Change |
|---|---|
| `schema.py` | Restructure. New header constants, `SOURCE_TIER_DEFINITIONS`, `BASELINE_DATASET_TIERS`, resolution enum. Old header constants deleted. |
| `credibility.py` | **New.** Tier normalization, publisher-class → tier, undated cap, baseline tiering, promotion gate. Pure functions. |
| `arbitration.py` | **New.** `resolve(site, attribute_name, evidence) -> resolved_row`. Pure. |
| `run_context.py` | **New.** Shared run scaffolding: `PipelineConfig`, output paths, cached-response IO, agent client. Depends on neither phase. |
| `search.py` | **New.** Phase 1: site selection, prompting, tool-failure retry, run log. The only phase that costs money. |
| `arbitrate.py` | **New.** Phase 2: evidence extraction, arbitration, and every output. Pure and offline. |
| ~~`pipeline.py`~~ | Removed. It had grown to 907 lines holding both phases; split into the three modules above. |
| `prompt_builder.py` | §6. |
| `workbook_io.py` | New sheets, validations, conditional formatting on `resolution`. |
| `seed_source.py` | Pass `internal_facility_id` and `contributing_data_sources` through to the pipeline. |

`credibility.py` and `arbitration.py` are pure and DB-free so the rubric is unit-testable without
network or database — the same split that made `seed_source.fold_facility_rows` testable.

## 8. Phasing

1. **Schema + credibility primitives** — `schema.py`, `credibility.py`, tests. No behavior change.
2. **Arbitration** — `arbitration.py`, tests against synthetic evidence sets.
3. **Pipeline rewire** — three-level emission, prompt changes, workbook. Re-run pilot, diff against
   `outputs/runs/foundry_pilot_10` to see what the new gating would have changed.
4. **Scale** — only after (3) is reviewed on real output.

**Correction to the original phasing.** The plan assumed the existing raw responses under
`outputs/runs/*/raw_foundry_responses/` could be replayed to show what the new gating changes.
They cannot: `pilot_sites.csv` and `consolidated_sites.csv` use **different `site_id` spaces**.
All 10 old pilot IDs exist in the new seed and **none of them refer to the same facility** —
`11467` was "Abule Egba Landfill" (NGA) and is now a Canadian "domestic landfill". Replaying old
responses against the new seed attaches correct findings to the wrong facilities.

The replay is therefore only a code smoke test, not a data comparison. What *does* survive the ID
mismatch is the source-level tier analysis, because it depends on the sources in the responses and
not on which facility they were matched to: re-scoring them moved **31 of 56 evidence rows (55%)**,
dominated by 19 news reports demoted Tier 2 → Tier 3. Comparing gating outcomes requires a fresh
run against the new seed.

## 10. Standardized facility spec conformance

`inputs/StandardizedFacilityTableSpecification.md` is **live, not aspirational**:

- The 5 categorical enums it defines exist in Postgres now (`facility_status`, `facility_type`,
  `cover_type`, `gccs_energy_project_type`, `gccs_current_project_status`) and their labels match
  the document exactly.
- `transformed.transformed_*` **are** the spec's standardized source tables — 43 columns each, one
  table per `data_source` token, 11 of them (`transformed_osm` holds 8,627 rows).
- The referenced `sql/standardized_facility.sql` is **not present in this repo**, and no
  `std_facility_tbl_*` table exists. The enums are the only machine-readable contract available.

So the pipeline shape is: `transformed.transformed_<source>` → merge → `consolidation.consolidated_facility`
(our seed). AI search is another *source*, which means its natural output is a spec-conformant
standardized table that merges like any other.

### 10.1 Every categorical value the agent returns is invalid (measured)

Checked the 21 categorical values in the existing raw responses against the live DB enums:

| Attribute | Agent returned | Valid? |
|---|---|---|
| `facility_status` | `'closed'` ×5, `'inactive'` ×3, `'active'` ×2 | **0 / 10 valid** |
| `facility_type` | `'sanitary landfill'` ×7, `'controlled dumpsite'`, `'legacy landfill'`, `'dumpsite'`, `'landfill'` | **0 / 11 valid** |

Two independent causes:

1. **Casing.** `DEFINITION_VALUES` is lowercase; the enums are Title Case. `'active'` ≠ `'Active'`.
   This alone invalidates every value.
2. **Vocabulary.** `DEFINITION_VALUES` offers values the database does not have. `facility_status`
   has **7** entries against the enum's **2** — and `'closed'`, the most frequently returned status,
   is not one of them. `facility_type` offers `open dump`, `transfer station`, `legacy landfill`,
   `unknown`; the enum has none of those and adds `Incineration Facility`, which we never offer.
   `'landfill'` appeared in output without being in either list — free-form leakage.

Nothing catches this today: `FIELD_TO_DEFINITION` omits `facility_status` and `facility_type`, so
`validate_category` never runs on them.

### 10.2 Units are in better shape than enums

For the 8 in-scope attributes, only mass conversions apply, and `unit_converter.py` covers them:
kg→t and US short tons→t both match the spec (0.90718474 vs the spec's 0.907185). Reported units
were all parseable (`metric tonnes per year`, `metric tonnes/year`, `metric tonnes`).

Latent gaps, unreachable while scope stays at 8 attributes but required if it widens:

| Spec conversion | Status |
|---|---|
| feet → meters (×0.3048) | missing — needed for `waste_depth_meters` |
| square feet → m² (×0.092903) | missing — needed for `area` |
| acres → m² (×4046.856) | missing — needed for `area` |
| ft³ CH₄ → kg CH₄ (×0.0192) | missing — needed for any `gccs_*` |
| MMCFD → t CH₄/year | missing — needed for any `gccs_*` |
| **Fractions 0–1, not percent** | not enforced — `gccs_ch4_percent`, `oxidation`, `mcf`, all `*_percent_by_weight` |

`unit_converter.py` also supports long tons and pounds, which the spec's table does not list.

### 10.3 Structural mismatches

| Issue | Detail |
|---|---|
| `has_biocover` | Not a spec column at all. The spec has `has_cover` (bool) + `cover_types` (array of `cover_type`, which includes `'organic cover'`). |
| `found_coordinates` | Spec has **separate** numeric `latitude` / `longitude`. We emit one `"lat, lon"` string. The `found_latitude`/`found_longitude` values flagged as drift in §3.1 are in fact spec-aligned. |
| `year` semantics | Spec: one record per facility-year, and *"a source record may be transformed into multiple standardized records when different measurements refer to different years."* `Resolved` is per facility-attribute, so emitting spec records requires a year pivot off `value_date`. |
| `data_source` | The enum has 11 tokens and none for AI search. A spec-conformant table needs a new one. |
| `facility_id` | Spec types it **text**; our `internal_facility_id` is an integer. |
| Missing baseline tiers | `sinir_2024` and `manual_entry` are in the spec's `data_source` list but absent from the seed, so Q7/Q13 do not cover them. |

## 9. Decision log

All settled. Where an answer overrode my recommendation it is marked.

| # | Decision | Outcome |
|---|---|---|
| Q1 | Verification scope | 8 substantive attributes; other seed columns not sent to the agent |
| Q2 | Credibility rubric | Four bands on publisher class; undated caps at Tier 3 |
| Q3 | Gating | Tier gates promotion; Tier 4 / Tier 5 never auto-validate (later relaxed for empty baselines by F1) |
| Q4 | Output target | Read DB → write CSV; no DB writes without explicit go-ahead |
| Q5 | Row grain | Three levels: Evidence → Resolved → Sources |
| Q6 | Baseline authority | Baseline tiered by its own datasets; relative credibility arbitrates |
| Q7 | Dataset tiers | As drafted, except **`mexico_inegi_2016` raised to T1** (overrode T2) |
| Q8 | Agreements | One Resolved row always; Evidence capped at 2 rows per confirmation |
| Q9 | Tier 3 escalation | No — hard floor kept |
| Q10 / Q16 | Staleness | Backfilled baselines lose one tier past a 3-year gap, floored T3, mass attributes only |
| Q11 | `has_biocover` baseline | Superseded by Q22 |
| Q12 | Calculated distance | `Resolved` with `resolution = Calculated`, no tier |
| Q13 | Missing tokens | `canada_ghgrp_2021` → T1, `waste_atlas_dumpsites_2013` → T3 |
| Q14 | Format at scale | Workbook for small runs, CSV above |
| Q15 / Q30 | Auto-validation | Confirmations auto-validate; Tier 1–2 fills auto-validate; Tier 3 fills reviewed |
| Q17 | Re-run semantics | Split `search` / `arbitrate`; arbitration rebuilds from cache |
| Q18 | Old constants | Deleted |
| Q19 | Standardized table | Emitted as a fourth artifact |
| Q20 / Q26 | Categorical mapping | Collapse onto DB enums; **`landfill` → `Sanitary Landfill`** (overrode my NULL / lead-only recommendation — see risk note below) |
| Q21 | Enum source of truth | Generated from the DB into a committed module |
| Q22 | `has_biocover` | Dropped; `cover_types` searched instead |
| Q23 | Coordinates | Split into `found_latitude` / `found_longitude` |
| Q24 | Year | **One record per facility, `year` = latest year of record** (overrode the per-year pivot) |
| Q25 | New token | `ai_search_2026`; `sinir_2024` → T1, `manual_entry` → T3 |
| Q27 | `facility_id` | `internal_facility_id` as text; rows only where something was promoted |
| Q28 | Confirmations | Included in the standardized table |
| Q29 | Search mode | **Gap-fill** — only attributes empty for that facility are requested |
| Q31 | SME artifact | Filtered review queue workbook; other levels are CSV sidecars |
| Q32 | Cost | Site delay 3s; prompt trimmed to the requested attributes |
| Q33 | Credibility is attribute-dependent | A coordinate directory is Tier 3 **for coordinates only**, Tier 4 for everything else. Wikis stay Tier 4 even for coordinates. |
| Q34 | Identity gate | Found coordinates more than 5 km from the seed withdraw auto-validation for that whole site |
| F1 | Fills bypass the credibility floor | An **empty** baseline is filled at any tier. Tier still decides auto-validation vs review. Reverses part of Q3. |
| F2 | Conflicts route by relative tier | Candidate better than baseline → review. Equal → review. Worse → leads. |
| F3 | Leads stay out of the queue | Lead-routed rows get `validation_status = Routed to leads` and are excluded from `review_queue.csv`. |
| F4 | Closure emphasis | `closing_year` is flagged REQUIRED for inactive sites, with upfront emphasis when the baseline or the name already says closed. |
| F5 | Coordinate review threshold | Coordinate differences under **1 km** auto-validate instead of queueing. |
| F6 | Widened attribute set | Added `waste_depth_meters`, `has_cover`, 4 GCCS methane quantities, 2 GCCS project enums, `gccs_collection_efficiency`. 20 requestable attributes, up from 11. |
| F7 | GCCS gated on gas collection | The 7 GCCS attributes are requested **only** where gas collection is known present. Unknown is deferred, not dropped. |
| F8 | Two-pass seed refresh | `refresh-seed` merges a run's promoted values back into the seed, so a facility whose gas collection is discovered on pass 1 gets its GCCS attributes on pass 2. |
| F9 | Search-tool failure is detected and retried | The agent's tool failure arrives as a valid JSON response, invisible to exception-based retry. Now detected, retried automatically (3 attempts, 20s apart), and reported. |
| F10 | Cumulative run log | A resumed or retried search merges into `foundry_run_log.csv` instead of replacing it. |
| F14 | Country scope | USA and (since F26) Brazil excluded by default. Brazil, when searched: coordinates only, and only where `is_location_exact` is FALSE — 3,913 of 4,225 SINIR sites (F26). Sites with nothing to ask are dropped from selection. |
| F13 | `area` and `has_biocover` added | 22 requestable attributes. `has_biocover` is a **new 44th standardized column**, not yet in the published spec. `waste_depth_meters` was already searched. |
| F12 | Per-attribute baseline provenance | Baseline tier comes from `consolidation.value_resolution_ledger`, joined per attribute, instead of the facility-level composite. `baseline_source` is published in `resolved.csv` and the review queue. |
| F15 | Schema realigned to upstream `8c0bb3fe` | `area` → `area_square_meters`; `waste_depth_meters` (numeric) → `waste_depth` (derived category `<=5m` / `>5m`); `has_biocover` is now spec-defined rather than a local 44th column, and is read from `consolidated_facility` instead of always gap-filled. The spec is vendored against a pinned upstream commit (`scripts/sync_schema.py`), so F6 and F13 above record the pre-rename state. |
| F16 | Seeded from the database, not a file | `search` and `run` read `consolidation.consolidated_facility` directly; `inputs/` is gone. `--input-csv` remains for a pinned or offline corpus and for the pass-2 follow-up. Each database-seeded run writes `seed.csv` into its own run directory, so what it searched is still recoverable. The rows above describing `inputs/` record the pre-F16 layout. |
| F17 | Not a Waste Facility (WP-525) | Facilities known only from OSM or Global Plastic Watch may get `facility_type = Not a Waste Facility` when a source says the site was never a waste site. Silence leaves facility_type empty, never a negative. Closure is explicitly not a contradiction. Promoted like any facility_type and always routed to review. The value is pending upstream: the DB enum and `chk_facility_type` reject it until extended. The prompt premise no longer asserts every site is a disposal facility. |
| F18 | Trusted names are not re-searched (WP-531) | `found_facility_name` is skipped where the seed's name came from a Tier 1–2 source — about 10,609 of 19,492 facilities. Reverses Q30 for those facilities only: identity rests on coordinates, which are still requested everywhere, so the Q34 gate is unaffected. Accepted because those names mostly came through translation, and comparing the agent's local-language name with the English seed name produced conflicts that were translation noise. Requires per-attribute provenance; the facility-wide composite is never used, since it would credit an OSM name with a regulator's tier. |
| F19 | Operator searched, review layer only (WP-531) | `operator` is a gap-fill attribute: the company, municipality or agency running the site, not the owner or regulator. The upstream spec has no column for it, so it is kept out of the standardized table until one exists. |
| F20 | Original names in every output (WP-531) | `site_name`, `original_site_name` and `source_language` sit side by side in Review_Queue, Leads, Contradictions, `resolved.csv` and `evidence.csv`, filled in one pass keyed by site. Review_Queue gains an editable `translation_note` beside them. |
| F21 | What distinguishes the facility types (WP-531) | A live check on Manresa (18133) found the new definitions alone still yielded Controlled Dumpsite, auto-validated from a Tier 1 source: the agent read compaction against the Controlled Dumpsite definition. The guidance now states that compaction, cover and access control occur at both types and only engineered containment decides it, and - for the EU27 and EEA only - that a landfill accepting waste after July 2009 is bound by the Landfill Directive and is a Sanitary Landfill unless a source says otherwise. Re-checked on five Catalan sites: 5/5 Sanitary Landfill, two from described engineering and three from the Directive, all routed to review. |
| F22 | Workbook-safe text and links | Characters openpyxl rejects are replaced with a space in every cell, since one would abort the whole workbook. Hyperlink targets are made safe - raw spaces and non-ASCII percent-encoded, non-ASCII hosts IDNA-encoded - while the cell shows the source's URL unchanged; existing `%` escapes are never decoded (the refining-ai-search RDP-63 lesson). Only `http`/`https` links are clickable, and none beyond Excel's 2,079-character limit. |
| F23 | Source links checked (WP-533) | Ported from refining-ai-search RDP-63. Cited URLs are fetched at the end of the search phase and given OK / Broken / Unverified verdicts in `link_check.json`; arbitration only reads them, so it stays offline. A Broken source is treated like one with no URL - not promoted, routed to Leads - unless `--keep-broken-link-evidence`. Unverified is never touched. |
| F24 | Blob storage and SharePoint (WP-534) | Run folders mirror to Azure Blob (adapted from refining-ai-search RDP-52): pull before searching so paid responses are reused, push after each search pass and after arbitration, never overwrite local files on pull. Used once configured, local until then. Review workbooks are copied into a OneDrive-synced SharePoint folder; a copy an SME has edited or has open is never overwritten. |
| F25 | `resolved.csv` keeps only auto-validated values (WP-542) | Only rows that are promoted (`Confirmed baseline`, `Filled empty baseline`, `Overrides baseline`), `Auto-validated` and carry a value are written - on `pilot10_v7`, 8 of 145 rows are kept; 14 are in the review queue, 120 were `Not found`. Needs-review rows live in the review workbook until an SME promotes them. The full set is still built in memory, so the review queue, Contradictions and the standardized table are unchanged. The pass-2 refresh reads `resolved.csv` plus `review_queue.csv`, so a gas collection system awaiting review still unlocks the follow-up. |
| F26 | Brazil SINIR municipality in the prompt (WP-543) | SINIR coordinates are a municipality centre and most names are generic ("Lixão"). The seed now carries SINIR's recorded municipality and state (`facility_city_location` named via `raw_sinir_general_city_data`) into `municipality`/`admin1` - all 3,913 inexact sites. On the same 20 sites: without it, 3 locations came back, all wrong (two other facilities 140 and 237 km away, one city hall); with it, 0 came back, and the agent identified the real facility in research and government reports that publish no readable coordinates. Web search improves precision here but finds no coordinates; matching against OSM/GPW landfills within the municipality is the likelier fix. So Brazil joins the US in `DEFAULT_EXCLUDED_ISO3`; `--iso3 BRA`, `--site-ids` or `--include-excluded-countries` still searches it. |
| F27 | Bing queries counted; retries capped (WP-545) | Each response's `web_search_call` items list the queries Bing was sent; their count, summed over every attempt, is logged per site as `web_searches` (adding up across resumed runs and follow-up passes) and totalled per run. Foundry-error and search-tool retries both default to 2, so a site runs the agent at most 4 times instead of 9. One live site with 12 attributes made 39 queries. A budget asked for in the prompt was ignored (233 vs 235 queries on the same 10 sites), so searches are capped with the Responses API's `max_tool_calls` (default 6 per attempt, `--max-web-searches`), which Foundry enforces: 189 queries on those sites, with similar values found. Each search sends ~4 queries, so the cap is on searches. |
| F28 | Sites searched in parallel (WP-546) | `search` runs `--workers` sites at once (default 4) on a thread pool; a search is mostly waiting on Foundry. The WP-545 10 sites took 1 min 36 s with 4 workers against about 4 minutes one at a time (hung site excluded), with no 429s. Each thread has its own client, because a client holds the Bing count of the site it is searching. The SIGALRM site timeout works only on the main thread, so `--hard-site-timeout-seconds` is now enforced in the client: each call runs in a daemon thread and is abandoned at the deadline, and no retry starts past it. That also ends the WP-545 case where a timed-out site was retried for another 15 minutes. Past runs: median call 41 s, p99 98 s, so 240 s cuts off only hung calls. |
| F29 | Trusted coordinates are not re-searched | `found_latitude` and `found_longitude` are skipped where both seed coordinates came from a Tier 1–2 source, as F18 does for the name: 4,063 of 12,122 searchable sites (Mexico INEGI 2,202, E-PRTR 1,729, Canada GHGRP 132), all with exact locations. Saves 8,126 of 175,293 attribute-requests (4.6%), about $150 of Bing searches over a full run if queries fall in proportion. Coordinates flagged inexact are still searched. The seed coordinates stay in the prompt to steer the search. See Accepted risks. |
| F30 | Full run in six country batches | `--batch N` (on `search` and `run`) searches one batch of `COUNTRY_BATCHES`. Ranking by how empty a baseline is did not separate countries: most are 85-95% blank on the core fields, and opening year, incoming waste and waste in place are blank for 97-100% of sites in every large country but Mexico (67%). So batches 1-2 are English-speaking, for SMEs to learn the review on familiar sources, and 3-5 follow the count of blank core fields (sites x share blank), about 2,000-2,300 sites each. Batch 6 is every unlisted country, so a country new to the database is never left out. On the 3 Oct 2026 seed the six cover all 12,122 searchable sites once each. |
| F31 | Flare and waste-type attributes; three methane volumes dropped | `bulk_waste_type` (enum: municipal solid waste, inert waste, others; unknown is NULL, not a value) is gap-filled for every facility, classified by the waste that makes up most of what a site receives; `has_flare` (boolean) and `flare_efficiency` (fraction 0-1, percentages converted) are gated on gas collection like the rest of GCCS, since a flare burns collected gas. None has an upstream column yet, so like `operator` they reach `resolved.csv` and review but not the standardized table. `gccs_ch4_generated_metric_tonnes`, `gccs_ch4_collected_metric_tonnes` and `gccs_ch4_flow_to_project_metric_tonnes` are no longer searched (`UNSEARCHED_ATTRIBUTES`) but stay readable from the seed and older cached responses. On the 3 Oct 2026 seed: +12,122 requests for `bulk_waste_type`, +424 for flares on the 212 sites with known gas collection, -636 dropped; net +11,910 (+7.1%). |
| F32 | Each run keeps the consolidation tables it searched | `internal_facility_id` (every output's `site_id`) is reassigned by each consolidation rebuild, so results cannot be tied back to facilities, or changes found, without the state a run searched. Seeding from the database first saves `consolidated_facility`, `value_resolution_ledger` and every `entity_linkage.crosswalk_*` table, whole, as gzipped COPY CSVs in one read-only REPEATABLE READ transaction, with a manifest of `consolidation_run_id`, row counts (from the database: values hold line breaks) and checksums. The crosswalk's `data_source` + `facility_id` is the key that survives a rebuild. The run stops if `consolidation_run_id` changes while it seeds. 7 s and 4.6 MB on the dev database. Diffing a later consolidation against it, to search only changed facilities, is a follow-up. |
| F33 | Transfer stations and recycling centres (WP-551) | `facility_type` offers `Transfer Station` and `Recycling Center`, both pending upstream: Transfer Station is in every `transformed.*` CHECK but not the enum type or `consolidated_facility`'s CHECK; Recycling Center is in none. The prompt's allowed list is built from the enum, so a regenerated `db_enums.py` reaches the agent without a code edit. A transfer station or recycling centre on a closed landfill is classified as the landfill, since the buried waste still emits. |
| F34 | One worklist (WP-550) | The Contradictions tab is gone: a "Not a Waste Facility" verdict, or any facility_type that differs from the baseline, is reviewed on the site's own `facility_type` row in Review_Queue. The closure cross-check moved into that row's `evidence_summary`. Separate tabs for overlapping purposes confused reviewers. |
| F35 | Newer evidence wins (WP-558) | Within a tier the newer `value_date` wins, ahead of corroboration. For `facility_status` only, a lower-tier source down to the promotion floor (Tier 3) may beat a better-tier source or the baseline when at least 3 years newer, always for review: a dated Tier 1 permit against recent Tier 3 news is the common case. `resolution_rule` states the dates. Re-arbitrating batch 1 changed 4 values (pass-1/pass-2 disagreements on gas-project fields). |
| F11 | Tier 5 replaces the Unrated sentinel | An unclassifiable source now sits on the ordered scale at Tier 5 rather than out-of-band at 99. `Other` removed from `source_type`: 15 types, no catch-all. |

### Accepted risks

- **No identity check where name and coordinates are both trusted (F29).** Those 4,063 sites
  are the same ones whose name F18 already skips, so neither is searched and the Q34 gate has
  no found coordinates to measure. A search that researched the wrong facility - the pilot's
  site 2695 resolved 238.6 km away - would auto-validate unflagged. Accepted by decision to cut
  cost; revisit if SME review of batch results finds values belonging to another facility.
- **`landfill` → `Sanitary Landfill` (Q26).** *Status after 10 sites: not yet triggered.* The
  agent returned exact enum values on every one of the 12 `facility_type` claims, so the generic
  fallback never fired and no mapping was needed. The risk below stands but is unrealized.
  `Sanitary Landfill` carries MCF = 1.0 in the seed
  (12,623 of 12,626 rows) — the maximum methane correction factor — and 68% of those rows have gas
  collection. The corpus base rate favouring Sanitary Landfill is a US artifact: USA is 19,074
  sanitary to 1 dumpsite, while NGA and PHL have **zero** sanitary landfills and only dumpsites.
  A generic one-word `landfill` will therefore be classified as an engineered facility in exactly
  the countries where that is least likely. Accepted by decision; revisit if modeled emissions in
  non-US countries look high.
- **Year semantics (Q24).** A value dated 2008 can sit in a record stamped `year = 2026`, which the
  spec says should not happen. Per-attribute `value_date` is preserved in `resolved.csv`; the
  43-column spec has nowhere to carry it.
- **Gap-fill makes Q6 mostly latent (Q29).** Because filled fields are never queried, the
  relative-credibility arbitration rarely fires. Wrong values already in the seed will not be
  challenged. Running verification mode on NGA+PHL (198 sites, ~3h) would exercise it cheaply.

### Why credibility had to become attribute-dependent (Q33)

The first 10-site run exposed a flaw in the original rubric. **14 of 15 coordinate sources were
Tier 4** (`map or coordinate directory`, `wiki or aggregator`), because coordinates live on
gazetteers by nature. Consequences:

- `found_latitude` and `found_longitude` promoted **0 / 10** times.
- `distance_to_original_coordinates_km` **never computed** — it needs both to resolve.
- 8 fills auto-validated with **no identity confirmation at all**, which is exactly what the Q30
  auto-validation rule depends on.

A coordinate directory is a poor source for operational metadata and a purpose-built one for a
location. So `(attribute, source_type)` pairs can override the tier, narrowly: coordinates from a
coordinate directory are Tier 3 — promotable, able to confirm a baseline, never able to override it
silently. Wikis are not gazetteers and stay Tier 4 even for coordinates.

Measured effect on the same cached responses, re-arbitrated at zero cost:

| | Before | After |
|---|---|---|
| `Insufficient credibility` | 16 | **6** |
| `Confirmed baseline` | 0 | **3** |
| `Calculated` (distance) | 0 | **5** |
| Review queue | 33 | 32 |

### The identity gate (Q34)

With distance computable, it immediately caught a real error: site 2695 "Barangay Cauayan Dumpsite"
resolved to coordinates **238.6 km** from its seed location — the agent had researched a different
facility — while its two Tier 1 fills sat auto-validated. The other four distances were
0.003–0.195 km.

Tier cannot detect a credible source describing the wrong facility; distance can. So found
coordinates more than **5 km** from the seed now withdraw auto-validation for every row of that
site and stamp the reason into `researcher_notes`. The threshold is a judgment call — a landfill
footprint spans at most a couple of km and seed coordinates are polygon centroids — and is a single
constant, `IDENTITY_DISTANCE_KM_THRESHOLD`.

### Pilot feedback, applied

Review queue fell **32 → 20 rows (-38%)** on the same cached responses, at no API cost.

| | Before | After |
|---|---|---|
| Review queue | 32 | **20** |
| `Filled empty baseline` | 20 | 20 (14 now queued, 6 auto-validated) |
| `Conflict - needs review` | 9 | **3** |
| `Conflict - lower credibility` → leads | 0 | **6** |
| Coordinate rows in queue | 8 | **2** |

The two coordinate rows left are site 2695's, 238 km out. Six coordinate differences of
0.011–0.183 km now auto-validate as the same place.

**F1 reverses part of Q3 deliberately.** The credibility floor no longer applies when the baseline
is empty — a Tier 4 value beats no value. Tier still governs whether a human sees it, so Tier 4
fills land in the queue rather than being auto-validated. The floor still applies the moment a
baseline value exists.

**F2 leaves equal-tier conflicts in review.** The feedback specified better → review and worse →
leads, but not equal. Equal credibility has no automatic winner and discarding it would lose real
signal — it is the case that motivated raising `mexico_inegi_2016` to Tier 1 in Q7 — so equal-tier
conflicts stay in the queue.

**F4 cannot be validated from cache.** It changes the prompt, so its effect only appears on a fresh
search. Note also that `closing_year` *was* already being requested for every pilot site including
Subic and Barangay Cauayan; the agent searched and returned nothing. F4 pushes harder rather than
adding a missing attribute. Upfront emphasis fires for 2 of the 10 pilot sites (2056 via baseline
status, 2695 via "(Closed)" in the name); the rest rely on the in-pass rule that discovering
closure obliges a closing-year search.

### The widened attribute set activated the latent unit conversions (F6)

Section 10.2 flagged five spec conversions as missing but unreachable while scope stayed at 8
attributes. Adding the GCCS and depth attributes made all five reachable, so they are now
implemented and tested:

| Spec conversion | Implementation |
|---|---|
| feet → meters (×0.3048) | `convert_length_value`, plus yards |
| ft³ CH₄ → kg CH₄ (×0.0192) | `convert_ch4_volume` |
| m³ CH₄ → kg CH₄ (0.679 kg/m³) | `convert_ch4_volume` |
| MMCFD → t CH₄/year | `convert_ch4_volume`; verified against the spec formula |
| Fractions 0–1, not percent | `convert_fraction_value` for `gccs_collection_efficiency` |

Three things that needed care:

- **MMCFD hid its own time denominator.** `mmcfd` does not match a `/day` pattern, so the spec's
  ×365 was silently skipped and results came out 365× too small. Gas-flow abbreviations
  (`mmcfd`, `mcfd`, `scfd`, `scfm`) are now split into volume scale plus time denominator before
  annualization. A test pins the result to the spec's own formula.
- **A landfill-gas volume is not a methane volume.** The spec's MMCFD formula takes a methane
  fraction as an input. When a source reports raw landfill gas without one, conversion is
  *refused* with a warning rather than assuming a fraction and fabricating a number.
- **Spec ranges are now enforced.** `ATTRIBUTE_RANGES` rejects depth ≤ 0, negative masses, and
  efficiency outside 0–1 as non-promotable, so an out-of-range value cannot reach a spec column.

Cost note: the prompt grew from ~6.8k to ~10.1k characters for a site with an entirely empty
baseline. Gap-fill still keeps it smaller for facilities that already have data.

### GCCS attributes are gated on gas collection (F7/F8)

Measured on the pilot: the GCCS quantities returned **0 values from 100+ requests**. The seed
explains why — the facilities that have gas capture mostly already have the numbers:

| | |
|---|---|
| Facilities with gas collection | 1,221 |
| …already carrying the GCCS quantities (GHGRP / LMOP) | 1,007 (82%) |
| **Real addressable gap** | **214** |
| `has_landfill_gas_collection = FALSE` | 3,614 |
| unknown | 10,702 |

GCCS attributes are therefore requested only where gas collection is **known present**.

| Gate | Attribute-requests across the seed |
|---|---|
| None (all 20 attributes) | 271,983 |
| Skip only where explicitly FALSE | 251,945 |
| **Skip unless TRUE (current)** | **177,031** (−35%) |

### Nothing is dropped, only deferred (F8)

`has_landfill_gas_collection` is itself a gap-filled attribute, so the unknown-status facilities
are not abandoned. `refresh-seed` merges a run's promoted values back into the seed and the next
`search` pass picks up what pass 1 unlocked:

```bash
uv run waste-ai-search search      --run-id pass1
uv run waste-ai-search arbitrate   --run-id pass1
uv run waste-ai-search refresh-seed --run-id pass1        # -> refreshed_seed.csv
uv run waste-ai-search search      --run-id pass2 --input-csv .../refreshed_seed.csv
```

Demonstrated on `pilot10_v2`: 7 facilities updated, 20 values merged, **1 gas collection system
discovered**. Site 2062 (Olushosun) asked 10 attributes on pass 1 with **0** GCCS because its
collection status was unknown; pass 1 established it as TRUE, and pass 2 asks 13 attributes
**including all 7 GCCS**, while dropping the 4 it already answered.

Two rules keep the loop honest:

- **Only promoted values merge.** `Conflict - needs review` and `Conflict - lower credibility`
  rows are excluded — a value still awaiting a human must not quietly become baseline.
- **Merged values keep their own tier.** `ai_filled_fields` records `field@Tier N`, and
  arbitration tiers an AI-filled baseline by the source that supplied it rather than by the seed's
  datasets. Without this, a Tier 3 value filled on pass 1 would be defended on pass 2 as though it
  came from the seed's Tier 1 registries, and a genuine Tier 1 correction would be misjudged.

Identity findings are **not** merged unless `--merge-identity` is passed: overwriting seed
coordinates changes what the distance-based identity gate (Q34) is measuring against.

### Search-tool failure is not "no data found" (F9/F10)

The Foundry agent's own web-search tool fails transiently. It arrives as a **successful API call
returning valid JSON** whose `search_notes` say the tool broke — so `foundry_client`'s
`max_retries=3` never fired, because there was no exception, and the run log recorded "Succeeded".
"Could not search" was indistinguishable from "searched and this facility is undocumented", and
only the first is worth retrying.

Observed across the session:

| run | tool failures | returned data |
|---|---|---|
| pilot10_v2 | 0 | 8/10 |
| pilot10_v3 | 2 | 2/10 |
| pilot10_v4 | 2 | 2/10 |
| pilot10_v5 (first pass) | **6** | 2/10 |
| pilot10_v5 (after retries) | **0** | 4/10 |

**It is transient and retry resolves it.** All 6 failing sites in v5 recovered, one needing two
attempts. So the search phase now detects the reported failure and retries the site itself
(2 attempts since WP-545, 20s apart, `--search-tool-retries`). Persistent failures are recorded with a distinct
status, listed in `sites_to_retry.csv`, and printed with a ready-made retry command.

Two related fixes this exposed:

- **The run log was not cumulative.** `run_search` rewrote `foundry_run_log.csv` with only the
  sites touched in that invocation, so retrying 1 site destroyed the other 9 rows — verified: the
  v5 log held 1 row where it should have held 10. It now merges, newest row per site wins. This
  matters most for the multi-day resumable run the scale-up needs.
- **A failed unit conversion could still be promoted.** A conversion that fails returns the value
  untouched in its *original* unit. Site 2114 reported `waste_in_place = 30000 "tons"`; the
  converter correctly refused to convert ambiguous tons, and the value was promoted anyway into a
  column named `metric_tonnes` — a 10% error if the source meant short tons. Any failed conversion
  now blocks promotion.

### Tier 5 closes the scale (F11)

Unclassifiable sources were previously held out-of-band as `Unrated = 99`. That worked for gating,
because every threshold is an upper bound, but it broke everything ordinal:

- `credibility_margin` had to be suppressed whenever either side was unrated, because
  `99 - 1 = 98` is not a credibility difference. Margins are now always computable.
- Comparisons needed a special case that was easy to forget when adding a rule.
- "Unrated" read like a missing value rather than a judgement, when in fact it *is* a
  judgement: we looked at the source and could not establish what it was.

Tier 5 is now part of one ordered 1–5 scale. Gating behaviour is unchanged — Tier 5 is above every
threshold, so it never auto-promotes against an existing value, never overrides, and never
auto-validates — but it does fill an empty baseline (F1) and lands in the review queue.

Measured on `pilot10_v5`: 6 of 20 evidence rows are Tier 5, **all** from `source_type: "Other"`.
So `Other` was removed from the vocabulary entirely — there is no catch-all. The 15 remaining
types all carry a real tier, and anything unrecognized or missing falls to Tier 5 with a rule that
says which case it was. The prompt tells the agent what to do instead of guessing: if no listed
type fits, the finding goes to `unverified_leads`. The scale is also surfaced in the workbook's
Definitions sheet so reviewers can see what a tier means.

Real margins from that run: `-4` (Tier 5 evidence against a Tier 1 baseline), `-2`, `0`, `+1`
(Tier 3 evidence beating a Tier 4 baseline) — all previously blank or meaningless.

### Module layout

```
run_context.py   config, paths, cached-response IO, agent client   <- neither phase
   |                                                                  depends on the other
   +-- search.py      phase 1: network, costs money, resumable
   +-- arbitrate.py   phase 2: pure, offline, free to re-run
```

Supporting modules: `credibility.py` (tier rubric), `arbitration.py` (resolve one attribute),
`standardized.py` (43-column spec emission), `seed_refresh.py` (two-pass loop),
`unit_converter.py`, `prompt_builder.py`, `schema.py`, `workbook_io.py`, `seed_source.py`,
`db.py` / `db_enums.py`, `geocoder.py`, `pilot_selector.py`, `input_loader.py`,
`foundry_client.py`.

Tests mirror the split: `test_search.py` and `test_arbitrate.py`.

### What each output is for

| File | Grain | Who reads it |
|---|---|---|
| `<run>_review.xlsx` → **Review_Queue** | Only rows needing a decision | **The SME. This is the worklist.** |
| `resolved.csv` | One row per value found and auto-validated (F25) | Anyone asking "what do we now believe, and why" |
| `evidence.csv` | One row per source-backed claim | Anyone asking "why did this value win or lose" |
| `sources.csv` | One row per unique URL | Source audit, tier drift monitoring |
| `std_facility_tbl_ai_search_<run>.csv` | One record per facility, 44 spec columns | The merge into `transformed.*` |
| `std_facility_tbl_ai_search_<run>.load.sql` | Load statement, every column named | Loading the above without a positional dependency |
| `supplementary_leads.csv` | Findings that lost | Optional manual promotion |
| `sites_to_retry.csv` | Sites whose search tool failed | Re-run these; not "no data" |

The review queue and `resolved.csv` are **disjoint** (F25): a row is written to `resolved.csv` only if
it is auto-validated, and to the queue only if `validation_status = "Needs review"`. `Not found` rows
go to neither. (Before F25, `resolved.csv` held every facility x attribute searched and the queue
was a subset of it — on `pilot10_v5`, 12 of 136 rows.)

Rows the pipeline signs off itself now carry `reviewer = "AI Agent"`, the arbitration date, and the
agent's own evidence summary in `researcher_notes`, so an auto-validated row is distinguishable from
an unreviewed one rather than showing three blank columns. On `pilot10_v5`: 122 of 136 rows signed by
the agent, 14 left blank and awaiting a human.

Five columns were removed from `resolved.csv` as not useful in review:
`baseline_carried_from_earlier_year`, `baseline_staleness_note`, `agreeing_source_count`,
`dissenting_source_count`, `credibility_margin`. The staleness penalty still runs — it just no
longer reports itself in every row. `agreeing_source_count` is retained in the review queue, where
corroboration helps a reviewer decide.

### Country scope (F14)

Two cost rules, both one-line edits in `schema.py`:

- **`DEFAULT_EXCLUDED_ISO3 = {"USA"}`** — 3,145 facilities, 20% of the corpus, already Tier 1
  covered by `usa_ghgrp_2026` and `lmop_2024`. Searching them mostly manufactures equal-tier
  conflicts for human review rather than new data.
- **`COORDINATES_ONLY_ISO3 = {"BRA"}`** — `found_latitude` and `found_longitude` only, **and only
  where `is_location_exact` is FALSE.** Brazil's other attributes are government-sourced, so a web
  search should not overwrite them. `is_location_exact` is TRUE for all 303 Brazilian facilities
  today (all 15,537 corpus-wide), so the rule currently matches **nothing** and all 303 are
  skipped. It is a default now so inexact-location facilities are scoped correctly on arrival
  rather than needing to be remembered later. Unknown is not inexact: only an explicit FALSE asks.

The exclusion is a *default*, not a prohibition: naming the country in `--iso3`, listing
`--site-ids`, or passing `--include-excluded-countries` all override it, because asking for USA
and silently getting nothing back would be worse than useless. The run prints how many sites it
dropped and why.

Coordinates-only returns **before** the gap-fill and GCCS logic, so a Brazilian facility with gas
collection and a dozen empty fields still gets exactly two attributes.

A site with no requestable attributes left is dropped from selection rather than queried for
nothing — country scope is what can empty a site entirely.

| Scope | Sites | Attribute-requests | Serial runtime |
|---|---|---|---|
| No rules at all | 15,537 | 341,814 | — |
| Gap-fill + GCCS gate | 15,537 | 197,110 | 8.0 days |
| **+ country rules (default)** | **12,089** | **166,804** | **6.3 days** |

### `area` and `has_biocover` (F13)

`waste_depth_meters` was already in the search set, added with the GCCS batch. The two genuinely
new attributes:

**`area`** activated the last two unimplemented spec conversions — square feet (x0.092903) and
acres (x4046.856) — plus hectares and square kilometres, which the spec's table omits but which is
how most non-US sources report a footprint. Range-checked above zero. 10,995 facilities already
have an area, so gap-fill asks only the remaining **4,542**.

**`has_biocover`** is a **44th column that the standardized spec does not yet define.** It is
placed next to `has_cover` and `cover_types` because that is where it belongs semantically — but if
the real DDL appends it at the end instead, `STANDARDIZED_FACILITY_COLUMNS` must be reordered to
match or a positional load will misalign every column after it. A test pins the position so the
change is deliberate.

Two consequences worth recording:

- **The seed no longer derives `has_biocover` from `cover_types`.** It used to, back when Q22
  dropped it as a search target. Keeping that derivation would have suppressed the search for the
  **3,205 facilities** with any cover data, and passed our own inference off as a source-backed
  baseline with no ledger provenance to tier it honestly. The consolidated table has no such
  column, so the baseline is always empty and the attribute is always searched — all 15,537.
- **`has_biocover` and `cover_types` describe the same physical cover**, and are searched
  independently, so they can contradict each other. A `TRUE` biocover with no organic cover type,
  or `FALSE` with one, raises a parse warning naming both values. It is surfaced rather than
  auto-corrected: which of the two is wrong depends on the sources, and the reviewer has both rows.

The prompt states explicitly that plain soil, clay or sand cover is **not** a biocover, because the
obvious failure mode is answering `TRUE` for any cover at all.

### Baseline credibility is per attribute, not per facility (F12)

`contributing_data_sources` is a composite: `usa_ghgrp_2026 + osm_2022` for one facility, tiered by
the **best** of everything the facility draws on. That over-credits individual values — an
`osm_2022` site name inherits the Tier 1 standing of a GHGRP filing on the same facility, and a
regulator correcting that name deadlocks at equal credibility instead of winning.

`consolidation.value_resolution_ledger` records the winning source **per attribute**:
304,448 rows keyed `(internal_facility_id, year, column_name)`, unique, covering all 15,537
facilities, with the same 9 dataset tokens the tier map already knows.

Joining needs both of the ledger's grains:

| `resolution_granularity` | Key | Attributes |
|---|---|---|
| `facility` (`year IS NULL`) | facility + column | name, coordinates, type, opening/closing year, `has_cover`, `has_landfill_gas_collection`, `gccs_collection_efficiency` |
| `facility_year` | facility + year + column | status, tonnages, depth, cover types, GCCS quantities and project fields |

For a year-varying attribute the year must be **the year the seed fold actually took the value
from** — the backfill year when the reference year was empty, not `reference_year`. The seed already
records that in `backfilled_fields`, so the join uses it.

Measured on the full seed: **84,736 baseline values, 100% resolved to a per-attribute source, zero
misses**, and every value on a multi-source facility resolved to a token inside its own composite.
**252 values (0.3%) get a truer, weaker tier** — all Tier 1 → Tier 3, `waste_atlas` data that the
composite was defending with a registry's reputation. Small, but those are precisely the cases that
would have blocked a legitimate correction.

Precedence, most specific first:

1. a value a previous AI pass supplied, tiered by the source that supplied it (`ai_filled_fields`)
2. the ledger's per-attribute dataset (`attribute_sources`)
3. the facility composite — now only a fallback, and recorded as such in the note

Because arbitration is offline, the provenance is baked into the seed at generation time as
`attribute_sources` (`attribute@dataset` pairs, the same shape as `backfilled_fields`), not queried
during arbitration.

`baseline_source` replaces `baseline_source_datasets` in `resolved.csv` and is added to the review
queue. Both `baseline_source` and `baseline_tier` are left blank when the baseline is empty: a tier
on a field with no value is a claim about something that does not exist, and the fill path does not
consult it.

### Identity is always requested

Gap-fill scopes *metadata*, not identity. `found_facility_name`, `found_latitude` and `found_longitude`
are requested for every site even though the seed already has them, because they are the only
evidence that the agent researched the right facility. Auto-validating a Tier 1–2 fill (Q30) is
only defensible if identity is confirmed — a genuine regulator document about a neighbouring
landfill is the failure mode tier cannot detect.

**Superseded in part by F18 (WP-531):** `found_facility_name` is no longer requested where the seed's name came from a Tier 1–2 source. Coordinates remain requested for every facility, so the Q34 identity gate is unaffected; what changes is that those facilities' identity rests on coordinates alone.

