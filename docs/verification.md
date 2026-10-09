# Evidence grounding and verification

Verifies an extracted `risk_model.json`, then writes the per-upgrade evidence summary. Nothing
is deleted. Weak or disputed conditions are downgraded (`condition_role: uncertain`, `confidence: low`) and
their paths listed under `low_confidence_or_disputed_risks` with reasons.

```bash
# Part of the default workflow (after extraction); d1 needs .venv-d1
PYTHONPATH=src .venv-d1/bin/python -m upgrade_pipeline --project React --current-version 17 --target-version 18 \
  --output-dir outputs/react

# On saved case directories (risk_model.json, extracted_facts.json, evidence.json, ...)
PYTHONPATH=src .venv-d1/bin/python -m upgrade_pipeline verify outputs/react-llm/01-react
```

`--verify-judge` (`--judge` in the standalone command) chooses the entailment judge:

| Judge | Model |
| --- | --- |
| `decision` (default) | Judge decision stage (`DECISION_JUDGE_*`, falling back to `DECISION_*`; default local LiquidAI d1-3B). Shares the loaded model with section relevance when settings match; works offline from the cache. |
| `d1`, `jev` | Shorthands that force that decision backend for the judge |
| `llm` | Second-pass critic with the judge LLM stage (`LLM_JUDGE_*`, falling back to `LLM_*`); any OpenAI-compatible endpoint, including a [local server](configuration.md#local-llms) |
| `none` | Deterministic checks only; confidence capped at medium |

## Steps

| Step | Kind | Work |
| --- | --- | --- |
| 1. Grounding | Code | Re-checks every evidence item against the original document in `evidence.json`, not the extractor's copy: the source is in the inventory, the quote occurs, versions are in the resolved interval, and the fact exists. Conditions are `grounded`, `partially_grounded` or `ungrounded`. |
| 2. Entailment | Judge | One call per condition (budget `--verify-max-conditions`, default 300) with the statement and up to three quotes: `supports`, `partially_supports`, `contradicts` or `unrelated`, plus whether the statement adds detail absent from the quotes. For application conditions the judge assesses whether the documentation establishes that such usage is affected. |
| 3. Corroboration | Code | Independent sources per condition, including exact copies found during normalization, and the best trust tier. |
| 4. Conflicts | Code | Facts about the same subject from different sources that disagree on severity (removed vs. deprecated), versions (both scoped by version headings, no overlap), value (different replacement or minimum) or risk vs. context. Resolved by trust tier, then specificity (version heading over whole document), then recency. A tie is `unresolved`. |
| 5. Confidence | Code | Final rubric below. A path is as strong as its weakest required or alternative condition. |
| 6. Summary | Code | `evidence_summary.json` in the output contract's shape, also embedded in `risk_model.json`. |

## Confidence rubric

| Level | Rule |
| --- | --- |
| high | Grounded; judge `supports` without unsupported detail; every cited fact explicit; an official source; no lost or unresolved conflict |
| medium | Grounded and not disputed, but some high requirement is missing: no judge, partial support, implied facts, probable-official or discussion sources, partial grounding, or a conflict it won |
| low | Ungrounded; judge `contradicts` or `unrelated`; marked uncertain at extraction; only speculative facts; only third-party sources; the cited source lost a conflict; or an unresolved conflict |

Ungrounded or contradicted conditions also change role to `uncertain` and `required: false`. The original
role and confidence are kept in `verification.preliminary_role` and `preliminary_confidence`, and
re-verification starts from them, so running it twice gives the same result.

## Open questions and diagnostics

`open_questions` holds unknowns about the upgrade itself: versions with no evidence collected, documentation
milestones (e.g. 4.0, 4.1, 4.2) with no release-note sections, unresolved source disagreements, conditions the
evidence does not confirm, and one question summarizing the critical or high paths backed by a single source (each
such path is flagged `single_source_severe_risk`). Patch releases without sections of their own are normal and are
not listed. Outputs saved before this split are sorted the same way when `final` or `report` rebuilds them.

`diagnostics` describes how the run went and is kept separate: sections over the extraction budget or without a
relevance verdict, rejected facts, failed model calls, collector gaps, no judge or judge failures, and contextual
facts whose evidence fails grounding. Both appear in `final_output.json`; the report shows diagnostics in a
collapsed section.

## Outputs

- `risk_model.json`: conditions gain `verification` (grounding, entailment, corroborating sources, conflicts,
  reasons); paths gain `confidence` and `verification_flags` (`ungrounded_condition`,
  `judge_disputed_condition`, `source_disagreement`, `single_source_severe_risk`, ...).
- `evidence_summary.json`: `project`, `upgrade` (e.g. `17_to_18`) and `evidence_summary` with
  `high_confidence_risks`, `medium_confidence_risks`, `low_confidence_or_disputed_risks`,
  `sources_with_disagreements` and `open_questions`.
- `grounding_report.json`: every check per condition, the judge verdict, confidence before and after, all
  conflicts, and judge failures.

## Known limits

Judge verdicts are uncalibrated. On a smoke test, d1 graded application conditions `partially_supports` with
choice confidence near 0.45, so few application conditions reach high. Conflict detection groups facts by
normalized subject name, so differently named mentions of the same API are not compared. Successive patch
releases that legitimately raise the same requirement (e.g. a dependency minimum raised in 4.2.19 and again in
4.2.28) are reported as `versions` or `value` disagreements, because the detector cannot tell an evolving
requirement from a contradiction; feeding only milestone and latest registry records to extraction removed most
of these on Django.
