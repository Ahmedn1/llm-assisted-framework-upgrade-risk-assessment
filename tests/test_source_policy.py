from conftest import FakeClient
from upgrade_resolver.catalogs import CatalogReader
from upgrade_resolver.discovery import Discoverer, rank_identities
from upgrade_resolver.models import Candidate, Catalog, Evidence, Release
from upgrade_resolver.resolver import resolve
from upgrade_resolver.source_policy import catalog_coverage, annotate_agreement, source_preference


def test_preference_uses_discovery_origin_not_just_html_kind():
    search = Candidate('html', 'Orbit', 'https://orbit.org', evidence=[Evidence('search', 'search_result', 'url')])
    github = Candidate('github', 'Orbit', 'https://github.com/a/orbit')
    registry = Candidate('npm', 'Orbit', 'https://registry.npmjs.org/orbit')
    other = Candidate('html', 'Orbit', 'https://other.org')
    assert [source_preference(c) for c in (search, github, registry, other)] == [0, 1, 2, 3]


def test_same_site_pages_are_one_identity():
    candidates = [Candidate('html', 'Orbit', 'https://orbit.org/' + path, score=6) for path in ('releases', 'download')]
    assert len(rank_identities(candidates)) == 1


def test_equal_identity_scores_use_search_before_github(monkeypatch):
    web = Candidate('html', 'Orbit Releases', 'https://orbit.org/releases', score=6,
                    evidence=[Evidence('search', 'search_result', 'url')])
    repo = Candidate('github', 'Orbit', 'https://github.com/a/orbit', repository='a/orbit', score=6)
    monkeypatch.setattr('upgrade_resolver.resolver.Discoverer.discover', lambda *args: [repo, web])
    catalog = Catalog(web.url, 'html_release_index', enumeration_complete=False,
                      releases=[Release(v, v, web.url, 'html_release_index') for v in ('1.0', '2.0')])
    monkeypatch.setattr('upgrade_resolver.resolver.CatalogReader.read', lambda *args: [catalog])
    result = resolve(FakeClient(lambda *_: None), 'Orbit', '1', '2')
    assert result['selected_catalog'] == web.url
    assert result['status'] == 'partial'


def test_crawl_prioritizes_interval_and_does_not_fetch_old_releases():
    root = 'https://orbit.org/releases/'
    page = ''.join(f'<a href="/release/{v}/">Release {v}</a>' for v in ('0.01', '1.0', '13.0', '14.0', '16.0', '20.0'))
    client = FakeClient(lambda url, _: page if url == root else '')
    CatalogReader(client, max_html_pages=4).html(root, 'Orbit', '13', '16', 'family')
    visited = [u for u, _ in client.calls]
    assert all('/release/' + v + '/' not in ' '.join(visited) for v in ('0.01', '1.0', '20.0'))
    assert root in visited and 'https://orbit.org/release/14.0/' in visited


def test_provenance_collects_outbound_release_link_but_not_self_link():
    root, archive = 'https://orbit.org/', 'https://orbit.org/releases/'
    client = FakeClient(lambda url, _: f'<title>Orbit</title><a href="{archive}">Release notes</a>')
    discoverer = Discoverer(client)
    discoverer.inspect_homepage(Candidate('html', 'Orbit', root, homepage=root), 'Orbit')
    candidate = discoverer.candidates[archive]
    assert Evidence(root, 'project_release_link', archive) in candidate.evidence
    discoverer.inspect_homepage(candidate, 'Orbit')
    assert Evidence(archive, 'project_release_link', archive) not in candidate.evidence


def test_coverage_distinguishes_major_only_and_observed_agreement():
    def catalog(url, versions):
        return Catalog(url, 'html_release_index', enumeration_complete=False,
                       releases=[Release(v, v, url, 'html_release_index') for v in versions])
    coarse = catalog('a', ['14', '15', '16'])
    detailed = catalog('b', ['14.0', '14.1', '15.0', '16.0'])
    mirror = catalog('c', ['14.0', '14.1', '15.0', '16.0'])
    assert catalog_coverage(coarse, '13', '16', 'family')['granularity'] == 'endpoint_precision_only'
    rows = annotate_agreement([coarse, detailed, mirror], '13', '16', 'family')
    assert rows[1]['matching_observed_catalogs'] == ['c']
    assert rows[0]['matching_observed_catalogs'] == []
    assert not rows[1]['enumeration_complete']
