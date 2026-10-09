import json
import pytest
from conftest import FakeClient
from evidence_collector import collect
from evidence_collector.extraction import xml_links


def resolution(records=None, selected='https://orbit.org/releases/'):
    return {'status': 'partial', 'input': {'project': 'Orbit', 'current_version': '1', 'target_version': '2', 'version_mode': 'family'},
            'selected_catalog': selected, 'coverage': {'catalog': 'partial'},
            'identity': {'homepage': 'https://orbit.org/'}, 'candidates': [],
            'versions': records or [{'version': '2.0', 'raw_version': '2.0', 'source_type': 'html_release_index',
                                    'record_url': 'https://orbit.org/releases/2.0/', 'source_url': selected}]}


def handler(url, accept):
    if url.endswith('/robots.txt'):
        return 'User-agent: *\nAllow: /'
    if url.endswith('/releases/2.0/'):
        return '<title>Orbit 2.0</title><nav>NOISE</nav><main><h1>Orbit 2.0</h1><h2 id="migration">Migration</h2><p>Remove the deprecated option.</p><a href="/docs/2/api/">API documentation</a><a href="/release/0.1/">Release 0.1</a></main>'
    if url.endswith('/docs/2/api/'):
        return '<title>Orbit API</title><main><p>Function reference.</p></main>'
    if url in ('https://orbit.org/', 'https://orbit.org/releases/'):
        return '<title>Orbit</title><main><a href="/releases/2.0/">Release 2.0</a></main>'


def test_release_extraction_keeps_anchors_and_unreviewed_applicability():
    client = FakeClient(handler)
    result = collect(client, resolution(), max_documents=1)
    doc = result['documents'][0]
    assert doc['document_type'] == 'release_notes'
    assert 'NOISE' not in doc['text']
    assert any(s['url'].endswith('#migration') for s in doc['sections'])
    assert doc['applicability'] == 'unreviewed'
    assert doc['version_associations']
    assert not doc['authority_inherited']
    assert result['resolver_status'] == 'partial' and result['llm_calls'] == 0
    assert result['pending_urls']


def test_repeated_url_keeps_multiple_version_links_once():
    r = resolution()
    r['versions'].append({**r['versions'][0], 'version': '2.1', 'raw_version': '2.1'})
    result = collect(FakeClient(handler), r, max_documents=1)
    assert len(result['documents']) == 1
    assert {a['version'] for a in result['documents'][0]['version_associations']} == {'2.0', '2.1'}
    groups = result['documents_by_version']
    assert groups['2.0'][0]['id'] == groups['2.1'][0]['id']
    assert groups['2.0'][0]['text'] == groups['2.1'][0]['text']
    assert groups['2.0'][0]['text']
    assert all(a['version'] == '2.1' for a in groups['2.1'][0]['matching_associations'])


def test_old_release_links_are_not_fetched():
    client = FakeClient(handler)
    result = collect(client, resolution())
    assert not any('/release/0.1/' in url for url, _ in client.calls)
    assert any('outside requested' in s['reason'] for s in result['skipped'])


def test_robot_disallow_prevents_document_request():
    client = FakeClient(lambda url, _: 'User-agent: *\nDisallow: /' if url.endswith('robots.txt') else '<p>forbidden</p>')
    result = collect(client, resolution())
    assert not result['documents']
    assert all(url.endswith('robots.txt') for url, _ in client.calls)


def test_unresolved_input_is_not_promoted_to_trusted_seed():
    r = resolution(); r['status'] = 'ambiguous'
    with pytest.raises(ValueError, match='ambiguity'):
        collect(FakeClient(handler), r)


def test_missing_pages_are_gaps_not_proof_of_absence():
    result = collect(FakeClient(lambda *_: None), resolution())
    assert result['failures']
    assert result['version_coverage']['2.0']['status'] == 'no_evidence_collected'
    assert result['documents_by_version']['2.0'] == []
    assert result['status'] == 'partial'


def test_issue_keeps_state_without_claiming_release_inclusion():
    r = resolution();r['versions'][0]['record_url'] = 'https://github.com/acme/orbit/issues/17'
    def route(url, accept):
        if url == 'https://api.github.com/repos/acme/orbit/issues/17':
            return {'number': 17, 'state': 'closed', 'title': 'Regression in 2.0', 'body': 'Possibly fixed.',
                    'html_url': 'https://github.com/acme/orbit/issues/17', 'state_reason': 'completed'}
        return handler(url, accept)
    doc = collect(FakeClient(route), r, max_documents=1)['documents'][0]
    assert doc['document_type'] == 'github_issue'
    assert not doc['metadata']['release_inclusion_verified']
    assert doc['metadata']['state'] == 'closed'
    assert doc['publisher_relationship'] == 'repository_discussion_not_release_confirmation'


def test_sitemap_does_not_fetch_external_entities():
    with pytest.raises(ValueError, match='entities'):
        xml_links('<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><urlset/>')


def test_registry_version_endpoint_is_direct_and_version_associated():
    selected = 'https://pypi.org/pypi/orbit/json'
    r = resolution([{'version': '2.0', 'raw_version': '2.0', 'source_type': 'pypi_release_catalog', 'source_url': selected}], selected)
    client = FakeClient(lambda url, _: {'info': {'name': 'orbit', 'version': '2.0'}} if url.endswith('/2.0/json') else None)
    doc = collect(client, r, max_documents=1)['documents'][0]
    assert doc['document_type'] == 'registry_metadata'
    assert doc['version_associations'][0]['version'] == '2.0'


def test_identical_content_stored_once_without_merging_publisher_authority():
    r = resolution();r['versions'].append({**r['versions'][0], 'version': '2.1', 'record_url': 'https://community.org/article'})
    client = FakeClient(lambda url, _: 'User-agent: *\nAllow: /' if url.endswith('/robots.txt') else '<title>Same</title><p>Identical text.</p>')
    result = collect(client, r, max_documents=2)
    docs = result['documents']
    assert docs[1]['duplicate_content_of'] == docs[0]['id']
    assert docs[1]['text'] == ''
    assert docs[1]['publisher_relationship'] == 'supporting_context'
    grouped = result['documents_by_version']['2.1'][0]
    assert grouped['text'] == docs[0]['text']
    assert grouped['publisher_relationship'] == 'supporting_context'
    assert grouped['duplicate_content_of'] == docs[0]['id']


def test_base_href_and_nested_section_ids_are_preserved():
    from evidence_collector.extraction import html_document
    body = '<base href="/docs/2/"><title>Release</title><main><div id="migration"><div><h2>Upgrade</h2></div><p>Read <a href="upgrading.html">this</a>.</p></div></main>'
    _, _, sections, links, _ = html_document(body, 'https://orbit.org/releases/2.0/')
    assert links[0][0] == 'https://orbit.org/docs/2/upgrading.html'
    assert sections[0]['url'] == 'https://orbit.org/releases/2.0/#migration'


def test_github_release_and_repository_changelog_use_native_apis():
    import base64
    r = resolution(); r['identity']['repository'] = 'acme/orbit'
    r['versions'][0]['record_url'] = 'https://github.com/acme/orbit/releases/tag/v2.0'
    r['versions'][0]['raw_version'] = 'v2.0'
    def route(url, accept):
        root = 'https://api.github.com/repos/acme/orbit'
        if url == root + '/releases/tags/v2.0':
            return {'tag_name': 'v2.0', 'name': 'Orbit 2.0', 'body': 'See #9 for migration details.',
                    'html_url': 'https://github.com/acme/orbit/releases/tag/v2.0', 'draft': False}
        if url == root + '/contents':
            return [{'name': 'CHANGELOG.md', 'type': 'file', 'url': root + '/contents/CHANGELOG.md'}]
        if url == root + '/contents/CHANGELOG.md':
            return {'name': 'CHANGELOG.md', 'encoding': 'base64', 'content': base64.b64encode(b'# 2.0\nRemoved legacy API.').decode(),
                    'sha': 'blob', 'html_url': 'https://github.com/acme/orbit/blob/main/CHANGELOG.md'}
        if url == root + '/issues/9':
            return {'number': 9, 'title': 'Migration', 'body': 'Discussion.', 'state': 'open', 'html_url': 'https://github.com/acme/orbit/issues/9'}
        return handler(url, accept)
    result = collect(FakeClient(route), r)
    types = {d['document_type'] for d in result['documents']}
    assert {'github_release', 'github_issue', 'release_notes'} <= types
    changelog = next(d for d in result['documents'] if d['metadata'].get('git_blob_sha'))
    assert not changelog['metadata']['historical_revision_verified']


def test_sitemap_and_feed_discover_supporting_pages():
    def route(url, accept):
        if url == 'https://orbit.org/sitemap.xml':
            return '<?xml version="1.0"?><urlset><url><loc>https://orbit.org/docs/2/migration</loc></url></urlset>'
        if url == 'https://orbit.org/docs/2/migration':
            return '<title>Migration</title><p>Upgrade instructions for 2.0.</p><link type="application/atom+xml" href="/feed.xml">'
        if url == 'https://orbit.org/feed.xml':
            return '<?xml version="1.0"?><feed><entry><link href="https://orbit.org/blog/upgrade-2"/></entry></feed>'
        if url == 'https://orbit.org/blog/upgrade-2':
            return '<title>Upgrade 2.0</title><p>Guidance.</p>'
        return handler(url, accept)
    result = collect(FakeClient(route), resolution(), max_depth=3)
    assert any(d['url'].endswith('/blog/upgrade-2') for d in result['documents'])


def test_invalid_xml_is_recorded_as_a_failure():
    def route(url, accept):
        return '<?xml version="1.0"?><urlset>' if url.endswith('sitemap.xml') else handler(url, accept)
    result = collect(FakeClient(route), resolution())
    assert any('Malformed sitemap' in f['error'] for f in result['failures'])


def test_future_project_blog_and_title_are_excluded():
    def route(url, accept):
        if url.endswith('/releases/2.0/'):
            return '<title>Orbit 2.0</title><a href="/blog/orbit-3-upgrade-guide">Upgrade</a><a href="/upgrade">Upgrade</a><a href="/blog/orbit-2-upgrade-guide">Upgrade</a>'
        if url.endswith('/upgrade'):
            return '<title>Orbit 3 Upgrade Guide</title><p>Future release</p>'
        if url.endswith('/blog/orbit-2-upgrade-guide'):
            return '<title>Orbit 2 Upgrade Guide</title><p>In range</p>'
        return handler(url, accept)
    client = FakeClient(route)
    result = collect(client, resolution())
    assert not any('orbit-3' in url for url, _ in client.calls)
    assert not any(d['title'] == 'Orbit 3 Upgrade Guide' for d in result['documents'])
    assert any(d['title'] == 'Orbit 2 Upgrade Guide' for d in result['documents'])


def test_guides_precede_issue_expansion():
    def route(url, accept):
        if url.endswith('/releases/2.0/'):
            return '<title>Orbit 2.0</title><a href="https://github.com/acme/orbit/issues/1">Issue</a><a href="/upgrade">Upgrade</a>'
        if url.endswith('/upgrade'):
            return '<title>Orbit 2 Upgrade</title><p>Migration instructions</p>'
        return handler(url, accept)
    r = resolution(); r['identity']['repository'] = 'acme/orbit'
    client = FakeClient(route)
    result = collect(client, r, max_documents=3)
    assert any(d['title'] == 'Orbit 2 Upgrade' for d in result['documents'])
    assert not any('/issues/1' in url for url, _ in client.calls)


def test_discussion_links_cannot_fill_documentation_queue():
    def route(url, accept):
        if url.endswith('/releases/2.0/'):
            return '<title>Orbit 2.0</title>' + ''.join(f'<a href="https://github.com/acme/orbit/issues/{i}">Issue</a>' for i in range(50)) + '<a href="/upgrade">Upgrade</a>'
        if url.endswith('/upgrade'):
            return '<title>Orbit 2 Upgrade</title><p>Instructions</p>'
        return handler(url, accept)
    result = collect(FakeClient(route), resolution(), max_urls=20)
    assert any(d['title'] == 'Orbit 2 Upgrade' for d in result['documents'])
    assert any(s['reason'].startswith('Discussion URL budget') for s in result['skipped'])


def test_supporting_site_robots_does_not_expand_its_sitemap():
    def route(url, accept):
        if url == 'https://external.org/robots.txt':
            return 'User-agent: *\nAllow: /\nSitemap: https://external.org/sitemap.xml'
        if url == 'https://external.org/upgrade':
            return '<title>Supporting upgrade advice</title><p>Context</p>'
        return handler(url, accept)
    client = FakeClient(route)
    collect(client, resolution(), extra_urls=['https://external.org/upgrade'])
    assert not any(url == 'https://external.org/sitemap.xml' for url, _ in client.calls)


def test_collector_uses_shared_search_fallback(monkeypatch):
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY', 'test')
    def route(url, accept):
        if 'brave.com' in url:
            return None
        if 'duckduckgo.com' in url:
            return '<a class="result__a" href="https://orbit.org/upgrade">Upgrade</a>'
        if url.endswith('/upgrade'):
            return '<title>Orbit 2 Upgrade</title><p>Migration instructions</p>'
        return handler(url, accept)
    result = collect(FakeClient(route), resolution(), search='auto')
    assert result['search_provider'] == 'duckduckgo'
    assert any('Brave unavailable' in w for w in result['warnings'])
    assert any(d['url'] == 'https://orbit.org/upgrade' for d in result['documents'])


def docs_site_handler(url, accept):
    if url.endswith('/robots.txt'):
        return 'User-agent: *\nAllow: /'
    if url in ('https://www.orbit.org/', 'https://orbit.org/releases/'):
        return '<title>Orbit</title><main><a href="https://docs.orbit.org/en/dev/releases/2.1.3/">Release notes 2.1.3</a></main>'
    if url == 'https://docs.orbit.org/en/dev/releases/2.1.3/':
        return '<title>Orbit 2.1.3 release notes</title><main><p>Fixes a crash.</p></main>'
    if url == 'https://docs.orbit.org/en/dev/releases/2.0/':
        return '<title>Orbit 2.0 release notes</title><main><h2>Backwards incompatible changes</h2><p>USE_X now defaults to True.</p></main>'


def test_milestone_notes_are_derived_from_patch_notes_on_a_sibling_docs_host():
    r = {**resolution(selected='https://orbit.org/releases/'), 'documentation_milestones': ['2'],
         'identity': {'homepage': 'https://www.orbit.org/'}}
    r['versions'] = [{**r['versions'][0], 'record_url': None}]
    result = collect(FakeClient(docs_site_handler), r)
    urls = {d['url']: d for d in result['documents']}
    milestone = urls['https://docs.orbit.org/en/dev/releases/2.0/']  # never linked; derived from the 2.1.3 notes URL
    assert milestone['publisher_relationship'] == 'selected_project_site'  # docs.orbit.org belongs to orbit.org
    assert any(p['relation'] == 'milestone_sibling_guess' for p in milestone['discovered_from'])
    assert any(f['url'] == 'https://docs.orbit.org/en/dev/releases/2/' for f in result['failures'])  # unverified guess recorded


def test_registry_records_have_their_own_budget():
    records = [{'version': f'2.0.{i}', 'raw_version': f'2.0.{i}', 'source_type': 'pypi_release_catalog', 'source_url': 'x'}
               for i in range(5)]
    selected = 'https://pypi.org/pypi/orbit/json'
    def handler(url, accept):
        if url.startswith('https://pypi.org/pypi/orbit/2.0.'):
            return {'info': {'name': 'orbit', 'version': url.split('/')[-2]}}
        return None
    result = collect(FakeClient(handler), {**resolution(records, selected), 'identity': {}}, max_documents=1, max_registry_documents=2)
    assert sum(d['document_type'] == 'registry_metadata' for d in result['documents']) == 2
    assert sum(s['reason'] == 'Registry metadata budget' for s in result['skipped']) == 3


def test_milestone_mentions_use_title_and_url_short_forms_only():
    from evidence_collector.extraction import milestone_mentions
    versions = ['13.0.0', '13.0.1', '14.0.0']
    assert milestone_mentions('Upgrading: Version 14', 'https://x.org/docs/upgrading/version-14', versions, ['13', '14']) == ['14.0.0']
    assert milestone_mentions('Blog', 'https://x.org/blog/next-13', versions, ['13', '14']) == ['13.0.0']
    assert milestone_mentions('Next 13.4', 'https://x.org/blog/next-13-4', versions, ['13', '14']) == []
    assert milestone_mentions('API reference', 'https://x.org/docs/api', versions, ['13', '14']) == []


def test_registry_catalog_seeds_milestone_tags_and_resolver_pages():
    r = resolution([{'version': v, 'raw_version': v, 'source_type': 'npm_release_catalog', 'record_url': None,
                     'source_url': 'https://registry.npmjs.org/orbit'} for v in ('2.0.0', '2.0.1')],
                   selected='https://registry.npmjs.org/orbit')
    r['documentation_milestones'] = ['2']
    r['identity'] = {'homepage': 'https://orbit.org/', 'repository': 'orbit/orbit',
                     'evidence': [{'url': 'https://duckduckgo.example/?q=orbit', 'field': 'search_result', 'value': 'https://orbit.org/blog/orbit-2'},
                                  {'url': 'https://elsewhere.example/orbit', 'field': 'title', 'value': 'Orbit'}]}

    def pages(url, accept):
        if url.endswith('/robots.txt'):
            return 'User-agent: *\nAllow: /'
        if url == 'https://api.github.com/repos/orbit/orbit/releases/tags/v2.0.0':
            return {'tag_name': 'v2.0.0', 'name': 'v2.0.0', 'body': '## Breaking\n- Removed the legacy flag.', 'html_url': 'https://github.com/orbit/orbit/releases/tag/v2.0.0'}
        if url == 'https://orbit.org/blog/orbit-2':
            return '<title>Orbit 2</title><main><h1>Orbit 2</h1><p>The legacy flag is removed.</p></main>'
    client = FakeClient(pages)
    result = collect(client, r)
    by_url = {d['url']: d for d in result['documents']}
    tag = next(d for u, d in by_url.items() if 'releases/tag' in u)
    assert '2.0.0' in {a['version'] for a in tag['version_associations']}
    blog = by_url['https://orbit.org/blog/orbit-2']
    assert {('2.0.0', 'milestone_mention')} <= {(a['version'], a['basis']) for a in blog['version_associations']}
    assert not any('elsewhere.example' in u for u, _ in client.calls)


def test_upgrade_guides_keep_a_reserve_of_the_url_budget():
    def pages(url, accept):
        if url.endswith('/robots.txt'):
            return 'User-agent: *\nAllow: /'
        if url in ('https://orbit.org/', 'https://orbit.org/releases/'):
            links = ''.join(f'<a href="/docs/api/{i}/">API docs {i}</a>' for i in range(10))
            return f'<title>Orbit</title><main>{links}<a href="/docs/upgrading/version-2/">Upgrading: Version 2</a></main>'
        return '<title>Orbit page</title><main><p>Text.</p></main>'
    r = resolution(); r['documentation_milestones'] = ['2']
    client = FakeClient(pages)
    collect(client, r, max_urls=6)
    assert any(u.endswith('/docs/upgrading/version-2/') for u, _ in client.calls)
