# Version resolver

The first pipeline stage discovers a software project's identity and release catalogs, then enumerates the
versions between the requested endpoints. It takes the three upgrade inputs; source URLs are optional.

**There are no project-to-repository or project-to-registry mappings in the implementation. The default
heuristic mode makes no model calls.** Provider adapters and identity heuristics are generic, and an
unfamiliar synthetic project is covered by an end-to-end test. Optional [decision scoring](source-scoring.md)
replaces the heuristic source ranking with a decision model (`--scorer decision`).

## Run the resolver alone

```bash
.venv/bin/upgrade-resolve \
  --project Django \
  --current-version 3.2 \
  --target-version 4.2 \
  --output outputs/django.json
```

`python -m upgrade_resolver` is equivalent to `upgrade-resolve`. The optional
`requirements.lock` records the dependency versions used during validation; use
`pip install -r requirements.lock` before installing the local project to
reproduce those versions.

Optional hints supplement automatic discovery and are checked like other sources:

```bash
.venv/bin/upgrade-resolve \
  --project Django --current-version 3.2 --target-version 4.2 \
  --source-url https://docs.djangoproject.com/en/4.2/releases/
```

By default, JSON goes to stdout and a short status goes to stderr. Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Source selected; catalog enumerated; both endpoints found |
| 1 | Fatal retrieval/runtime error |
| 2 | Partial, ambiguous, or unresolved result; also invalid CLI arguments |

Read `coverage` as well as `status`: an enumerated tag catalog does **not** prove
that corresponding packages were published, or that all historical releases
remain in the source. Search itself is bounded, never exhaustive.

## Version interpretation

The default `--version-mode family` preserves the precision of the inputs:

- `3.2 → 4.2`: exclude all `3.2.x`; include discovered releases in `4.0.x`,
  `4.1.x`, and `4.2.x`.
- `17 → 18`: exclude all `17.x`; include discovered stable `18.x` releases.
- Releases are selected by version order, not publication date. Backports can
  be published after newer release families.

This is a documented policy for underspecified upgrade inputs, **not a claim
that patches or current-family changes cannot introduce risks**. For an actual
exact interval use:

```bash
.venv/bin/upgrade-resolve \
  --project Django --current-version 3.2.0 --target-version 4.2.0 \
  --version-mode exact
```

Exact mode selects `current < version <= target`. Family endpoints must have
equal precision. Neither mode silently replaces an endpoint with today's latest
patch. Target-family mode includes available target-family patches as observed
in the retrieved catalog, not an assumed historical snapshot.

Numeric stable versions are compared with `packaging.version.Version` after
strict parsing. This is **not** a universal SemVer/CalVer implementation. Stable
numeric tags such as `v1.2.3`, `release-1.2`, `REL_16_0`, and `<repo>@1.2.3` are
recognized. Prereleases, arbitrary version labels, and unavailable PyPI releases
are retained in `catalogs[].skipped_labels`; prerelease endpoints are rejected.
Yanked PyPI versions are retained and marked, not silently dropped.

`documentation_milestones` groups the selected versions to the input precision.
The full selected list remains in `versions`. Milestones are neither a complete
list of release-note pages nor an installation sequence.

## Discovery and trust

1. Look up name candidates in PyPI and npm, and search npm and GitHub. A generic
   `.js` naming convention also tries the suffix-free npm name; all such
   candidates must still pass identity checks.
2. Optionally search the web with a templated query using project name and both
   endpoints. `--web-search auto` uses the shared search function: Brave when a key is present, with
   DuckDuckGo HTML fallback if Brave is unavailable or no key is set. Bot challenges are recognized and recorded; API discovery
   continues. `--web-search none` uses provider discovery only.
3. Inspect high-scoring candidates' homepages, record release-archive and package links, and check
   repository backlinks. Resolve old repository names using GitHub API metadata.
4. Score explicit evidence: name match, repository name, homepage host match,
   description, and backlinks. Penalize forks, clients, SDKs, and companion
   packages. Popularity determines which GitHub candidates are retrieved, but
   does not contribute to their identity scores. Search snippets are discovery
   hints, not release evidence.
5. Group references to the same canonical repository and HTML pages on the same exact host. Never treat a common
   repository as proof that every package within it is the requested component.
6. Require a score of at least 6 and a margin of at least 3 over the next identity.
   For exact score ties, prefer web-search discoveries, then GitHub, registries,
   and other sources. Remaining ties return `ambiguous`. Search origin is a
   preference, not proof of authority.
7. Read catalogs for the selected identity and rank their source roles separately.
   Observed project-site links to release archives receive preference; published
   catalogs outrank source tags/mirrors. Equal authority uses the source preference
   above, followed by endpoint coverage and granularity. HTML crawling prioritizes
   the requested interval and skips identified out-of-range release links.
   Record partial enumeration explicitly.
8. Check that the requested endpoints occur in the selected catalog; parse,
   filter, sort, and deduplicate versions deterministically.

The score is an explainable **heuristic, not a calibrated probability or ownership
verification**. A name and a claimed homepage can be spoofed. Reciprocal links
provide stronger evidence but do not guarantee identity. A production resolver
would need stronger identity validation and a review mechanism.

## APIs, keys, and bounds

No API key is required. Optional environment variables:

- `GITHUB_TOKEN`: increases API allowance; only public resources are needed.
- `BRAVE_SEARCH_API_KEY`: enables the supported Brave search API instead of the
  best-effort DuckDuckGo HTML integration.

Credentials are sent only to their provider host and are not stored in snapshots.
URLs with embedded credentials or non-public addresses are rejected; redirects
are checked again. This CLI is not a hardened multi-tenant URL-fetching service.

Default bounds are 100 network requests, 30 pages per GitHub catalog, 8 pages per
HTML crawl, 15-second HTTP timeouts, and 50 MB per decoded response. The first three
are configurable with `--max-requests`, `--max-pages`, and `--max-html-pages`.
Next.js's abbreviated npm catalog exceeded 25 MB during validation, which is why
the response limit is 50 MB. Failed pages, rate limits, and exhausted budgets
produce partial results rather than a claim of no intermediate versions.

## Reproducible retrieval

Responses are saved under `.cache/upgrade-resolver/`, keyed by URL and Accept
header. Each snapshot includes retrieval time, final URL, HTTP status, body, and
a SHA-256 of the decoded body. `source_inventory` exposes metadata without the
response bodies. Each selected version points to the response supplying the
record; `record_url`, when present, links to its release/tag/document page.

Cached responses are reused by default, **without automatic expiry**. Use
`--refresh` to retrieve a new snapshot. An offline run performs zero network
requests and reports missing cache entries explicitly:

```bash
.venv/bin/upgrade-resolve \
  --project Django --current-version 3.2 --target-version 4.2 \
  --offline --output outputs/django-offline.json
```

Use the same inputs, search backend, and optional sources when replaying. For
portable caches prefer an explicit `--web-search` value: `auto` depends on whether
a Brave key is present. Cache files are ignored by Git because catalogs can be
large; copy the cache separately to replay on another machine. Tests use local
fixtures and do not require these snapshots or network access.

## Validation

```bash
.venv/bin/python -m pytest -q
```

Tests cover an unseen project, a repository rename, companion-package rejection,
identity ambiguity, bot challenges, exact/family bounds, numeric ordering,
prereleases, missing endpoints, pagination, request failures, HTML discovery,
offline replay, snapshot integrity, and credential isolation on redirects.

Live checks were performed for the first five example inputs, with no source URL
arguments and no project mappings. All five were also replayed with zero network
requests. These were discovery-stage checks, not final risk models.

Replay every example input with `.venv/bin/python examples/resolve_examples.py --offline`;
omit `--offline` to allow fetching missing snapshots. The example input file
contains only the three required arguments per upgrade, not source mappings.

## Known limitations

- Only the listed provider formats are implemented; other registries and hosting
  providers need adapters. Arbitrary public software is not guaranteed to resolve.
- Ambiguous names cannot always be resolved from three inputs. No LLM is used to
  guess an identity or fill missing versions.
- Search ranking, index coverage, naming heuristics, and rate limits affect
  recall. A correctly matching low-ranked project may never enter the candidate set.
- GitHub mirrors and tags remain distinguishable from publication records.
  PostgreSQL currently resolves through its mirror's tags; publication remains
  explicitly unverified.
- HTML extraction uses same-host links and is always marked incomplete. It can
  still misidentify a numeric documentation link as a release. It needs review.
- The stable-numeric parser intentionally does not support every ecosystem's
  version syntax. Monorepos with unrelated package names may need stronger
  component identity evidence.
- Sources can remove old versions. An enumerated current catalog is not proof
  of complete historical coverage.

## API references

- [GitHub repository search](https://docs.github.com/en/rest/search/search#search-repositories)
- [GitHub Releases](https://docs.github.com/en/rest/releases/releases#list-releases)
- [npm registry API](https://github.com/npm/registry/blob/main/docs/REGISTRY-API.md)
- [PyPI JSON API](https://docs.pypi.org/api/json/)
- [Python version parsing](https://packaging.pypa.io/en/stable/version.html)
