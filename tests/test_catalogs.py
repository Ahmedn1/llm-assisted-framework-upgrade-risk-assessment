from conftest import FakeClient
from upgrade_resolver.catalogs import CatalogReader
from upgrade_resolver.http import FetchError
from upgrade_resolver.models import Candidate


def test_empty_github_releases_fall_back_to_tags():
    def handler(url, accept):
        return [{"name": "REL_2_0"}, {"name": "REL_1_0"}, {"name": "nightly"}] if "/tags?" in url else []
    catalogs = CatalogReader(FakeClient(handler)).read(Candidate("github", "orbit", "https://github.com/acme/orbit", repository="acme/orbit"), "1", "2", "family")
    assert [c.source_type for c in catalogs] == ["github_releases", "github_tags"]
    assert [r.version for r in catalogs[1].releases] == ["2.0", "1.0"]
    assert catalogs[1].skipped_labels == ["nightly"]
    assert "tags?" in catalogs[1].releases[0].source_url
    assert catalogs[1].warnings


def test_pagination_limit_does_not_claim_complete_catalog():
    client = FakeClient(lambda url, accept: [{"name": f"v1.0.{i}"} for i in range(100)])
    catalog = CatalogReader(client, max_pages=1).github("acme/orbit", "tags")
    assert not catalog.enumeration_complete
    assert "limit" in catalog.warnings[1]


def test_github_follows_all_pages():
    def handler(url, accept):
        return [{"name": f"v1.0.{i}"} for i in range(100)] if url.endswith("&page=1") else [{"name": "v0.9.0"}]
    client = FakeClient(handler)
    catalog = CatalogReader(client).github("acme/orbit", "tags")
    assert catalog.enumeration_complete
    assert len(catalog.releases) == 101
    assert len(client.calls) == 2


def test_failed_page_retains_results_and_marks_partial():
    def handler(url, accept):
        return [{"name": f"v1.0.{i}"} for i in range(100)] if url.endswith("&page=1") else FetchError("HTTP 429")
    catalog = CatalogReader(FakeClient(handler)).github("acme/orbit", "tags")
    assert len(catalog.releases) == 100
    assert not catalog.enumeration_complete


def test_draft_and_flagged_prerelease_are_excluded_even_with_numeric_tag():
    client = FakeClient(lambda url, accept: [{"tag_name": "2.0", "draft": True}, {"tag_name": "1.9", "prerelease": True}, {"tag_name": "1.8"}])
    catalog = CatalogReader(client).github("acme/orbit", "releases")
    assert [r.version for r in catalog.releases] == ["1.8"]


def test_html_crawler_follows_release_index_and_preserves_evidence():
    def handler(url, accept):
        if url == "https://orbit.org":
            return '<title>Orbit</title><a href="/releases/">Release notes</a>'
        if url == "https://orbit.org/releases/":
            return '<a href="/release-2">Release 2.0</a><a href="/release-3">Release 3.0 beta 1</a>'
    catalog = CatalogReader(FakeClient(handler), max_html_pages=2).html("https://orbit.org", "Orbit")
    assert [r.version for r in catalog.releases] == ["2.0"]
    assert catalog.releases[0].source_url == "https://orbit.org/releases/"
    assert catalog.releases[0].record_url == "https://orbit.org/release-2"
    assert not catalog.enumeration_complete


def test_pypi_yanked_versions_are_retained_and_marked():
    data = {"releases": {"1.0": [{"yanked": True}], "1.1": [], "2.0": [{"yanked": False}]}}
    candidate = Candidate("pypi", "orbit", "https://pypi.org/pypi/orbit/json", metadata=data)
    catalog = CatalogReader(FakeClient(lambda *_: None)).pypi(candidate)
    assert catalog.releases[0].yanked
    assert len(catalog.releases) == 2
