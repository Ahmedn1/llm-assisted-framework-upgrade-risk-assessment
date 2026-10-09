# Optional decision scoring of version sources

`--scorer heuristic` remains the default and makes no model calls. `--scorer decision`
replaces the heuristic identity gate and catalog preference with native decision scores from
the scorer decision model (default local d1; see [configuration](configuration.md)). Whether
decision scoring improves on the heuristic is an open evaluation question (below).

## Workflow

1. Run existing PyPI, npm, GitHub, optional seed URL, and web discovery. Web discovery
   visits the first five results returned by Brave or DuckDuckGo (or fewer if unavailable).
   Failures are recorded, not interpreted as absence of releases. `--web-search none`
   explicitly disables search.
2. Attempt catalog retrieval for every discovered candidate, including heuristic-rejected
   candidates, and linked homepages. There is no early stop after a successful registry.
   Existing per-provider adapters, pagination limits, HTML limits, and total HTTP request
   budget still apply. GitHub tags remain a fallback when its releases are insufficient.
   This is bounded discovery, not an exhaustive search of the web.
3. Deduplicate catalogs by URL and type. Send one decision request per catalog, or a
   failed-source record when retrieval fails. Both questions use a five-level rubric:
   exact project/component match, and authority as a published-version source.
   Inputs include provenance, available page excerpts, endpoint checks, and up to 20
   interval records. Heuristic scores are excluded. Source text is marked as untrusted.
4. Validate native answers and distributions. A source is eligible only if it has
   interval versions and both expected scores are at least 3 on the 0–4 scale.
   Project match is an eligibility gate; rank eligible sources by authority alone.
   Exact authority ties prefer endpoint coverage, detailed release granularity,
   enumeration completeness, then URL. Sources within 0.25 authority points from
   different/unlinked publishers yield `ambiguous` source selection, while
   project matching is reported separately. Repository/homepage associations
   remain provisional, not ownership proofs.
5. Parse and filter versions deterministically. Missing endpoints or bounded HTML
   enumeration keep the result `partial`, regardless of the model score. Tags retain
   `tags_only_unverified` publication coverage.

Thresholds and margins are initial policy choices, **not calibrated confidence cutoffs**.
A native model distribution is not proof that a source is correct. An evaluation set
should establish whether this mode improves on the heuristic baseline before adoption.
A source-scoring error is recorded; it never silently invokes the heuristic fallback.
Other successfully scored sources can still be selected.

## Choose the decision model

Both `upgrade-resolve --scorer decision` and the pipeline's `--scorer decision` use the scorer decision
stage: `DECISION_SCORER_*`, falling back to `DECISION_*`, default local d1 (run with `.venv-d1`). For a remote
TypeSafe-compatible backend set `DECISION_BACKEND=jev` (or a base URL), `DECISION_MODEL_NAME`,
`DECISION_BASE_URL` and optionally `DECISION_API_KEY`; see [configuration](configuration.md).
The API root must support the existing TypeSafe-compatible `/systemone` contract;
OpenAI chat compatibility alone is insufficient. No provider or model name is hardcoded.

```bash
# Local d1 (default): needs .venv-d1
PYTHONPATH=src .venv-d1/bin/python -m upgrade_resolver \
  --project PostgreSQL --current-version 13 --target-version 16 \
  --scorer decision --output outputs/postgresql-decision.json

# Remote decision API
DECISION_BACKEND=jev DECISION_MODEL_NAME=... DECISION_BASE_URL=... .venv/bin/upgrade-resolve \
  --project PostgreSQL --current-version 13 --target-version 16 --scorer decision
```

`--offline` works with local d1 (weights and discovery from the cache) and is rejected for the remote
backend. From Python, inject any already-loaded backend:

```python
from pathlib import Path
from inference import OpenD1Client
from upgrade_resolver.http import HttpClient
from upgrade_resolver.resolver import resolve

backend = OpenD1Client(loaded_model, model_name="your-loaded-model")
client = HttpClient(Path(".cache/upgrade-resolver"), offline=True)
try:
    result = resolve(client, "PostgreSQL", "13", "16",
                     scorer="decision", decision_backend=backend)
finally:
    client.close()
```

Any backend implementing `DecisionBackend.decide` can be injected. Model loading stays
outside the resolver and the independent `inference` package has no resolver dependency.

## Output and costs

The existing result fields remain. `scoring` adds the versioned rubric, thresholds,
per-source inputs, native model/backend identity, distributions, token usage, request ID,
rank score, eligibility, and failures. It does not fabricate a rationale. `llm_calls`
counts attempted logical decision calls (transport retries are not separate entries).
Model calls are not included in the source HTTP request count, and model responses are
not cached. Exhausting discovery's request budget can leave many catalogs unavailable.

Both modes emit only the requested interval in `catalogs[].releases`; endpoint
checks use the complete retrieved inventory. `catalog_coverage` reports endpoint
checks, granularity, counts and matching observed version sets. Agreement does not
establish ownership, completeness or independent corroboration. Source selection does not collect migration documents
or extract upgrade risks.

Tests use synthetic native responses and source fixtures. No real model quality or
calibration claim follows from those tests.

## Local GPU runner

`examples/local_decisions.py` loads revision
`051bcc464b01b9f92942b364d9586b0ef5912432` through the model's custom Transformers code.
Model files and dynamic modules are cached under `.cache/huggingface`. The code and
weights are pinned together, including the tokenizer loaded by the native engine.
The example requires CUDA with BF16 support and defaults to an 8,192-token maximum
per question. Over-limit inputs are rejected rather than silently truncated.

Create a separate environment with Python 3.12 and install the local inference
requirements (the normal resolver environment does not require Torch):

```bash
/usr/bin/python3 -m venv .venv-d1
.venv-d1/bin/python -m pip install -r requirements-d1.lock
.venv-d1/bin/python examples/local_decisions.py
```

The default test uses three **synthetic** cases: an official release archive, an
unofficial repository mirror, and an unrelated client package. These check actual
model execution and illustrative ranking; they are not a real-source accuracy benchmark.

For full source discovery and scoring:

```bash
.venv-d1/bin/python examples/local_decisions.py \
  --project PostgreSQL --current-version 13 --target-version 16 \
  --output outputs/postgresql-d1.json
```

`--offline` reuses cached weights and discovery responses. JSON includes the pinned
revision, dependency versions, device, model load time, sequential call times, and
Torch peak allocated/reserved GPU memory. The first call is cold: no compilation or
warmup is performed. Torch memory figures exclude allocations from other applications.

### First measured local run (2026-10-09)

Tested on the RTX 3080 Laptop GPU (16 GB), BF16, Torch 2.14.1+cu130,
Transformers 5.14.1, with the pinned revision above:

- Synthetic cases: official archive authority 3.642/4, mirror 1.936/4,
  unrelated client 0.127/4. Peak Torch allocation was 5.864 GiB.
  First call took 379 ms; subsequent short calls took 79–81 ms.
- Cached PostgreSQL 13 → 16 discovery: 46 model calls, peak Torch allocation
  6.277 GiB. Both decision mode and the heuristic returned `ambiguous`.
- The official release archive scored 3.460 for project match and 3.703 for
  authority. VersionLog scored 3.518 and 3.522. Our minimum-of-two ranking
  therefore put VersionLog slightly ahead, within the ambiguity margin.
  GitHub tags received authority 2.092, below the eligibility threshold.

The real-source result does not demonstrate an improvement over the heuristic.
Inspect provenance evidence, the two-score aggregation rule, and false authority
positives before adjusting cutoffs. Offline cache misses also limit catalog coverage.
The reports were generated locally and are not committed.

## Revised policy and Laya comparison

The heuristic now groups HTML pages on an exact host together. An exact identity
score tie uses the requested order: web-search discoveries, GitHub, registries,
then other sources. Ties remaining within the same preference still abstain.
This rule does not override unequal identity scores or establish ownership.
Source role/authority is evaluated separately after identity selection.

Discovery records observed project-site links to release archives in addition to
repository backlinks and registry links, excluding self-links. The model receives
these relationships under `source.provenance`, explicitly labeled as observations,
not ownership verification. Rubric `source-authority-v2` requires maintenance or
endorsement evidence for high authority scores, independently of project match.

HTML traversal skips version-labeled links outside the requested endpoint families,
prioritizes relevant release links above generic navigation, and retains the current
family for endpoint validation. Generic, unlabeled indexes remain eligible for
exploration. Bounded HTML enumeration still cannot claim complete coverage.

### Run Laya

The independent `inference.LayaClient` wraps an already-loaded Laya Agent. Native
probabilities and entropy-based confidence are preserved; additional answer fields
and truncation metadata are stored in `DecisionResponse.provider_metadata`. Neither
confidence values nor the existing thresholds are calibrated across models.

The runner pins the English model `convaiinnovations/laya` to
`7b928d828b7b0e022f929d9bd2e44165aa270148`, and the runtime in `requirements-d1.lock`
to commit `1adc59f7e371deb601fcfa18a14e25db238addcc` of the
[official Laya repository](https://github.com/NandhaKishorM/laya).
The [English checkpoint](https://huggingface.co/convaiinnovations/laya) uses a
512-token default. The adapter checks the runtime's actual tokenizer and question
budget and refuses truncated evidence or clipped rubrics instead of accepting
silently shortened inputs. This integration depends on the pinned runtime's
encoding helpers; runtime upgrades require rerunning adapter checks.

```bash
.venv-d1/bin/python examples/local_decisions.py --backend laya --compact \
  --project PostgreSQL --current-version 13 --target-version 16 --offline \
  --output outputs/postgresql-laya-v2.json
```

`--compact` is an explicit, lossy evidence projection, not a model-generated summary.
It retains task, URL/type, a short description/excerpt, up to two observed provenance
links, mirror indication and coverage. The exact projected input is recorded in each
measurement. The Laya adapter then checks that this entire projection and rubric fit.
Use the same flag for d1 to compare equal inputs; do not equate this with the older
full-evidence run. Laya's native default eager runtime is used, without fine-tuning
or threshold changes.

### Cached PostgreSQL 13 → 16 comparison

| Method | Result | Selected source | Versions |
| --- | --- | --- | --- |
| Revised heuristic | partial | Official PostgreSQL release archive | 61 |
| Laya, compact evidence | unresolved | None: no source clears both thresholds | 0 |
| d1-3B, same compact evidence | partial | Official PostgreSQL release archive | 61 |

| Source | Laya match / authority | d1 match / authority |
| --- | --- | --- |
| Official archive | 1.990 / 2.539 | 3.460 / 3.015 |
| VersionLog | 1.891 / 2.475 | 3.519 / 1.305 |
| GitHub tags | 1.519 / 2.109 | 3.372 / 2.242 |

Both models completed 46 calls without input truncation or validation errors.
Laya's median call took 59 ms with 1.61 GiB peak Torch allocation; d1's took
109 ms with 5.88 GiB. These are single cached runs, not throughput or quality benchmarks.
Laya's runtime warned that a shipped temperature for choices with 11+ options was
clamped; this test uses five-level score questions, not that affected category.

Laya does not pass this task's current eligibility policy out of the box. This alone
does not prove inferior general model quality. The official source's d1 authority
score is also close to the threshold. Keep the heuristic baseline and evaluate more
projects before tuning the rubric or thresholds. Offline cache misses limit available
catalog evidence in all of these model runs.
