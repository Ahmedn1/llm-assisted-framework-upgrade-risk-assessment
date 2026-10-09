# Upgrade risk extraction

Turns `candidate_sections.json` from either collection path into `risk_model.json`, a set of upgrade
risk paths in the output contract's schema. Every condition cites verified evidence. The model cites IDs;
code copies quotes, source types and URLs from verified records, so the model cannot invent evidence.

```bash
# Part of the default workflow (relevance with d1, then extraction); d1 needs .venv-d1
PYTHONPATH=src .venv-d1/bin/python -m upgrade_pipeline --project React --current-version 17 --target-version 18 \
  --output-dir outputs/react

# On saved case directories
PYTHONPATH=src .venv/bin/python -m upgrade_pipeline extract outputs/react-llm/01-react
```

Extraction uses the extraction LLM stage (`LLM_EXTRACTION_*`, falling back to `LLM_MODEL_NAME`,
`LLM_BASE_URL`, `LLM_API_KEY`; see [configuration](configuration.md)) with strict JSON-schema output. Any
OpenAI-compatible endpoint works, including a locally served model; see [local LLMs](configuration.md#local-llms). `--structured-mode`, `--max-output-tokens` and `--token-parameter` apply.

## Stages

| Stage | Kind | Work |
| --- | --- | --- |
| Select | Code | Non-duplicate candidate sections. When section relevance was scored, `not_relevant` sections are excluded; `risk_candidate` sections go first, then `context`. Sections the decision model failed to score are kept, last, and counted in `selected_without_relevance_verdict`. Budget: `--extract-max-sections` (default 150). Overflow becomes an open question. |
| A. Facts | LLM, per batch of up to 8 sections / 6,000 characters from one source | Atomic change facts: subject, change kind, old and new value, versions, a verbatim quote, `is_risk`, `known_incompatibility` and certainty (`explicit`, `implied`, `speculative`). The schema restricts `candidate_id` to the batch and `versions` to the resolved interval. |
| A′. Verify | Code | The quote must be a contiguous passage of the cited section (whitespace and Markdown markers ignored). Versions must agree with the section's version heading. Failures go to `rejected_facts` with a reason and never reach a path. |
| B. Group | Code, then LLM | Code clusters facts sharing a normalized subject name; the LLM merges clusters that name the same change differently (`render`, `ReactDOM.render`). Code enforces a partition. Over 150 clusters are merged in chunks; a failed call keeps the code clusters. |
| C. Paths | LLM, one call per group | Risk paths with conditions (usually a target-version condition plus an application or deployment condition), roles, reasoning, ambiguities, documented migration steps, suggested validation actions and code search patterns. Conditions and steps may cite only that group's fact IDs. A group of context only returns no path and a reason. |
| D. Assemble | Code | Stable IDs, evidence copied from facts, severity caps (below), a preliminary confidence rubric, structural warnings, open questions, summary and overall confidence. |

## What counts as a risk

The prompts make these facts context (`is_risk: false`) unless the text says applications depend on the old
behavior: relaxations (something that used to error or warn is now allowed, including removed development-only
warnings), bug fixes and security fixes shipped by the upgrade, announcements and new optional features. A
known incompatibility (a platform, tool or library not yet supported, or advice to stay on the old version) is
a separate flag, so dropped support can be both `removed` and an incompatibility.

Code then caps what the model can claim, and records each cap in `structural_warnings`:

| Cited facts | Maximum `risk_level` |
| --- | --- |
| Only from sections marked experimental, alpha, canary or unstable | low |
| Only bug fixes ("Fix …") | low |
| Only security fixes (`security_fix`, CVE identifiers, "potential SQL injection/denial of service") | low |
| None explicit (implied or speculative only) | medium |

## Output

`risk_model.json` follows the final output shape (`project`, `current_version`, `target_version`,
`summary`, `source_inventory`, `risk_paths`, `open_questions`, `overall_confidence`) with these additions:

- `condition_role`: `required`, `optional`, `alternative`, `disqualifying` or `uncertain`. The
  output contract asks for this distinction; its example's boolean `required` is kept and equals
  `condition_role == "required"`.
- `reasoning` and `search_patterns` per condition. Search patterns are literal strings or regexes for a
  downstream codebase scan.
- Evidence items add `fact_id`, `candidate_id`, `url`, `trust_tier`, `versions` and `certainty`.
- `migration_steps` per path: documented migration actions, each with evidence; `suggested_actions` holds
  validation steps (search, test).
- `change_kinds` per path, computed by code from the cited facts (`removed`, `deprecated`, `migration_step`,
  …), including `known_issue` when any cited fact is a known incompatibility.
- `structural_warnings` per path: a missing target-version condition, no application or deployment
  condition, or a severity cap.
- `contextual_facts`: useful facts that are not risks, kept apart from risk paths.
- `extraction`: models, call count, token usage, the selection record, rejected facts and failures.

Before `risk_model.json` is written, the whole model is validated against `RiskModelOut`, including
values computed by code rather than chosen by the model. `risk_type`, `risk_level`, `affected_surface`,
condition `category`, `operator` and `confidence`, and `overall_confidence` must take exactly the
output contract's allowed values; `required` must be a boolean; every condition and contextual fact needs at least
one evidence item. The model-facing drafts use the same value lists in their structured-output schemas. A
validation failure is recorded as `risk_extraction_error` and nothing is written. The JSON Schema is
committed at [`schemas/risk_model.schema.json`](../schemas/risk_model.schema.json); a test fails if it drifts
from the code. Regenerate both schemas with:

```bash
.venv/bin/upgrade-pipeline schemas          # regenerate schemas/*.schema.json
.venv/bin/upgrade-pipeline schemas --check  # exit 1 if they drifted from the code
```

`extracted_facts.json` is the intermediate output: accepted facts, rejected facts with reasons, groups
and stage failures.

Preliminary confidence (refined in verification): **high** when every cited fact is explicit and one comes from an
official source; **low** when the role is uncertain, all facts are speculative, or only third-party sources
support it; otherwise **medium**.

## Failure handling

A failed model call never discards other work. A failed fact batch skips those sections, a failed grouping
falls back to the code-only grouping, and a failed path call leaves its facts unpathed. Each failure is
recorded and becomes an open question, and `overall_confidence` cannot be `high` when any stage failed.
Collector gaps (versions without evidence, budget exhaustion) are carried into `open_questions`.
