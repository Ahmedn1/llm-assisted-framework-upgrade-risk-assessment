# Design note

## 1. Decomposition into stages

A fixed sequence of stages, each saving its output before the next runs, so any stage can be re-run on saved
data:

1. **Resolve versions**: find the project's identity and release catalog, enumerate the interval.
2. **Collect evidence**: release records, registry metadata, release notes, guides, changelogs, linked issues.
3. **Normalize**: rank sources by trust, cut documents into version-scoped sections, tag topics, deduplicate.
4. **Score relevance**: triage every section before any LLM spends tokens on it.
5. **Extract risks**: facts → verified facts → grouped changes → risk paths → assembled model.
6. **Verify**: re-ground evidence, judge entailment, detect conflicts, assign final confidence.
7. **Finalize**: the schema-validated `final_output.json`, plus an interactive HTML report.

An alternative agentic path replaces stages 1–2 with a bounded LLM research agent (search/fetch actions, cited
quotes). Both paths feed the same stages 3–7.

## 2. The role of the LLM, and of decision models

Two kinds of model are used, each where it is strongest.

**The LLM (generative)** does what needs language understanding and generation: reading release notes and
writing atomic change facts with verbatim quotes, merging differently named mentions of the same change
(`render` vs `ReactDOM.render`), and turning each change into conditions, migration steps and code search
patterns. It works in small structured calls (one per batch of sections, one per change group), never in one
unstructured prompt.

**Decision models (classification)** answer fixed questions with a probability over predefined options instead
of free text. We use LiquidAI's d1-3B locally by default (a hosted Jev backend is interchangeable) at three
points:

- **Relevance gate** before extraction: is this section about a change that can affect applications, which risk
  type, and does it name something checkable?
- **Entailment judge** in verification: does the quoted evidence support, partially support, contradict or not
  address each condition?
- **Source scoring** in the resolver (optional): is this catalog the project's authoritative release source?

The split is strategic. Classification is most of the volume and needs no prose, so it goes to the small,
fast model; generation goes to the LLM, and only on sections that passed the gate.

- **Speed**: d1 scored Django's 200 sections in 47 s, about 0.23 s each; short source-scoring calls took
  80–110 ms. An LLM fact-extraction call on one batch of sections takes around 18 s.
- **Size**: 3B parameters, about 6 GB of GPU memory in bf16, loaded in 5 s on a laptop RTX 3080. Relevance and
  verification share one loaded instance.
- **Cost**: no per-call API cost. The Django 3.2 → 4.2 run made 238 LLM calls for extraction and 500 decision
  calls (200 relevance, 300 judge). Sent to the LLM, those 500 would have tripled the API calls.
- **Safety**: an answer is one of the offered options, validated against the rubric, so a decision model cannot
  invent a fact or a citation.
- **Independence**: the judge is a different model family from the extractor, so verification is not the
  extractor grading its own work.

The trade-off is judgment quality: d1's scores are uncalibrated, it has rejected relevant sections (React's React
Native note, Django's `USE_TZ` deprecation), and it hedges ("partially supports") on application conditions. The
gate is the main known source of lost recall (section 9).

## 3. What is deterministic

Everything except the three model calls above: version resolution, retrieval and caching, sectioning, trust
tiers, deduplication, quote verification, evidence assembly, severity caps, conflict resolution, the confidence
rubric, open questions, schema validation and the report. Models return bounded, schema-constrained outputs;
code decides what is kept.

## 4. Preventing hallucinated risks

- **Evidence is copied, never written by a model.** The LLM cites section and fact IDs (restricted by the schema
  to the batch it was given); code copies the quote, source and URL from the verified record.
- **Quotes are checked twice**: against the cited section during extraction and against the original document
  during verification. A fact whose quote is not a contiguous passage, or whose versions contradict the section's
  version heading, is rejected and logged.
- **Closed vocabularies**: every categorical field, including IDs and versions, is an enum in the schema.
- **Severity caps**: a path supported only by implied facts cannot exceed medium; one built only from bug fixes,
  security fixes shipped by the upgrade, or experimental features is capped at low.
- **Downgrade, never delete**: a condition that fails grounding or that the judge finds unsupported becomes
  `uncertain` with low confidence and is listed as disputed, so a reviewer sees it rather than losing it.

## 5. Risk condition versus useful context

A condition must name something an application or deployment can be checked for, tied to a target-version
change, and each path pairs a target-version condition with application conditions (roles `required`,
`alternative`, `optional`, `disqualifying`, `uncertain`). The prompts treat as context, unless the text says
applications depend on the old behavior: relaxations (something that used to error is now allowed), removed
development-only warnings, bug and security fixes shipped by the upgrade, announcements and optional features.
Context is kept in `contextual_facts`, not discarded. Known incompatibilities (a platform not yet supported) are
flagged separately because they can be both.

## 6. Conflicting or incomplete documentation

- **Trust tiers** come from recorded facts, not models: official (project site, repository, registry) >
  probable official > repository discussion > third party.
- **Conflicts** (removed vs deprecated, different versions or values, risk vs context) are resolved by trust,
  then specificity, then recency. Ties stay unresolved as open questions; every conflict is reported with all
  claims and the winner.
- **Gaps are reported, never filled**: versions without evidence, milestones without release notes and severe
  risks resting on one source become open questions; budget overruns and failed calls are run diagnostics.

## 7. Scaling to thousands of projects and versions

- **Extract per release, compose per interval**: facts are tied to versions, so each release is read once and
  stored, and any upgrade interval is answered from stored facts.
- **Incremental and cached**: retrieval is cached; a new release only adds its own documents and facts.
- **Cost tiers**: projects run in parallel; decision models absorb high-volume triage at near-zero cost, so LLM
  spend grows with relevant sections, not total documentation. Batch APIs or local LLMs cut it further.
- **No per-project mappings**: new ecosystems need registry and layout adapters, not new prompts.
- **Budgets used here**: the defaults were sized to fit one person's LLM budget in both time and API
  spend.
  - Collection: 100 documents, 60 registry records and 200 requests per upgrade.
  - Models: 200 sections scored for relevance, 150 sent to the LLM, 300 conditions judged.

  Each upgrade ran in 13 to 42 minutes. The price is recall on large upgrades: Django exceeded every
  model budget, and 366 of Pydantic's 566 sections were never scored. Overruns are reported as diagnostics,
  and every limit is an argument. At scale, these budgets become a throughput setting rather than a cap.
- **Raw text is a regenerable artifact**: `evidence.json` keeps the full text of every collected document (up
  to 171 MB per upgrade), so it is not committed. It is regenerated from the HTTP cache, or by re-fetching, with
  a collection-only run that makes no model calls. The final output carries the quotes and URLs it needs.
  At scale the same split holds: store raw documents in object storage keyed by content hash, and keep only
  facts and quotes in the database.

## 8. Evaluating precision and recall

- **Gold sets**: hand-written expectations per upgrade. `evals/react-17-18-distinctions.json` and
  `evals/pydantic-1-2-distinctions.json` have 16 checks each over the extraction distinctions the output must make, and
  `upgrade-pipeline evaluate` scores any output against them. The Pydantic set includes a trap: the collector
  also kept the migration guide of a different product on the same site, and nothing from it may become a risk.
- **Recall**: match extracted paths to each upgrade's official "backwards incompatible changes" and
  "deprecations" lists. For Django 3.2 → 4.2, a check of eight known changes went from 2 to 6 found after the
  collector fix, and traced the remaining misses to the relevance gate and budgets.
- **Precision**: sample paths and have reviewers label each as a real risk, context or wrong. Stratify by
  confidence level, so the rubric itself is validated (high-confidence paths should be almost always correct).
- **Stage metrics** (quote rejection rate, judge verdicts, conflicts) localize regressions to a stage.

## 9. Improvements with more time

- **Recall**: prioritize milestone release notes within budgets; keep `not_relevant` sections that contain strong
  risk keywords; split and retry LLM batches whose output was truncated.
- **Calibration**: fit relevance and confidence thresholds on labeled data; gold sets for every upgrade, not only React and Pydantic.
- **Closing gaps**: an open question should trigger targeted retrieval, not only a report entry.
- **Conflicts**: semantic matching across differently named subjects, and evolving requirements across patches.

## 10. Downstream consumption

`final_output.json` follows a published JSON Schema. An assessment engine evaluates each condition against a
codebase or deployment: `category`, `operator` and `expected_value` say what to check, and `search_patterns`
give strings or regexes to scan for. A path applies when its required conditions hold, at least one alternative
holds, and no disqualifying condition holds. `risk_level` and `confidence` rank and filter, `migration_steps`
become remediation tasks, and every claim links to its source quote for human review, also in the HTML report.
