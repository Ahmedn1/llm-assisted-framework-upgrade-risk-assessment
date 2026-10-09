from urllib.parse import urlsplit

from conftest import FakeClient
from upgrade_resolver.models import Candidate
from upgrade_resolver.resolver import resolve


def orbit_handler(url, accept):
    path = urlsplit(url).path
    repository = {"name": "orbit.js", "full_name": "acme/orbit.js", "html_url": "https://github.com/acme/orbit.js", "description": "Orbit.js framework", "homepage": "https://orbitjs.org"}
    if url == "https://registry.npmjs.org/orbit/latest":
        return {"name": "orbit", "description": "A web framework", "repository": {"url": "git+https://github.com/acme/orbit.js.git"}, "homepage": "https://orbitjs.org"}
    if url == "https://registry.npmjs.org/orbit":
        return {"versions": {v: {} for v in ["12.0.0", "12.5.0", "13.0.0", "13.2.0", "14.0.0", "14.0.1", "15.0.0-rc.1"]}}
    if path == "/-/v1/search":
        return {"objects": []}
    if path == "/search/repositories":
        return {"items": [repository]}
    if path == "/repos/acme/orbit.js":
        return repository
    if url == "https://orbitjs.org":
        return '<title>Orbit.js</title><a href="https://github.com/acme/orbit.js">Source</a>'


def test_unseen_project_resolves_dynamically_with_only_three_inputs():
    client = FakeClient(orbit_handler)
    result = resolve(client, "Orbit.js", "12", "14", web_search="none")
    assert result["status"] == "resolved"
    assert result["identity"]["id"] == "github:acme/orbit.js"
    assert result["selected_catalog"] == "https://registry.npmjs.org/orbit"
    assert result["documentation_milestones"] == ["13", "14"]
    assert len(result["versions"]) == 4
    assert result["llm_calls"] == 0
    assert all(r["source_url"] in client.sources for r in result["versions"])


def test_missing_endpoint_is_partial_not_success():
    result = resolve(FakeClient(orbit_handler), "Orbit.js", "12", "20", web_search="none")
    assert result["status"] == "partial"
    assert not result["endpoint_checks"]["target_found"]


def test_all_discovery_failures_return_unresolved_with_reasons():
    result = resolve(FakeClient(lambda *_: None), "Unknown", "1", "2", web_search="none")
    assert result["status"] == "unresolved"
    assert result["identity"] is None
    assert result["warnings"]
    assert result["open_questions"]


def test_tied_identities_are_not_arbitrarily_selected(monkeypatch):
    candidates = [Candidate("github", "orbit", "https://github.com/" + repo, repository=repo, score=8) for repo in ("a/orbit", "b/orbit")]
    monkeypatch.setattr("upgrade_resolver.resolver.Discoverer.discover", lambda *args: candidates)
    result = resolve(FakeClient(lambda *_: None), "Orbit", "1", "2", web_search="none")
    assert result["status"] == "ambiguous"
    assert result["versions"] == []


def test_web_challenge_does_not_block_api_discovery():
    def handler(url, accept):
        if "duckduckgo.com" in url:
            return '<form id="challenge-form">Bot challenge</form>'
        return orbit_handler(url, accept)
    result = resolve(FakeClient(handler), "Orbit.js", "12", "14", web_search="duckduckgo")
    assert result["status"] == "resolved"
    assert any("bot challenge" in w for w in result["warnings"])


def test_repo_rename_is_resolved_using_api_response():
    def handler(url, accept):
        if url == "https://registry.npmjs.org/orbit/latest":
            return {"name": "orbit", "description": "Orbit.js framework", "repository": "https://github.com/old/orbit.js", "homepage": "https://orbitjs.org"}
        if url == "https://api.github.com/repos/old/orbit.js":
            return {"full_name": "acme/orbit.js"}
        return orbit_handler(url, accept)
    result = resolve(FakeClient(handler), "Orbit.js", "12", "14", web_search="none")
    assert result["status"] == "resolved"
    assert result["identity"]["id"] == "github:acme/orbit.js"
    assert any(e["value"] == "acme/orbit.js" and "old/orbit.js" in e["url"] for e in result["identity"]["evidence"])


def test_complete_registry_catalog_beats_a_truncated_release_list():
    # GitHub's release listing stops after ~1,000 entries: page 1 holds only recent patches, page 2 fails.
    def handler(url, accept):
        if "/repos/acme/orbit.js/releases" in url:
            if "page=1" in url:
                recent = [{"tag_name": f"v14.0.{n}", "html_url": f"https://github.com/acme/orbit.js/releases/v14.0.{n}"} for n in range(2, 101)]
                return [{"tag_name": "v12.9.0", "html_url": "https://github.com/acme/orbit.js/releases/v12.9.0"}] + recent
            return None  # HTTP 404/422: enumeration incomplete
        if "/repos/acme/orbit.js/tags" in url:
            return []
        return orbit_handler(url, accept)
    result = resolve(FakeClient(handler), "Orbit.js", "12", "14", web_search="none")
    assert result["selected_catalog"] == "https://registry.npmjs.org/orbit"
    assert [r["version"] for r in result["versions"]] == ["13.0.0", "13.2.0", "14.0.0", "14.0.1"]
