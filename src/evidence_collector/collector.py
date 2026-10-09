"""Direct-first evidence collection with explicit gaps and no model calls."""
import base64
import hashlib
import heapq
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit, urldefrag
from urllib.robotparser import RobotFileParser

from web_search import search_web

from upgrade_resolver.http import FetchError, validate_public_url
from upgrade_resolver.discovery import github_repository
from upgrade_resolver.versions import parse_endpoint, family_upper, numeric_version
from .extraction import TOPICS, classify, html_document, markdown_document, milestone_mentions, xml_links, version_mentions


log = logging.getLogger('upgrade.collect')  # handler attached by the pipeline CLI
SHARED_HOSTING = ('github.io', 'readthedocs.io', 'netlify.app', 'vercel.app', 'pages.dev', 'gitlab.io')


def site_of(hostname):
    """Registrable domain (last two labels); shared hosting keeps the full host so tenants stay separate."""
    hostname = (hostname or '').lower()
    return hostname if hostname.endswith(SHARED_HOSTING) else '.'.join(hostname.split('.')[-2:])


def collect(client, resolution, *, max_documents=100, max_depth=2, max_urls=500,
            max_index_pages=8, search='none', extra_urls=(), max_registry_documents=60):
    if any(type(v) is not int or v < 1 for v in (max_documents, max_urls, max_index_pages, max_registry_documents)) or max_depth < 0:
        raise ValueError('Collection limits must be positive; max_depth may be zero')
    if search not in ('none', 'auto', 'brave', 'duckduckgo'):
        raise ValueError('search must be none, auto, brave, or duckduckgo')
    # A ranking tie must not quietly become a trusted documentation seed.
    if resolution.get('status') not in ('resolved', 'partial') or not resolution.get('selected_catalog') or not resolution.get('versions'):
        raise ValueError('Collect from a resolver result with a selected catalog and nonempty versions; resolve ambiguity first')
    records = resolution['versions']
    versions = list(dict.fromkeys(r['version'] for r in records))
    identity = resolution.get('identity') or {}
    selected = resolution['selected_catalog']
    host = urlsplit(identity.get('homepage') or selected).hostname
    project_hosts = {host} if host and host not in ('github.com', 'api.github.com', 'pypi.org', 'registry.npmjs.org') else set()
    # Release notes often live on a sibling host (docs.example.org for www.example.org).
    project_sites = {site_of(h) for h in project_hosts}

    def on_project_site(hostname):
        return bool(hostname) and (hostname in project_hosts or site_of(hostname) in project_sites)
    repositories = {identity['repository']} if identity.get('repository') else set()
    for candidate in resolution.get('candidates', []):
        if candidate.get('repository') and urlsplit(candidate.get('homepage', '')).hostname in project_hosts:
            repositories.add(candidate['repository'])
    # These are associations from the resolver, not fresh ownership verification.
    repositories = set(sorted(repositories)[:3])
    queue, nodes, documents, by_final, failures, skipped = [], {}, [], {}, [], []
    robots, visited = {}, set()
    deferred_urls = []
    warnings, index_count = [], 0
    sequence = 0
    discussion_count = 0
    lower = parse_endpoint(resolution['input']['current_version'])
    target = parse_endpoint(resolution['input']['target_version'])
    family_mode = resolution['input'].get('version_mode', 'family') == 'family'
    upper = family_upper(target) if family_mode else target
    started_requests = client.request_count

    def scope_numbers(url, title=''):
        path = urlsplit(url).path
        labels = re.findall(r'(?:releases?[-/]|/docs?/(?:[a-z]{2}/)?|/en/|version[-/])(\d+(?:[.-]\d+){0,3})(?=/|\.html|$)', path, re.I)
        # Project-qualified blog slugs/headings avoid treating dates and issue IDs as versions.
        project = re.escape(resolution['input']['project'])
        labels += re.findall(project + r'[-\s]+v?(\d+(?:\.\d+){0,3})(?![\d.])', path + ' ' + title, re.I)
        return [parse_endpoint(v.replace('-', '.')) for v in labels]

    def outside_scope(url, title=''):
        numbers = scope_numbers(url, title)
        return bool(numbers) and not any(n >= lower and (n < upper if family_mode else n <= upper) for n in numbers)

    milestones = [parse_endpoint(m) for m in resolution.get('documentation_milestones', [])]

    def release_notes_rank(url, label=''):
        """0 for notes of a documentation milestone (e.g. 4.0), 1 for other in-interval releases, None otherwise."""
        if not re.search(r'release|changelog|changes|what.?s.?new', label + ' ' + urlsplit(url).path, re.I):
            return None
        numbers = [n for n in scope_numbers(url, label) if n >= lower and (n < upper if family_mode else n <= upper)]
        if not numbers:
            return None
        return 0 if any(n in milestones for n in numbers) else 1  # numeric equality: 4.0 == 4.0.0

    templates = set()

    def milestone_siblings(url, depth):
        """Patch-release notes are often linked while the milestone notes (4.0, 4.1, 4.2) are not. Derive the
        milestone URLs from a fetched notes URL; they are guesses until a fetch succeeds."""
        parsed = urlsplit(url)
        match = re.search(r'((?:releases?|release-notes|changelog)[-/])v?(\d+(?:\.\d+){0,3})(?=/|\.html|$)', parsed.path, re.I)
        if not match or not on_project_site(parsed.hostname):
            return
        template = parsed.path[:match.start(2)] + '{}' + parsed.path[match.end(2):]
        if template in templates:
            return
        templates.add(template)
        for milestone in resolution.get('documentation_milestones', []):
            for label in dict.fromkeys([milestone] + ([milestone + '.0'] if '.' not in milestone else [])):
                add(urlunsplit(parsed._replace(path=template.format(label), query='')), url, 'milestone_sibling_guess',
                    [v for v in versions if parse_endpoint(v) == parse_endpoint(milestone)], depth=depth, priority=0)

    def link_priority(url, label='', same_site=True):
        if re.search(r'github\.com/[^/]+/[^/]+/(issues|pull)/\d+', url):
            return 9
        rank = release_notes_rank(url, label) if same_site else None
        if rank is not None:
            return rank  # notes for in-interval releases before guides and context pages
        if same_site and re.search(r'migrat|upgrad|changelog|deprecat', label + ' ' + url, re.I):
            # A guide named for a documentation milestone ("Upgrading: Version 14") ranks with milestone notes.
            return 0 if milestone_mentions(label, url, versions, resolution.get('documentation_milestones', [])) else 1
        return 3 if same_site else 7

    def add(url, parent=None, relation='linked_context', associated=(), depth=0, priority=3, supporting=False):
        nonlocal sequence, discussion_count
        url = urldefrag(url)[0]
        try:
            validate_public_url(url, resolve_dns=False)
        except (FetchError, ValueError) as exc:
            skipped.append({'url': url, 'reason': str(exc)})
            return
        if re.search(r'\.(?:zip|gz|xz|tar|exe|whl|png|jpg|svg|pdf)(?:$|\?)', url, re.I):
            skipped.append({'url': url, 'reason': 'Unsupported binary/document format'})
            return
        if outside_scope(url):
            skipped.append({'url': url, 'reason': 'Versioned link outside requested interval and baseline'})
            return
        proof = {'url': parent, 'relation': relation, 'versions': list(associated)}
        if url in nodes:
            if proof not in nodes[url]['discovered_from']:
                nodes[url]['discovered_from'].append(proof)
            return
        discussion = bool(re.search(r'/(?:issues|pull|pulls)/\d+(?:$|[/?])', urlsplit(url).path))
        if discussion and discussion_count >= max(1, max_urls // 5):
            skipped.append({'url': url, 'reason': 'Discussion URL budget; reserve space for documentation'})
            return
        # Release notes and upgrade guides keep a reserve, so a large site cannot crowd them out of the URL budget.
        if depth > max_depth or len(nodes) >= max_urls + (max_urls // 5 if priority <= 1 else 0):
            skipped.append({'url': url, 'reason': 'Depth or URL budget'})
            return
        node = {'url': url, 'depth': depth, 'supporting': supporting, 'discovered_from': [proof]}
        nodes[url] = node
        discussion_count += int(discussion)
        heapq.heappush(queue, (priority, sequence, node))
        sequence += 1

    def allowed(url):
        parsed = urlsplit(url)
        # APIs supply their own access/rate rules. All requests still use public-URL validation.
        if parsed.hostname in ('api.github.com', 'pypi.org', 'registry.npmjs.org', 'api.search.brave.com'):
            return True
        origin = f'{parsed.scheme}://{parsed.netloc}'
        if origin not in robots:
            robot_url = origin + '/robots.txt'
            try:
                page = client.get(robot_url, accept='text/plain')
                policy = RobotFileParser()
                policy.parse(page['body'].splitlines())
                robots[origin] = policy
                if on_project_site(parsed.hostname):
                    for sitemap in policy.site_maps() or []:
                        add(sitemap, robot_url, 'sitemap', priority=2)
            except FetchError as exc:
                if '404' in str(exc) or '410' in str(exc) or client.offline:
                    robots[origin] = None
                    warnings.append(f'robots.txt unavailable for {origin}: {exc}')
                else:
                    robots[origin] = False
                    warnings.append(f'Host deferred because robots.txt could not be checked: {origin}: {exc}')
        policy = robots[origin]
        return policy is None or (policy is not False and policy.can_fetch('UpgradeResolver', url))

    def convert(url):
        parsed = urlsplit(url)
        parts = parsed.path.strip('/').split('/')
        if parsed.hostname == 'github.com' and len(parts) >= 4:
            root = 'https://api.github.com/repos/' + '/'.join(parts[:2])
            if parts[2] in ('issues', 'pull') and parts[3].isdigit():
                return root + ('/pulls/' if parts[2] == 'pull' else '/issues/') + parts[3]
            if len(parts) >= 5 and parts[2:4] == ['releases', 'tag']:
                return root + '/releases/tags/' + quote('/'.join(parts[4:]), safe='')
            if parts[2] == 'tree':
                return None  # A source tag is not release-note text.
        return url

    def emit(node, page, title, text, sections, kind, metadata=None, canonical=None):
        canonical = canonical or page['final_url']
        if canonical in by_final:
            existing = by_final[canonical]
            existing['aliases'].append(node['url'])
            existing['discovered_from'].extend(x for x in node['discovered_from'] if x not in existing['discovered_from'])
            return existing
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        hints = [{'version': v, 'basis': 'text_mention', 'status': 'unreviewed'} for v in version_mentions(title + '\n' + text, versions)]
        hints += [{'version': v, 'basis': 'milestone_mention', 'status': 'unreviewed'}
                  for v in milestone_mentions(title, canonical, versions, resolution.get('documentation_milestones', []))]
        for proof in node['discovered_from']:
            for v in proof['versions']:
                hints.append({'version': v, 'basis': proof['relation'], 'status': 'unreviewed'})
        parsed = urlsplit(canonical)
        repo = github_repository(canonical)
        relationship = ('selected_project_site' if on_project_site(parsed.hostname) else
                        'associated_repository' if repo in repositories else
                        'package_registry' if kind == 'registry_metadata' else 'supporting_context')
        if kind in ('github_issue', 'github_pull_request'):
            relationship = 'repository_discussion_not_release_confirmation'
        doc = {'id': 'doc_' + hashlib.sha256(canonical.encode()).hexdigest()[:16],
               'url': canonical, 'retrieval_url': page['url'], 'aliases': [node['url']],
               'document_type': kind, 'title': title, 'text': text, 'sections': sections,
               'publisher_relationship': relationship, 'authority_inherited': False,
               'retrieved_at': page.get('retrieved_at'), 'validated_at': page.get('validated_at'),
               'content_sha256': content_hash,
               'response_sha256': page.get('content_sha256') or hashlib.sha256(page['body'].encode()).hexdigest(),
               'discovered_from': node['discovered_from'], 'version_associations': hints,
               'applicability': 'unreviewed', 'metadata': metadata or {}}
        duplicate = next((d for d in documents if d['content_sha256'] == content_hash), None)
        if duplicate:
            doc['duplicate_content_of'] = duplicate['id']
            doc['text'], doc['sections'] = '', []
        documents.append(doc)
        if len(documents) % 10 == 0:
            log.info('%d documents (%d registry records), %d URLs queued, %d requests',
                     len(documents), sum(d['document_type'] == 'registry_metadata' for d in documents),
                     len(queue), client.request_count - started_requests)
        by_final[canonical] = doc
        return doc

    # Direct release records have first priority. Current-family documentation can
    # still be reached via guides; this collector preserves the resolver's interval.
    for release in records:
        if release.get('record_url'):
            add(release['record_url'], selected, 'resolver_release_record', [release['version']], priority=0)
        if release['source_type'] == 'pypi_release_catalog':
            add(selected.removesuffix('/json') + '/' + quote(release['raw_version'], safe='') + '/json',
                selected, 'registry_version', [release['version']], priority=1)
        elif release['source_type'] == 'npm_release_catalog':
            add(selected.rstrip('/') + '/' + quote(release['raw_version'], safe=''), selected,
                'registry_version', [release['version']], priority=1)
    add(selected, relation='selected_catalog', priority=1)
    if identity.get('homepage'):
        add(identity['homepage'], selected, 'resolver_homepage', priority=2)
        home = urlsplit(identity['homepage'])
        if home.hostname in project_hosts:
            add(f'{home.scheme}://{home.netloc}/sitemap.xml', identity['homepage'], 'sitemap_hint', priority=2)
    for repo in sorted(repositories):
        root = 'https://api.github.com/repos/' + repo
        add(root + '/releases?per_page=100&page=1', selected, 'repository_releases', priority=2)
        add(root + '/contents', selected, 'repository_document_index', priority=4)
        query = f'repo:{repo} "{resolution["input"]["target_version"]}" "upgrade"'
        add('https://api.github.com/search/issues?' + urlencode({'q': query, 'per_page': 20}),
            selected, 'repository_scoped_search', priority=6)
    # A catalog without release pages (a package registry) leaves the milestone notes to the repository's
    # tagged releases. The tag name is a guess until the fetch succeeds.
    for repo in sorted(repositories):
        for release in records:
            if not release.get('record_url') and parse_endpoint(release['version']) in milestones:
                add(f"https://github.com/{repo}/releases/tag/v{release['raw_version']}", selected,
                    'milestone_release_tag_guess', [release['version']], priority=0)
    # Project-site pages the resolver met while identifying the project, such as a release announcement.
    for item in identity.get('evidence', []):
        url = item.get('value') if item.get('field') == 'search_result' else item.get('url')
        if isinstance(url, str) and url.startswith('http') and on_project_site(urlsplit(url).hostname):
            add(url, selected, 'resolver_identity_evidence', priority=1)
    for url in extra_urls:
        add(url, relation='user_supplied_supporting_hint', supporting=True, priority=4)

    # Search results are discovery hints, not authority evidence.
    actual_search_provider = 'none'
    if search != 'none':
        query = f'"{resolution["input"]["project"]}" {resolution["input"]["current_version"]} to {resolution["input"]["target_version"]} migration upgrade deprecation'
        try:
            response = search_web(client, query, provider=search)
            actual_search_provider = response.provider
            warnings.extend(response.warnings)
            for item in response.results:
                add(item.url, response.source_url, 'search_hint', supporting=True, priority=7)
        except FetchError as exc:
            actual_search_provider = 'unavailable'
            failures.append({'operation': 'web_search', 'query': query, 'error': str(exc)})

    def budget_left():
        # Per-version registry records are small metadata with their own cap, so they cannot crowd out documentation.
        registry = sum(d['document_type'] == 'registry_metadata' for d in documents)
        return len(documents) - registry < max_documents

    while queue and budget_left():
        if not client.offline and client.request_count >= getattr(client, 'max_requests', float('inf')):
            warnings.append('HTTP request budget exhausted; remaining URLs are pending, not failed retrievals')
            break
        _, _, node = heapq.heappop(queue)
        url = node['url']
        if url in visited:
            continue
        visited.add(url)
        retrieval = convert(url)
        if retrieval is None:
            skipped.append({'url': url, 'reason': 'Source tag is not a published release-note document'})
            continue
        if 'registry_version' in {p['relation'] for p in node['discovered_from']} and \
                sum(d['document_type'] == 'registry_metadata' for d in documents) >= max_registry_documents:
            skipped.append({'url': url, 'reason': 'Registry metadata budget'})
            continue
        try:
            if not allowed(retrieval):
                skipped.append({'url': url, 'reason': 'robots.txt disallows retrieval or is unavailable'})
                continue
            api = urlsplit(retrieval).hostname in ('api.github.com', 'pypi.org', 'registry.npmjs.org')
            page = client.get(retrieval, accept='application/json' if api else 'text/html')
            if page['final_url'] != retrieval and not allowed(page['final_url']):
                skipped.append({'url': url, 'reason': 'Redirect destination disallowed for further processing'})
                continue
            body, links, feeds = page['body'], [], []
            if api:
                data = json.loads(body)
                if isinstance(data, dict) and isinstance(data.get('items'), list):
                    if data.get('incomplete_results') or data.get('total_count', 0) > len(data['items']):
                        warnings.append(f'GitHub search coverage is incomplete: {url}')
                    for item in data['items'][:20]:
                        add(item['html_url'], url, 'repository_search_hint', depth=node['depth'] + 1, priority=9)
                    continue
                if isinstance(data, list):
                    if index_count >= max_index_pages:
                        skipped.append({'url': url, 'reason': 'API index budget'})
                        continue
                    index_count += 1
                    for item in data:
                        if 'tag_name' in item:
                            if item.get('draft') or item.get('prerelease'):
                                continue
                            matching = [r['version'] for r in records if r['raw_version'] == item['tag_name'] or numeric_version(item['tag_name']) == parse_endpoint(r['version'])]
                            if matching and item.get('url'):
                                add(item['url'], url, 'release_tag_match', matching, priority=1)
                        elif item.get('type') in ('file', 'dir') and TOPICS.search(item.get('name', '')):
                            add(item['url'], url, 'repository_document', depth=node['depth'] + 1, priority=4)
                    match = re.search(r'<([^>]+)>; rel="next"', page.get('headers', {}).get('link', ''))
                    if match:
                        add(match.group(1), url, 'api_pagination', priority=5)
                    continue
                if not isinstance(data, dict):
                    raise ValueError('Expected a JSON document or index')
                canonical = data.get('html_url') or page['final_url']
                metadata = {}
                if 'tag_name' in data:
                    kind, title, text = 'github_release', data.get('name') or data['tag_name'], data.get('body') or ''
                    metadata = {k: data.get(k) for k in ('tag_name', 'published_at', 'draft', 'prerelease')}
                elif 'number' in data and ('state' in data or 'pull_request' in data):
                    kind = 'github_pull_request' if '/pulls/' in retrieval or 'pull_request' in data else 'github_issue'
                    title, text = data.get('title', url), data.get('body') or ''
                    metadata = {k: data.get(k) for k in ('state', 'state_reason', 'merged_at', 'closed_at', 'author_association', 'created_at', 'updated_at')}
                    metadata['release_inclusion_verified'] = False
                elif data.get('encoding') == 'base64' and 'content' in data:
                    text = base64.b64decode(data['content']).decode('utf-8', errors='replace')
                    title = data.get('name', url)
                    kind = classify(canonical, title, text)
                    metadata = {'git_blob_sha': data.get('sha'), 'historical_revision_verified': False}
                else:
                    kind, title = 'registry_metadata', data.get('name') or data.get('info', {}).get('name', url)
                    if urlsplit(retrieval).hostname not in ('pypi.org', 'registry.npmjs.org'):
                        raise ValueError('Unsupported GitHub API response')
                    text = json.dumps(data, ensure_ascii=False, sort_keys=True)
                    metadata = {'metadata_only': True}
                sections, links = markdown_document(text, canonical)
                if kind in ('github_release', 'github_issue', 'github_pull_request'):
                    repo_path = re.search(r'/repos/([^/]+/[^/]+)/', retrieval)
                    if repo_path:
                        links.extend((f'https://github.com/{repo_path.group(1)}/issues/{n}', 'referenced issue')
                                     for n in re.findall(r'(?<![\w/])#(\d+)\b', text))
                emit(node, page, title, text, sections, kind, metadata, canonical)
            elif body.lstrip().startswith('<?xml') or re.search(r'<(?:urlset|sitemapindex|rss|feed)\b', body[:500]):
                if index_count >= max_index_pages:
                    skipped.append({'url': url, 'reason': 'Sitemap/feed index budget'})
                    continue
                index_count += 1
                kind, locations = xml_links(body)
                for link in locations:
                    if kind == 'sitemapindex' or TOPICS.search(link):
                        add(link, url, 'sitemap' if kind in ('urlset', 'sitemapindex') else 'feed', depth=node['depth'] + 1, priority=link_priority(link))
                continue
            else:
                if re.search(r'anomaly\.js|id=["\']challenge-form|cf-chl-|Just a moment\.\.\.', body[:20000], re.I):
                    raise ValueError('Bot challenge, not document content')
                title, text, sections, links, feeds = html_document(body, page['final_url'])
                if not text.strip():
                    raise ValueError('Empty extracted document')
                if outside_scope(page['final_url'], title):
                    skipped.append({'url': url, 'reason': 'Document title or destination outside requested interval and baseline'})
                    continue
                emit(node, page, title, text, sections, classify(url, title, text))
                if release_notes_rank(page['final_url'], title) is not None:
                    milestone_siblings(page['final_url'], node['depth'])
            for feed in feeds:
                if not node['supporting']:
                    add(feed, url, 'feed_discovery', depth=node['depth'] + 1, priority=7)
            for link, label in links:
                parsed = urlsplit(link)
                if not TOPICS.search(label + ' ' + parsed.path) and not re.search(r'github.com/[^/]+/[^/]+/(issues|pull)/\d+', link):
                    continue
                same_site = on_project_site(parsed.hostname) or github_repository(link) in repositories
                # External context is followed once; it cannot recursively confer trust.
                if not same_site and node['supporting']:
                    continue
                linked_versions = sorted({v for proof in node['discovered_from'] for v in proof['versions']})
                priority = link_priority(link, label, same_site)
                add(link, url, 'linked_context', associated=linked_versions, depth=node['depth'] + 1, priority=priority,
                    supporting=not same_site)
        except (FetchError, ValueError, KeyError, TypeError) as exc:
            if 'Host deferred for this run' in str(exc):
                deferred_urls.append(url)
            else:
                failures.append({'url': url, 'retrieval_url': retrieval, 'error': str(exc)})

    if deferred_urls:
        warnings.append(f'{len(deferred_urls)} URLs pending because their host was deferred after rate limiting or repeated access failures')

    # Final association pass incorporates links discovered after a URL was fetched.
    for doc in documents:
        for alias in doc['aliases']:
            for proof in nodes[alias]['discovered_from']:
                if proof not in doc['discovered_from']:
                    doc['discovered_from'].append(proof)
        for proof in doc['discovered_from']:
            for v in proof['versions']:
                hint = {'version': v, 'basis': proof['relation'], 'status': 'unreviewed'}
                if hint not in doc['version_associations']:
                    doc['version_associations'].append(hint)
    # Materialize documents in every associated version bucket. Keep the canonical
    # inventory for provenance/deduplication, but consumers need not join IDs.
    by_id = {doc['id']: doc for doc in documents}
    documents_by_version = {version: [] for version in versions}
    unassigned_document_ids = []
    for doc in documents:
        associated = {a['version'] for a in doc['version_associations']} & set(versions)
        if not associated:
            unassigned_document_ids.append(doc['id'])
        content = by_id.get(doc.get('duplicate_content_of'), doc)
        for version in versions:
            if version in associated:
                documents_by_version[version].append({
                    **doc, 'text': content['text'], 'sections': content['sections'],
                    'matching_associations': [a for a in doc['version_associations'] if a['version'] == version],
                })
    coverage = {v: {'document_ids': [d['id'] for d in documents if any(a['version'] == v for a in d['version_associations'])],
                    'applicability_reviewed': False} for v in versions}
    for value in coverage.values():
        value['status'] = 'candidate_evidence_collected' if value['document_ids'] else 'no_evidence_collected'
        value['document_types'] = sorted({d['document_type'] for d in documents if d['id'] in value['document_ids']})
        value['not_collected_types'] = sorted({'release_notes', 'upgrade_guide', 'api_documentation',
            'github_release', 'github_issue', 'github_pull_request', 'deprecation_notice', 'registry_metadata', 'blog_post'} - set(value['document_types']))
    return {'schema_version': '1.1', 'generated_at': datetime.now(timezone.utc).isoformat(),
            'input': resolution['input'], 'resolver_status': resolution['status'], 'resolver_coverage': resolution.get('coverage'),
            'selected_catalog': selected, 'status': 'partial',
            'coverage_note': 'Bounded collection cannot establish exhaustive coverage. Version mentions and links are not verified applicability.',
            'documents_by_version': documents_by_version, 'unassigned_document_ids': unassigned_document_ids,
            'documents': documents, 'version_coverage': coverage, 'failures': failures, 'skipped': skipped,
            'warnings': warnings, 'pending_urls': list(dict.fromkeys(deferred_urls + [node['url'] for _, _, node in queue])),
            'limits': {'max_documents': max_documents, 'max_registry_documents': max_registry_documents, 'max_depth': max_depth,
                       'max_urls': max_urls, 'max_index_pages': max_index_pages},
            'source_inventory': list(client.sources.values()), 'network_requests': client.request_count - started_requests,
            'llm_calls': 0, 'search_provider': actual_search_provider, 'search_requested': search}
