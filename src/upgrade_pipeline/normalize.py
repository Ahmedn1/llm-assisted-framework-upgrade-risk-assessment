"""Path-independent views of collected evidence: the source inventory and candidate sections.

Both collectors write the same evidence.json schema; this stage turns either one into
  * source_inventory.json   - ranked, trust-labelled sources in the output contract's shape;
  * candidate_sections.json - topic-tagged, version-scoped, deduplicated sections for risk extraction.
Everything here is deterministic. Trust labels come from recorded resolver/registry facts, never from the model.
"""
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from evidence_collector.collector import site_of as site
from evidence_collector.extraction import section_topics
from upgrade_resolver.versions import numeric_version
from .common import VERSION_TOKEN, get_logger, write_json
from .schema import CHANGE_TYPES, TIER_RANK

TYPE_ORDER = ('upgrade_guide', 'release_notes', 'github_release', 'deprecation_notice', 'api_documentation',
              'registry_metadata', 'github_pull_request', 'github_issue', 'blog_post', 'supporting_document')
RELATION_ORDER = ('resolver_release_record', 'repository_document', 'resolver_homepage', 'linked_context')
MAX_SECTION_CHARS = 6000


def repository_path(url):
    """owner/repo for GitHub web, raw and API URLs; None otherwise."""
    parsed = urlsplit(url)
    parts = [p for p in parsed.path.split('/') if p]
    if parsed.hostname in ('github.com', 'raw.githubusercontent.com') and len(parts) >= 2:
        return '/'.join(parts[:2]).lower().removesuffix('.git')
    if parsed.hostname == 'api.github.com' and len(parts) >= 3 and parts[0] == 'repos':
        return '/'.join(parts[1:3]).lower()
    return None


def project_identity(resolution, research_sources=()):
    """Official homepage sites and repositories from the resolver identity or the selected registry record."""
    sites, repositories = set(), set()
    identity = resolution.get('identity') or {}
    if identity.get('homepage'):
        sites.add(site(urlsplit(identity['homepage']).hostname))
    if identity.get('repository'):
        repositories.add(identity['repository'].lower())
    catalog = resolution.get('selected_catalog') or ''
    for source in research_sources:
        if source.get('url') != catalog or source.get('projection') != 'registry_inventory':
            continue
        try:
            data = json.loads(source['text'])
        except (ValueError, KeyError):
            continue
        info = data.get('info') or {}
        repository = data.get('repository')
        urls = [data.get('homepage'), info.get('home_page'), repository.get('url') if isinstance(repository, dict) else repository,
                *(info.get('project_urls') or {}).values()]
        for url in filter(None, urls):
            url = re.sub(r'^git\+', '', str(url))
            if repository_path(url):
                repositories.add(repository_path(url))
            elif urlsplit(url).hostname:
                sites.add(site(urlsplit(url).hostname))
    sites.discard('github.com')
    return sites, repositories


def trust(document, sites, repositories, catalog):
    url = document['url']
    host = urlsplit(url).hostname or ''
    repository = repository_path(url)
    discussion = document['document_type'] in ('github_issue', 'github_pull_request')
    if url == catalog or document['document_type'] == 'registry_metadata':
        return 'official', 'Package registry record selected as the release catalog.'
    if repository and repository in repositories:
        if discussion:
            return 'official_repository_discussion', f'Issue/PR in the project repository {repository}; discussion, not a release statement.'
        return 'official', f'Hosted in the project repository {repository}.'
    if repository and repository.split('/')[1] in {r.split('/')[1] for r in repositories}:
        return 'probable_official', (f'Repository name matches {sorted(repositories)} but owner differs ({repository}); '
                                     'possibly an organisation rename or a fork. Not verified.')
    if host and site(host) in sites:
        return 'official', f'Hosted on the project site {site(host)}.'
    if document.get('publisher_relationship') in ('associated_repository', 'selected_project_site'):
        return 'official', f"Resolver relationship: {document['publisher_relationship']}."
    return 'third_party', 'Not hosted on the project site or repository; supporting context only.'


def source_type(document, tier):
    kind, official = document['document_type'], tier in ('official', 'probable_official')
    if kind == 'release_notes':
        if re.search(r'changelog|changes\.(?:md|rst|txt)', document['url'], re.I):
            return 'official_changelog' if official else 'changelog'
        return 'official_release_notes' if official else 'release_notes'
    return {
        'upgrade_guide': 'official_migration_guide' if official else 'migration_guide',
        'api_documentation': 'official_api_documentation' if official else 'third_party_documentation',
        'blog_post': 'maintainer_blog_post' if official else 'community_writeup',
        'supporting_document': 'official_documentation' if official else 'community_writeup',
        'registry_metadata': 'package_registry_metadata',
    }.get(kind, kind)


def why_relevant(document, versions, topic_counts):
    claims = document.get('relevance_evidence') or []
    if claims:
        rationales = list(dict.fromkeys(c['rationale'] for c in claims))
        reason = ' '.join(rationales[:2]) + f' ({len(claims)} verified quote(s).)'
    else:
        relations = sorted(document.get('discovered_from') or [], key=lambda r: RELATION_ORDER.index(r['relation'])
                           if r.get('relation') in RELATION_ORDER else len(RELATION_ORDER))
        relation = relations[0] if relations else {}
        reason = {
            'resolver_release_record': f"Release record for {', '.join(relation.get('versions') or versions)} in the selected catalog.",
            'repository_document': f"Project repository document ({document['title'][:80]}).",
            'resolver_homepage': 'Project homepage selected by the resolver.',
            'linked_context': f"Linked from {relation.get('url', 'a collected document')}.",
        }.get(relation.get('relation'), 'Collected as supporting context.')
        mentioned = sorted({a['version'] for a in document['version_associations'] if a['basis'] == 'text_mention'})
        if mentioned:
            reason += f" Mentions in-range versions {', '.join(mentioned)}."
    if topic_counts:
        reason += ' Candidate sections: ' + ', '.join(f'{n} {t}' for t, n in topic_counts.most_common(4)) + '.'
    return reason


def exclusion(document, tier):
    bases = {a['basis'] for a in document['version_associations']}
    if not bases:
        return 'No association with any resolved version.'
    if tier == 'third_party' and bases <= {'linked_context'}:
        return 'Third-party page reached only by link-following; no version-specific statement.'
    return None


def resolved_label(label, resolved):
    version = numeric_version(label)
    return next((v for v in resolved if version is not None and numeric_version(v) == version), None)


def section_versions(section, resolved):
    """In-range labels of the nearest version heading, or None if no ancestor heading names a version.
    An empty list means the section belongs to an out-of-range release."""
    for heading in [section['heading'], *reversed(section.get('heading_path') or [])]:
        labels = [l for l in VERSION_TOKEN.findall(heading) if numeric_version(l) is not None]
        if labels:
            return sorted({v for v in (resolved_label(l, resolved) for l in labels) if v})
    return None


def legacy_scopes(document, resolved):
    """Version scope for evidence saved before heading paths existed. Older parsers dropped a heading with no
    text of its own (a version heading directly followed by a subheading), so recover the nearest version
    heading from the document text, which still contains every heading line."""
    text = document.get('text') or ''
    headings = {s['heading'] for s in document['sections']}
    marks, position = [], 0
    for line in text.splitlines(keepends=True):
        bare = re.sub(r'^\s*#{1,6}\s+', '', line).strip()
        if bare and (line.lstrip().startswith('#') or bare in headings) and VERSION_TOKEN.search(bare):
            marks.append((position, section_versions({'heading': bare}, resolved)))
        position += len(line)
    scopes, cursor = [], 0
    for section in document['sections']:
        probe = section['text'].strip()[:80]
        found = text.find(probe, cursor) if probe else cursor  # a heading-only section sits where the last one ended
        if found < 0:
            scopes.append(section_versions({'heading': section['heading']}, resolved))
            continue
        cursor = found
        governing = [scope for at, scope in marks if at <= found]
        scopes.append(governing[-1] if governing else None)
    return scopes


def fingerprint(text):
    return re.sub(r'[\W_]+', ' ', text.lower()).strip()


def normalize_case(resolution, evidence, research_sources=()):
    item = evidence['input']
    resolved = list(evidence['version_coverage'])
    sites, repositories = project_identity(resolution, research_sources)
    catalog = evidence.get('selected_catalog') or ''
    sources, excluded, candidates = [], [], []
    # Per-patch registry records repeat the same metadata; only milestone and latest records feed extraction.
    milestones = {numeric_version(m) for m in resolution.get('documentation_milestones', [])}
    registry_keep = {v for v in resolved if numeric_version(v) in milestones} | set(resolved[-1:])
    registry_skipped = 0
    for document in evidence['documents']:
        tier, basis = trust(document, sites, repositories, catalog)
        reason = exclusion(document, tier)
        if reason:
            excluded.append({'source_id': document['id'], 'url': document['url'], 'title': document['title'][:200],
                             'document_type': document['document_type'], 'trust_tier': tier, 'reason': reason})
            continue
        doc_versions = sorted({a['version'] for a in document['version_associations']}, key=resolved.index)
        topic_counts, kept, out_of_range = Counter(), [], 0
        legacy = legacy_scopes(document, resolved)
        patch_registry = document['document_type'] == 'registry_metadata' and not set(doc_versions) & registry_keep
        registry_skipped += patch_registry
        for index, section in enumerate([] if patch_registry else document['sections']):
            if not section['text'].strip():
                continue  # a heading with no body has nothing to extract
            topics = section.get('topics')
            if topics is None:  # evidence written before topic tagging existed
                topics = section_topics(section['heading'], section['text'])
            versions = section_versions(section, resolved) if 'heading_path' in section else legacy[index]
            if versions == []:
                out_of_range += 1
                continue
            if not topics and document['document_type'] in CHANGE_TYPES and section['text'].strip():
                topics = ['unclassified_change']
            if not topics:
                continue
            topic_counts.update(topics)
            text = section['text'].strip()
            kept.append({'candidate_id': f"{document['id']}#s{index}", 'source_id': document['id'],
                         'url': section.get('url') or document['url'], 'heading': section['heading'],
                         'heading_path': section.get('heading_path', []), 'topics': topics,
                         'versions': versions or doc_versions,
                         'version_scope': 'version_heading' if versions else 'document_association',
                         'text': text[:MAX_SECTION_CHARS], 'truncated': len(text) > MAX_SECTION_CHARS})
        sources.append({'source_id': document['id'], 'title': document['title'][:300], 'url': document['url'],
                        'source_type': source_type(document, tier), 'retrieved_at': document['retrieved_at'],
                        'why_relevant': why_relevant(document, doc_versions, topic_counts),
                        'trust': {'tier': tier, 'basis': basis}, 'document_type': document['document_type'],
                        'roles': document.get('document_types') or [document['document_type']],
                        'versions': doc_versions, 'version_association_bases': sorted({a['basis'] for a in document['version_associations']}),
                        'verified_quotes': len(document.get('relevance_evidence') or []),
                        'candidate_sections': len(kept), 'out_of_range_sections': out_of_range,
                        'content_sha256': document['content_sha256']})
        candidates.append((sources[-1], kept))

    rank = lambda s: (TIER_RANK[s['trust']['tier']], TYPE_ORDER.index(s['document_type'])
                      if s['document_type'] in TYPE_ORDER else len(TYPE_ORDER), -s['verified_quotes'], s['url'])
    sources.sort(key=rank)
    for position, source in enumerate(sources, 1):
        source['rank'] = position
    # Exact duplicate passages (e.g. a deprecation list repeated in a release post and a guide) are kept but
    # pointed at the copy from the most trusted source, so extraction can skip them without losing provenance.
    seen, sections = {}, []
    for source, kept in sorted(candidates, key=lambda pair: pair[0]['rank']):
        for candidate in kept:
            key = fingerprint(candidate['text'])
            candidate['duplicate_of'] = seen.get(key) if len(key) >= 40 else None
            if len(key) >= 40:
                seen.setdefault(key, candidate['candidate_id'])
            candidate['source_rank'] = source['rank']
            sections.append(candidate)

    header = {'project': item['project'], 'current_version': item['current_version'], 'target_version': item['target_version'],
              'resolved_versions': resolved, 'documentation_milestones': resolution.get('documentation_milestones', []),
              'collection_path': evidence.get('path', 'deterministic'),
              'generated_at': datetime.now(timezone.utc).isoformat()}
    inventory = {**header, 'sources': sources, 'excluded_sources': excluded,
                 'trust_policy': {'official': 'Project site, project repository or the selected package registry record.',
                                  'probable_official': 'Repository name matches but owner differs; not verified.',
                                  'official_repository_discussion': 'Issues/PRs in the project repository: context, not release statements.',
                                  'third_party': 'Supporting context only; never overrides official sources.'},
                 'notes': ['source_id equals the evidence document id, so evidence.json and candidate_sections.json join on it.',
                           'Fetch-level request log (including robots.txt checks) remains in evidence.json source_inventory.']}
    by_topic = Counter(t for c in sections if not c['duplicate_of'] for t in c['topics'])
    by_version = Counter(v for c in sections if not c['duplicate_of'] for v in c['versions'])
    candidate_file = {**header, 'summary': {'candidate_sections': len(sections), 'patch_registry_records_skipped': registry_skipped,
                                            'duplicates': sum(bool(c['duplicate_of']) for c in sections),
                                            'by_topic': dict(by_topic), 'by_version': {v: by_version.get(v, 0) for v in resolved}},
                      'topic_note': 'Topics are recall-oriented keyword signals that nominate sections for extraction; they are not risk claims.',
                      'sections': sections}
    return inventory, candidate_file


def write_normalized(output_dir, manifest, resolution, evidence, research_sources=()):
    inventory, candidates = normalize_case(resolution, evidence, research_sources)
    write_json(Path(output_dir) / 'source_inventory.json', inventory)
    write_json(Path(output_dir) / 'candidate_sections.json', candidates)
    manifest['files'].update(source_inventory='source_inventory.json', candidate_sections='candidate_sections.json')
    get_logger('normalize').info('%d sources kept, %d excluded; %d candidate sections (%d duplicates)',
                                 len(inventory['sources']), len(inventory['excluded_sources']),
                                 candidates['summary']['candidate_sections'], candidates['summary']['duplicates'])
    return inventory, candidates


def main(argv=None):
    """Re-normalize saved case directories without re-collecting: python -m upgrade_pipeline normalize DIR..."""
    directories = [Path(p) for p in (argv if argv is not None else sys.argv[1:])]
    if not directories:
        print('usage: python -m upgrade_pipeline normalize CASE_DIR [CASE_DIR...]', file=sys.stderr)
        return 2
    for directory in directories:
        evidence = json.loads((directory / 'evidence.json').read_text())
        resolution = json.loads((directory / 'resolution.json').read_text())
        research = directory / 'research-sources.json'
        manifest = {'files': {}}
        inventory, candidates = write_normalized(directory, manifest, resolution, evidence,
                                                 json.loads(research.read_text()) if research.exists() else ())
        print(json.dumps({'directory': str(directory), 'sources': len(inventory['sources']),
                          'excluded': len(inventory['excluded_sources']), **candidates['summary']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
