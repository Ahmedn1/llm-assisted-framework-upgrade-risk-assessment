# LLM-directed resolution and evidence collection

This is an alternative to the deterministic resolver/collector, not another source
scorer. The LLM decides what to search, which pages to fetch, which catalog to select,
which observed versions to propose, and which documents support each version.
The deterministic path remains the default.

## Run

Configure an OpenAI-compatible chat endpoint explicitly, hosted or local (vLLM, llama.cpp's `llama-server`,
Ollama, LM Studio; see [local LLMs](configuration.md#local-llms)). For example, for an already-running local
server:

```bash
export LLM_BASE_URL=http://localhost:8000/v1
export LLM_MODEL_NAME=your-served-model-name
# Set LLM_API_KEY if your endpoint requires authentication.
# LLM_COLLECTION_* overrides these for the LLM path only; see configuration.md.

PYTHONPATH=src .venv-d1/bin/python -m upgrade_pipeline \
  --path llm \
  --project React --current-version 17 --target-version 18 \
  --output-dir outputs/react-llm \
  --llm-max-steps 12 \
  --token-parameter max_tokens
```

After collection, the same post-collection steps as the deterministic path run (relevance with d1, risk
extraction, verification, final output), so the default command needs `.venv-d1`. Add
`--no-section-relevance --no-verify-risks` to run from `.venv` without d1, or `--no-extract-risks` to stop
after collection.

The same path supports `--inputs examples/inputs.json`. No model is downloaded or
started automatically. Use a generative chat model that can produce structured
JSON; native decision-only clients (Laya/Jev/d1 adapters) cannot plan this workflow.
`--structured-mode json_schema` is the default. Select `json_object` or `prompt`
explicitly if your server lacks JSON Schema decoding; all modes still validate
responses locally. `--max-output-tokens` defaults to 8192 per call.

## Workflow

1. The resolution stage receives the three inputs and optional source URLs.
2. The model emits a structured `search`, `fetch`, or `finish` action. Actions are
   executed by the application, not by generated code or a shell.
3. Search uses the shared Brave/DuckDuckGo function. Fetch uses the existing public
   HTTP transport, cache, rate deferral, HTML/Markdown parsers, and robots checks.
   GitHub release/issue/file URLs have API adapters. Registry inventories are
   explicitly projected to release labels to reduce context size.
4. Resolution finish proposes version labels with exact quotes from fetched text.
   Code validates the citations, parses numeric stable versions, filters the
   requested interval, sorts/deduplicates versions, and checks observed endpoints.
   No heuristic resolver or source-score fallback runs behind the agent.
5. The collection stage receives the accepted versions and can search/fetch again.
   It proposes documents, classifications, relevance explanations, supporting
   quotes, and explicit version associations. Shared documents appear under every
   version the model associates them with.
6. Invalid citations/actions become observations for a subsequent correction,
   within the same call budget. Unresolved resolution blocks collection.

The default family interval is identical to the deterministic path. Python inputs
can specify `version_mode='exact'`. Current-family changes are not included in
family mode. Inputs still need only project/current/target; URL hints are optional.

## Budgets and artifacts

- At most 12 model calls per stage by default (24 per case if both run).
- Separate resolver and collector network request budgets, default 100 and 200.
- At most 100 selected documents and 500 fetched sources by default.
- Fetch exposes 12,000 characters and 40 links at a time, with offsets for further
  reads, and a `version_outline` giving the offsets of in-interval version headings in long
  changelogs. The prompt includes the latest four observations, plus the two most recent fetched
  pages if rejected actions pushed them out, and a fetched-source index; source bodies and previous
  actions remain in artifacts.
- Model transport retries may produce additional HTTP attempts. `llm_calls` counts
  logical completion calls; trace entries preserve provider usage and request IDs.

Outputs retain `resolution.json`, `evidence.json`, `manifest.json`, and batch
`summary.json`, followed by the shared post-collection outputs (`source_inventory.json` through
`final_output.json`; see [the pipeline](pipeline.md)). Additional `agent-trace.json` and `research-sources.json` record the
structured actions, observed text, errors, model usage, source hashes and full
extracted documents. The trace is checkpointed after each action.

`--web-search` controls both LLM stages. `--evidence-search` and `--max-depth` apply
only to the deterministic collector. The LLM path uses action/source budgets
instead of an automatic crawl depth. `--refresh` controls source-cache refresh,
not inference caching. The CLI rejects `--offline --path llm` to avoid unexpected
inference calls; Python callers can explicitly inject a local/mock `llm_client`
with offline source retrieval. Missing robots policy in offline cache blocks that
site rather than bypassing the check.

## Interpretation and limitations

Results remain **partial**. A quoted version number is not proof of a published
release or exhaustive enumeration. A verbatim quote does not prove the model's
relevance interpretation, publisher authority, or applicability to an actual
application. These claims are explicitly unreviewed. Search snippets cannot be
used as evidence; citations must match text returned by a fetch action.

Model citations use `llm_cited_relevance`, so they do not inflate the deterministic
summary's direct release-record evidence count. Document bodies can include
out-of-range sections: the model's quote/association identifies the proposed
relevant material; downstream extraction must retain that scope.

Fetched content is treated as untrusted data in the system prompt; it cannot
introduce new execution tools. This does not guarantee that a model will resist
semantic prompt injection. There is no browser rendering, PDF extraction, or
video transcription. Robots for redirect destinations are checked after the
transport follows the redirect. GitHub blob URLs with slash-containing refs need
an explicit contents-API URL. Huge inventories may require more reads/calls; budget
exhaustion never upgrades an incomplete result to complete.

Tests use scripted model responses through the real structured-output client and
mocked public retrieval. They validate plumbing and failure handling, not model
quality. No real-model evaluation is implied. Compare this path against the
existing deterministic baseline before deciding whether its cost is justified.

## Completion gates and failure reporting

A failed web search disables search for the rest of that case, including the next
stage. Structured decoding then permits only fetch/finish, and the model receives
an explicit instruction to retrieve official registries or release indexes
directly. This prevents query rephrasing from repeatedly hitting the same challenge.

Resolution requires a recognized inventory: npm/PyPI release records, a GitHub
releases API list, or multiple version-labeled links to release pages in an HTML
index. A migration-guide heading cannot stand in for an inventory. Every proposed
version must belong to the selected inventory, and all observed in-range records
must be included. This is completeness relative to the fetched inventory, not
proof that every published release or every pagination page has been retrieved.
Registry projections exclude unrelated version families and prereleases. GitHub
release lists are projected to labels and record URLs; fetch individual records
for their full notes. Pagination links are exposed to the agent.

Collection citations must occur in extracted body sections and cannot consist
only of titles/headings. Exact quote matching remains enforced. Reports separate
`retrieval_failures`, `validation_failures`, and `inference_failures`, combining
both stages. `versions_with_llm_cited_evidence` is reported separately from direct
release-record evidence. A collection with documents but missing release-note
evidence for resolved versions is `insufficient_evidence`, rather than a successful
`completed_partial`; the CLI exits with code 2. These rules do not prove semantic
relevance or authority, which remain explicitly unreviewed.

## Separate stage contracts

Resolution and collection use separate system prompts and Pydantic schemas.
`ResolutionAction` has catalog selection and version claims, but no documents;
`CollectionAction` has documents, but no catalog-selection or version-enumeration
fields. Each also has a fetch/finish-only variant when search is unavailable.

Collection starts with fresh recent-action history, an explicit per-version
release-note and target-upgrade-guide checklist, and an index of previously
fetched sources, their links and observed excerpts. Previously fetched sources
remain citable; no redundant fetch is required if their body evidence is usable.

An empty collection with a gap explanation is rejected while more than one model
action remains and the HTTP request budget is not exhausted. The corrective
observation directs the model to fetch release notes/guides or reuse fetched body
text. An empty finish remains possible on the last action or after exhaustion of
the HTTP request budget, so blocked research is reported without inventing evidence.
A nonempty result still passes the existing citation and coverage checks; a single
guide is not automatically sufficient release-note coverage.

## Incremental evidence and citation repair

Collection schemas enumerate the exact resolved version labels, so a major label
such as `18` is not an allowed substitute for `18.0.0` or `18.3.1`. Shared guidance
must explicitly list the intended resolved versions.

Document proposals are validated independently on every action, including fetch
and search actions. Accepted claims are retained when another claim fails, a
subsequent retrieval fails, or the action budget expires. Rejection feedback names
the claim index and source. The next prompt includes accepted evidence so the
model can focus on missing material rather than reconstructing the whole result.

A document may have multiple roles, stored in `document_types` and per-claim
`relevance_evidence[].document_type`. The original `document_type` is retained as
the first accepted role for compatibility. Coverage is computed per claim/version;
a release-note claim for one version does not turn every other association on the
same document into release-note evidence.

As in the deterministic collector, a cited document whose extracted text hash matches
an already accepted document (a mirror, or raw vs. rendered copy) records
`duplicate_content_of`, keeps its own URL and claims, and stores text/sections only on
the original; `documents_by_version` materializes the original content.

Citation matching tolerates whitespace differences only. The words must still
occur contiguously in observed source text; invented wording, concatenated
nonadjacent passages, unseen text and title- or heading-only evidence remain
rejected. A quote may lead with its section headings (e.g. a changelog version
heading) when the rest is body text. A release-note claim must name each of its
versions in the quote, its nearest version heading, or the page title. A quote found
verbatim in a different observed source is reattributed and records
`cited_source_corrected_from`. Submitted quote text and the matching policy are
retained for audit.

An offline replay of the saved React research actions retained
two documents instead of zero under the revised citation rules (an offline replay of the original
actions, not new inference). A subsequent live React run with `gpt-5.4-mini` reached
`completed_partial`, with release-note evidence for all five resolved 18.x versions from the
React 18 release post, the upgrade guide and `CHANGELOG.md`. Collection also rejects a finish while
budget remains and some resolved version lacks release-note evidence.
