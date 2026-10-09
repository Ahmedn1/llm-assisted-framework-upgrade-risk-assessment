# Upgrade risk pipeline

Run all six example inputs with the default workflow (d1 runs locally, so use `.venv-d1`):

```bash
PYTHONPATH=src .venv-d1/bin/python -m upgrade_pipeline \
  --inputs examples/inputs.json \
  --output-dir outputs
```

Run a single project:

```bash
PYTHONPATH=src .venv-d1/bin/python -m upgrade_pipeline \
  --project PostgreSQL --current-version 13 --target-version 16 \
  --output-dir outputs/postgresql-pipeline
```

`python -m upgrade_pipeline` and the installed `upgrade-pipeline` command are the same CLI. Without a command
it runs the pipeline; commands re-run single steps on saved case directories:

| Command | Does |
| --- | --- |
| `run` (default) | Resolve, collect, then relevance, extraction, verification and the final output |
| `normalize DIR…` | Rebuild `source_inventory.json` and `candidate_sections.json` |
| `relevance DIR…` | Score candidate sections with the relevance decision model |
| `extract DIR…` | Extract risk paths |
| `verify DIR…` | Verify the risk model and write the evidence summary |
| `final DIR…` | Rebuild `final_output.json` |
| `report DIR…` | Rebuild `report.html`; with several directories, also an `index.html` |
| `evaluate GOLD DIR…` | Check risk models against a gold expectations file |
| `schemas [--check]` | Regenerate, or check, `schemas/*.schema.json` |

Every command logs progress to stderr as `HH:MM:SS step | message` (steps: `run`, `case`, `collect`,
`normalize`, `relevance`, `extract`, `verify`, `final`, `models`, `research`). Counted steps log their first
item, about every tenth and the last, with elapsed seconds. `--log-level debug|info|warning` or `-q` sets the
level for any command; stdout keeps one JSON summary line per case.

By default the CLI resolves and collects with the heuristic path (no LLM calls), then scores the collected
sections with the local d1 decision model, extracts risks with the LLM and verifies them with d1. Each step
can be skipped with `--no-section-relevance`, `--no-extract-risks` or `--no-verify-risks`. An optional
`--path llm` performs resolution and collection through iterative LLM research; see
[the LLM path](llm-pipeline.md). `--scorer decision` scores version sources in the deterministic resolver
with the scorer decision model; it is rejected with `--path llm`. Which model each step uses, and how to set a
different one per step, is described in [models and default workflow](configuration.md). The resolved
settings (never API keys) are printed at start and recorded in each manifest under `configuration.models`.

From Python, `upgrade_pipeline.run_case` loads no models itself: pass `scorer='decision'` with a loaded
`decision_backend` for source scoring, and a `postprocess=PostprocessConfig(...)` holding the relevance backend,
extraction LLM and verification judge for the later steps (`upgrade_pipeline.postprocess`). Without it, only
resolution, collection and normalization run. Inputs can optionally include `source_urls` and `version_mode`;
the example run supplies only project name and version endpoints.

For each case, the pipeline saves `resolution.json` before collection, then
`evidence.json` with `documents_by_version`, and `manifest.json` with configuration
and stage outcomes. Either path then writes two deterministic, path-independent views:

- `source_inventory.json`: the source inventory (`source_id`, `title`, `url`,
  `source_type`, `retrieved_at`, `why_relevant`), ranked by trust tier and document type.
  Trust tiers (`official`, `probable_official`, `official_repository_discussion`,
  `third_party`) come from the resolver identity or the selected registry record, never
  from a model. Documents with no version association, and third-party pages reached only by
  link-following, are listed under `excluded_sources` with a reason. `source_id` equals the
  evidence document id.
- `candidate_sections.json`: sections nominated for risk extraction, tagged with keyword
  topics (`breaking_change`, `deprecation`, `removal`, `changed_default`, `behavior_change`,
  `runtime_requirement`, `dependency_requirement`, `configuration_change`, `migration_step`,
  `data_migration`, `api_change`, `known_issue`, `security`; untagged sections of release
  notes and guides are `unclassified_change`). A section takes its versions from the nearest
  version heading above it; sections under out-of-range releases are dropped. Exact repeated
  passages point at the copy in the most trusted source via `duplicate_of`. Topics are
  recall-oriented signals, not risk claims. Per-patch registry records repeat the same metadata, so only
  the records of documentation milestones and the latest version feed candidates
  (`patch_registry_records_skipped` counts the rest); all records stay in the inventory.

Section relevance (on by default, either path) then asks the relevance decision model to triage each
non-duplicate candidate section,
most trusted sources first, up to `--section-relevance-max-sections` calls per case
(default 200). One request per section answers three native questions:

| Question | Type | Use |
| --- | --- | --- |
| `risk_relevance` | Score 0–4 | From "unrelated or out of interval" to "explicit breaking change, removal, changed default, required migration or minimum requirement" |
| `risk_type` | Choice | The output contract's risk types plus `not_a_risk` (useful context) and `unknown` |
| `checkable_condition` | Noul | Whether the section names an API, setting, version, flag or behavior that a codebase or deployment could be checked against |

Each section gains a `relevance` object: `label` (`risk_candidate` at score ≥ 2.5,
`context` at ≥ 1.5, else `not_relevant`), the score, risk type, probabilities, model and
request id. Duplicates inherit their original's result; failed or over-budget sections
are marked `failed` / `not_scored`. Labels never remove or rewrite sections, and the
thresholds are uncalibrated policy choices; `relevance_scoring` in
`candidate_sections.json` records the rubric, thresholds and counts. The manifest summary
adds `section_relevance_*` counts. The step works with `--path llm`. Saved case
directories can be scored later with
`PYTHONPATH=src .venv-d1/bin/python -m upgrade_pipeline relevance outputs/<run>/01-react`
(add `--decision-backend jev` to use the remote API from `.venv`).

Risk extraction (on by default, either path) then runs [risk extraction](risk-extraction.md) with the
extraction LLM and writes `risk_model.json` and `extracted_facts.json`. When section relevance ran, only
sections the decision model judged relevant are extracted.
`--extract-max-sections` (default 150) bounds the sections per case. Like the other derived steps,
an extraction error is recorded in the manifest (`risk_extraction_error`) without failing the case.

Verification (on by default whenever extraction runs) then runs [verification](verification.md): it re-checks
evidence against the collected documents, asks the `--verify-judge` judge (default `decision`, the judge
decision model; also `llm` or `none`) whether each condition is supported, detects source disagreements, assigns final confidence, and writes
`evidence_summary.json` and `grounding_report.json`.

Whenever extraction ran, the case also gets `final_output.json`, the per-upgrade deliverable in the
final output shape: `project`, `current_version`, `target_version`, `summary`, `source_inventory`,
`risk_paths`, `open_questions` and `overall_confidence`, plus `contextual_facts`, the embedded
`evidence_summary` and `provenance`. It is built from the verified model when verification ran (otherwise
`provenance.verified` is false and confidence is preliminary). Paths are ordered by risk level, then
confidence. Conditions keep the contract's fields plus `condition_role`, `reasoning`, `search_patterns` and a
compact `verification` (grounding, entailment verdict, independent sources, conflict ids). Evidence keeps
source, type, quote, relevance, URL, versions and trust tier; internal fact and section ids stay in the working
files. The summary is composed by code from the validated model. The file is validated against
[`schemas/final_output.schema.json`](../schemas/final_output.schema.json) before writing. Batch runs also copy
each upgrade's file to `<output-dir>/final/<project>_<current>_to_<target>.json`.

Next to it the pipeline writes `report.html`, an interactive page for people: summary and counts, risk paths
with search and filters (level, type, confidence, surface), each condition with its role, confidence,
verification status, verbatim quotes linked to their sources and copyable code search patterns, migration and
validation steps, the evidence summary with source disagreements, the source inventory, contextual facts and
open questions. The page embeds the final output and makes no network requests; page text is inserted as text,
never as HTML. Batch runs copy each report to `final/<…>.html` and write `final/index.html` linking them. Rebuild it from a saved risk
model with `PYTHONPATH=src .venv/bin/python -m upgrade_pipeline final outputs/<run>/01-react`.

Saved case directories can be re-normalized without re-collecting:
`PYTHONPATH=src .venv/bin/python -m upgrade_pipeline normalize outputs/<run>/01-react`.
A normalization error is recorded in the manifest and does not fail the case.
`summary.json` is updated after each completed case. A stage
failure is recorded without preventing other projects from running. No source is
chosen automatically when resolution is ambiguous/unresolved.

A `partial` resolver result can proceed when it has a selected catalog and versions.
Collection remains explicitly partial. A successful process exit means every case
produced some version-associated candidate evidence, not that discovery or applicability was proven complete.
A run that collects documents but cannot associate any with selected versions is
`collected_unassociated` and the batch exits with code 2.

Default per-case budgets: 100 resolver HTTP requests; 200 collector HTTP requests;
100 collected documents, plus up to 60 per-version registry records with their own budget; 500 discovered
URLs; traversal depth two; 200 relevance calls; 150 extracted sections; 300 judged conditions. These are
separate budgets, and the manifest records them. `--offline` replays cache; `--refresh` revalidates
cached responses where supported. Without either flag, cached responses are reused and
missing responses fetched. This mode is not a fresh-as-of-today crawl.

The summary separates candidate evidence from direct release-record/registry
associations and calls out versions with registry metadata only. High document counts
or version mention coverage do not establish useful migration guidance. See each
case's failures, pending URLs, document types, and unreviewed associations.

The published results of the six example upgrades are in `outputs/` (see the README's Findings section);
each case's `evidence.json` is not committed because of its size.
