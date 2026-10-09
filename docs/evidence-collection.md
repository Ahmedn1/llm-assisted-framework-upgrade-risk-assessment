# Evidence collection

The separate `evidence_collector` package consumes a saved resolver result. It makes
no model calls and does not change resolver source scores. Inputs must have a selected
catalog and a nonempty version interval (`resolved` or `partial`); an ambiguous identity
must be resolved first. The original resolver status/coverage are retained in the output.

```bash
.venv/bin/python -m evidence_collector \
  --resolution outputs/postgresql-heuristic-v2.json \
  --output outputs/postgresql-evidence.json
```

After installing the updated project, `upgrade-collect` is the equivalent entry point.
Use `--offline` for cached replay and `--refresh` for conditional revalidation using
ETag/Last-Modified when available. A 304 preserves the original body/retrieval time
and records `validated_at`. Cache keys include the requested representation.

## Discovery

1. Fetch the resolver's release-record URLs and selected catalog. For registry-backed
   versions, fetch version-specific PyPI/npm metadata directly. These per-version records have
   their own budget (60 by default) and do not count toward the document limit, so a long patch
   series cannot crowd out documentation.
2. Follow relevant links in content and upgrade/migration navigation. Resolve HTML
   base URLs and redirects. Preserve nested section IDs as citation anchors. The project site
   includes sibling subdomains of the homepage's domain (`docs.example.org` for
   `www.example.org`), except on shared hosting such as `github.io` or `readthedocs.io`.
   Notes of the resolver's documentation milestones (e.g. 4.0, 4.1, 4.2), and upgrade guides named
   for one ("Upgrading: Version 14"), are fetched first; other in-interval release notes share the
   next priority with migration and changelog links.
3. Patch-release notes are often linked while milestone notes are not. When a release-notes URL
   on the project site carries an in-interval version (`…/releases/4.2.30/`), the same URL is
   derived for each documentation milestone (`…/releases/4.0/`) and fetched first. A derived URL
   is a guess until it fetches; a 404 is recorded as a failure, never as evidence.
   A registry catalog (npm, PyPI) has no release pages, so for each documentation milestone the
   repository's tagged release (`github.com/<repo>/releases/tag/v14.0.0`) is guessed the same way.
   Project-site pages the resolver met while identifying the project (a release announcement, a
   versioned upgrade guide found by its search) are fetched as well.
4. For associated repositories, use GitHub release and contents APIs, following
   changelog/documentation directories within the depth budget. Referenced issues/PRs
   use their APIs; a bounded repository-scoped search supplements explicit links.
   Repository association inherited from the resolver is not ownership verification.
5. Discover sitemaps through robots.txt and the conventional sitemap.xml location,
   including sitemap indexes. Follow RSS/Atom feeds declared in page metadata.
6. Optional `--search auto` enables five search hints through the shared web-search
   function: Brave with `BRAVE_SEARCH_API_KEY`, falling back to DuckDuckGo when
   unavailable. `brave` also permits fallback; `duckduckgo` selects it directly.
   The default `none` makes no general web-search calls. Search/explicit `--source-url` hints are supporting context.

There are no project-name-to-URL mappings. Bounded keyword rules classify document
roles and identify candidate links; they can miss unusual naming or JS-only pages.
External writeups and blog posts are not automatically labeled reputable or maintainer
endorsed. Their publisher relationships and applicability require review.

## Boundaries and transport

Defaults: 100 documents, 60 per-version registry records, depth 2, 500 discovered URLs, eight
API/sitemap/feed index pages, 200 HTTP requests, and 10 MB per decoded response. Limits are
CLI-configurable except the registry-record budget and the response-size cap. Requests are sequential with per-host pacing and up to
two retries on 429/500/502/503/504. Retry-After delays above ten seconds defer retrieval
instead of retrying too early. No CAPTCHA bypass or search-result HTML scraping.

Public-URL checks apply to each request/redirect. robots.txt disallows are respected
for HTML sites; unavailable robots policies defer live retrieval except 404/410.
Offline replay can read cached public documents without a cached robots file and
records that limitation. APIs use their own access/rate rules. Redirect destinations
are checked against robots policy before processing their content; the redirect fetch
itself is already performed by the shared HTTP transport.

Binary assets, PDFs and browser-rendered content are outside this first implementation.
GitHub source-tree tags are not treated as published release notes. Repository file
snapshots record their Git blob SHA but are not assumed to represent the target release.
Issue/PR bodies and status are captured; discussion comments and full PR diffs are not.
GitHub's list/contents/search limits and local index budgets can leave gaps.

## Output

Schema 1.1 exposes `documents_by_version`, keyed by each selected version number.
Each entry is a list of full document objects, including text, sections and provenance.
A document associated with multiple versions appears in every corresponding list,
with the same stable ID. `matching_associations` explains its placement in that
particular version. Empty versions have an empty list. Grouping preserves unreviewed
applicability; it does not upgrade candidate relevance into a verified claim.

The canonical `documents[]` inventory remains available. `unassigned_document_ids`
identifies collected documents with no association to any selected version.
Grouped views expand deduplicated text/sections from the referenced content document,
retaining `duplicate_content_of` and original section citation URLs; publisher
relationships remain those of the individual document. JSON therefore intentionally
repeats content across version entries for direct consumption.

For example, a shared guide appears as:

```json
{
  "documents_by_version": {
    "14.0": [{"id": "doc_shared", "title": "Upgrade guide", "text": "..."}],
    "15.0": [{"id": "doc_shared", "title": "Upgrade guide", "text": "..."}]
  }
}
```

`documents[]` stores:

- Stable ID, canonical retrieval destination/API HTML URL, aliases, and discovery edges.
- Document type, title, extracted text and sections with original anchors.
- Publisher relationship; `authority_inherited` is always false.
- Retrieval/revalidation timestamps, raw-response and extracted-content SHA-256 hashes.
- Version associations and their basis: release-record link, registry version,
  explicit text mention, a milestone named by the title or URL (`Version 14`, `/blog/next-13`;
  body text is not used for the short form) or linked context. Every association is `unreviewed`.
- Native metadata such as issue state, PR merge timestamp or release tag.

One URL is fetched once and can associate with multiple versions. Identical extracted
content is stored once: duplicate documents retain their distinct provenance and refer
to `duplicate_content_of` rather than inheriting another publisher's authority.

`version_coverage` lists candidate evidence per selected version. `failures`, `skipped`,
`pending_urls`, `warnings` and transport `source_inventory` explain gaps. A budget-exhausted
URL stays pending; a fetch failure does not mean the document or release does not exist.
The result remains `partial`: collection does not prove exhaustive discovery or factual
applicability. CLI success means documents were collected, not that coverage is complete.

An issue mentioning 2.0 does not prove a defect affects 2.0; a merged PR does not prove
it shipped. A guide linked from release notes is contextual evidence, not a verified
version applicability statement. This stage collects evidence; [risk extraction](risk-extraction.md)
and [verification](verification.md) build and check claims from it.

## Provider references

- [GitHub release API](https://docs.github.com/en/rest/releases/releases)
- [GitHub contents API](https://docs.github.com/en/rest/repos/contents)
- [GitHub issues API](https://docs.github.com/en/rest/issues/issues)
- [PyPI JSON API](https://docs.pypi.org/api/json/)
- [Sitemap protocol](https://www.sitemaps.org/protocol.html)

## PostgreSQL validation (2026-10-09, before the milestone and registry changes)

Using the selected PostgreSQL 13 → 16 resolver interval and limits of 90 documents,
350 URLs, and 60 new HTTP requests, the final run collected 62 release-note documents,
24 API/documentation pages and four upgrade guides, with zero retrieval failures.
All 61 selected versions have candidate evidence. Migration section anchors such as
`/docs/release/14.0/#id-1.11.6.30.4` are retained.

The document budget left 260 URLs pending; this is not exhaustive collection and does
not establish every document type for every version. `version_coverage` reports both
collected and not-collected document types. Missing types are not claims of absence.
The final run reused earlier snapshots and needed seven new requests. Offline replay
produced identical document IDs/content hashes with zero network requests.
The reports were generated locally and are not committed.

### Retrieval and scope safeguards

The collector defers live requests to a host after an explicit GitHub rate-limit
403, exhausted 429 retries, a long Retry-After, or three 403 access failures in a
run. It can still read that host's cached documents. Unattempted URLs on deferred
hosts remain pending and are reported separately from retrieval failures.

Migration guides, changelogs, and sitemap discovery precede issue expansion.
Discussion links can consume at most one fifth of the URL budget, reserving space
for documentation. Release notes and upgrade/migration guides may exceed the URL budget by a
further fifth, so a large documentation site cannot crowd them out. Explicit version markers in documentation paths,
project-qualified blog slugs, and extracted HTML titles are checked against the
requested interval (including baseline documentation). This is a conservative
filter, not proof of applicability: unversioned pages and multi-version changelogs
still require downstream review. No model calls are used for these safeguards.
Robots-advertised sitemaps are expanded only for the project site (including its sibling
subdomains); supporting sites do not seed their unrelated sitemaps or feeds.
