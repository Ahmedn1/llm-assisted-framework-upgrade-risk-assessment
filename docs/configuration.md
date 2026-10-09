# Models and default workflow

## Default workflow

With no step flags, `python -m upgrade_pipeline` runs:

1. **Heuristic resolver and collector** (`--path deterministic`, `--scorer heuristic`).
2. **Section relevance** of the collected evidence with the **decision model** (local d1 by default).
3. **Risk extraction** with the **LLM**.
4. **Verification** with the **decision model** as entailment judge, then the final output.

| Step | Default | Disable | Model stage |
| --- | --- | --- | --- |
| Section relevance | on | `--no-section-relevance` | decision `relevance` |
| Risk extraction | on (off with `--offline`) | `--no-extract-risks` | LLM `extraction` |
| Verification | on whenever extraction runs | `--no-verify-risks` | decision `judge` (`--verify-judge decision`), or LLM `judge` (`--verify-judge llm`) |
| Resolver source scoring | off (`--scorer heuristic`) | — | decision `scorer` with `--scorer decision` |
| LLM-path collection | off (`--path deterministic`) | — | LLM `collection` with `--path llm` |

With `--offline`, steps that need a remote model and were only on by default are skipped with a notice;
asking for them explicitly is an error. The local d1 model loads from the cache offline. If a default step
has no usable model (for example no LLM is configured), the run stops before collecting and names the
variables to set and the `--no-...` flag that skips the step. The default workflow loads d1, so run it with
`.venv-d1/bin/python` (or choose `DECISION_BACKEND=jev`).

## Environment variables

Every model kind has a generic group and one group per stage. Stage values override generic ones.

| Group | Variables | Used by |
| --- | --- | --- |
| LLM, generic | `LLM_MODEL_NAME`, `LLM_BASE_URL`, `LLM_API_KEY` | every LLM stage without its own values |
| LLM, per stage | `LLM_COLLECTION_*`, `LLM_EXTRACTION_*`, `LLM_JUDGE_*` (same three suffixes) | LLM-path collection; risk extraction; LLM critic judge |
| Decision, generic | `DECISION_BACKEND` (`d1` or `jev`), `DECISION_MODEL_NAME`, `DECISION_MODEL_REVISION`, `DECISION_BASE_URL`, `DECISION_API_KEY` | every decision stage without its own values |
| Decision, per stage | `DECISION_SCORER_*`, `DECISION_RELEVANCE_*`, `DECISION_JUDGE_*` (same five suffixes) | resolver source scoring; section relevance; verification judge |

Decision defaults: backend `d1`, model `LiquidAI/d1-3B` pinned to a reviewed revision, loaded locally on
CUDA. A configured base URL without an explicit backend selects `jev` (remote decision API). A d1-style model
other than the default must be pinned with a `*_MODEL_REVISION`, because loading it runs code from its
repository.

`LLM_MODEL` and `DECISION_MODEL` are still accepted as deprecated aliases of `*_MODEL_NAME`. There is no
automatic `.env` loading; see [`.env.example`](../.env.example) for a template to `source`.

## CLI arguments

Each environment variable except API keys has an argument: generic `--llm-model`, `--llm-base-url`,
`--decision-backend`, `--decision-model`, `--decision-revision`, `--decision-base-url`, and per stage
`--<stage>-llm-model`, `--<stage>-llm-base-url` (stages `collection`, `extraction`, `judge`) and
`--<stage>-decision-backend`, `-model`, `-revision`, `-base-url` (stages `scorer`, `relevance`, `judge`). API
keys are environment-only so they never land in shell history or process listings. The step commands
(`upgrade-pipeline relevance`, `extract`, `verify`) accept the generic arguments and the
arguments of their own stage.

## Local LLMs

Every LLM step talks to an **OpenAI-compatible Chat Completions API** (`POST {base_url}/chat/completions`), so
a locally served model works the same way as a hosted one. Point the base URL at the local server; an API key
is optional and only sent if set:

```bash
# vLLM (default port 8000), llama.cpp's llama-server (8080), Ollama (11434) or LM Studio (1234) all expose /v1
export LLM_BASE_URL=http://localhost:8000/v1 LLM_MODEL_NAME=<served model name>
PYTHONPATH=src .venv-d1/bin/python -m upgrade_pipeline --project React --current-version 17 --target-version 18

# Or only one step, e.g. a local model for the verification critic while extraction stays hosted
export LLM_JUDGE_BASE_URL=http://localhost:8000/v1 LLM_JUDGE_MODEL_NAME=<served model name>
```

What the server and model need:

- **Structured output.** Requests use JSON Schema decoding (`response_format: json_schema`) by default. If the
  server does not support it, pass `--structured-mode json_object` (JSON mode with the schema in the prompt) or
  `--structured-mode prompt`. Every response is validated against the schema locally in all modes, so a malformed
  answer is rejected, never used.
- **Token parameter.** Older servers expect `max_tokens`: pass `--token-parameter max_tokens`.
  `--max-output-tokens` (default 8192) sets the limit.
- **Context window.** The largest request, grouping up to 150 change clusters, is about 10k input tokens; fact
  batches are about 3k. A context of at least 16k tokens is needed, and 32k is comfortable.
- **Capability.** The LLM must follow the extraction instructions and copy quotes verbatim. Smaller models tend to
  produce more rejected facts and failed calls; these are recorded as diagnostics and never become evidence, but
  coverage drops. The LLM steps have been validated with `gpt-5.4-mini`; tests exercise the same OpenAI-compatible
  client against a mocked server.

`--offline` turns off the LLM steps even when the server is local, because it promises zero network requests.
For a fully local run, collect with `--offline` from the cache (or online once), then run the LLM steps on the
saved case with `upgrade-pipeline extract DIR`, which reaches the local server, and `upgrade-pipeline verify DIR`
(local d1 judge by default; add `--judge llm` to use the local LLM as the critic).

The decision model is separate: it runs locally by default (d1 on CUDA) and is not an OpenAI-compatible chat model.

## Resolution rules

- **Precedence:** stage argument > stage environment variable > generic argument > generic environment
  variable > default. Stage-specific always beats generic; at the same level an argument beats the environment.
- **No borrowed credentials.** A stage that sets its own base URL, or a decision backend different from the
  generic one, is a different provider. Its model name and API key come only from that stage's group; the
  generic model and key are never sent to it. A separate endpoint without a stage model name is an error.
- **One instance per model.** Stages that resolve to identical settings share one client or loaded model, so
  the default workflow loads d1 once for relevance and verification.
- **Provenance.** The run prints the resolved model for each active stage to stderr, and each case's
  `manifest.json` records `configuration.models`: backend, model, revision, base URL, whether a key was set
  (never the key), and which argument or variable supplied each value.

## Examples

```bash
# One provider for everything, local d1 for decisions (the default workflow)
export LLM_MODEL_NAME=gpt-5.4-mini LLM_BASE_URL=https://api.openai.com/v1 LLM_API_KEY=...
PYTHONPATH=src .venv-d1/bin/python -m upgrade_pipeline --project React --current-version 17 --target-version 18

# A stronger model only for risk extraction, same provider and key
export LLM_EXTRACTION_MODEL_NAME=gpt-5.4

# A different provider for the LLM critic judge; it needs its own model and key
export LLM_JUDGE_BASE_URL=http://localhost:8000/v1 LLM_JUDGE_MODEL_NAME=local-critic
PYTHONPATH=src .venv-d1/bin/python -m upgrade_pipeline ... --verify-judge llm

# Remote decision API for resolver scoring only; relevance and verification stay on local d1
export DECISION_SCORER_BACKEND=jev DECISION_SCORER_MODEL_NAME=jev-latest \
       DECISION_SCORER_BASE_URL=https://api.typesafe.ai/v1 DECISION_SCORER_API_KEY=...
PYTHONPATH=src .venv-d1/bin/python -m upgrade_pipeline ... --scorer decision
```

The `upgrade-resolve` command (resolver only) uses the same scorer stage for `--scorer decision`: it accepts
the generic `--decision-*` and `--scorer-decision-*` arguments, reads `DECISION_SCORER_*` then `DECISION_*`, and
defaults to local d1 (which works with `--offline` from the cache).
