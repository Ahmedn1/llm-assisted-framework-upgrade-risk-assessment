from upgrade_resolver.discovery import github_repository, rank_identities, score_candidate
from upgrade_resolver.models import Candidate


def test_same_named_client_is_penalized():
    candidate = Candidate("pypi", "Orbit", "https://pypi.org/pypi/Orbit/json", "Python client for Orbit", homepage="https://orbit.org")
    score_candidate(candidate, "Orbit")
    assert candidate.score < 6


def test_shared_repository_does_not_authorize_a_companion_package():
    candidate = Candidate("npm", "orbit-env", "https://registry.npmjs.org/orbit-env", "Orbit.js environment helpers",
                          "acme/orbit.js", "https://orbitjs.org", metadata={"reciprocal_link": True})
    score_candidate(candidate, "Orbit.js")
    assert not candidate.metadata["component_match"]
    assert candidate.score < 6


def test_generic_js_package_alias_needs_other_identity_evidence():
    candidate = Candidate("npm", "orbit", "https://registry.npmjs.org/orbit", "A web framework", "acme/orbit.js", "https://orbitjs.org")
    score_candidate(candidate, "Orbit.js")
    assert candidate.metadata["component_match"]
    assert candidate.score >= 6


def test_repeated_sources_do_not_inflate_identity_score():
    candidates = [Candidate("github", "Orbit", f"https://github.com/acme/orbit/{i}", repository="acme/orbit", score=7) for i in range(3)]
    assert rank_identities(candidates)[0]["score"] == 7


def test_unrelated_packages_not_grouped_by_claimed_homepage():
    candidates = [Candidate("npm", name, f"https://registry.npmjs.org/{name}", homepage="https://orbit.org", score=6) for name in ("orbit", "orbit-client")]
    assert len(rank_identities(candidates)) == 2


def test_repository_url_formats():
    assert github_repository("git+https://github.com/acme/orbit.git") == "acme/orbit"
    assert github_repository("git@github.com:acme/orbit.git") == "acme/orbit"
    assert github_repository("https://github.com/acme/orbit/tree/main/docs") == "acme/orbit"
    assert github_repository("https://github.com.evil.test/acme/orbit") == ""
    assert github_repository("https://github.com/topics/orbit") == ""
