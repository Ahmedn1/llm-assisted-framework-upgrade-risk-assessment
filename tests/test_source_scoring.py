import pytest

from conftest import FakeClient
from inference import DecisionResponse, ModelOutputError, ScoreAnswer
from upgrade_resolver.discovery import Discoverer
from upgrade_resolver.models import Candidate, Catalog, Release
from upgrade_resolver.resolver import resolve


class Backend:
    def __init__(self, scores):
        self.scores = scores
        self.states = []

    def decide(self, state, questions):
        self.states.append(state)
        level = self.scores[state['source']['url']]
        if isinstance(level, Exception):
            raise level
        return DecisionResponse(model='fixture', backend='fixture', usage={'input_tokens': 1, 'output_tokens': 1},
            answers={name: ScoreAnswer(type='score', score=float(level), confidence=0.9,
                legend={str(i): c for i, c in enumerate(q.criteria)},
                probabilities={str(i): float(i == level) for i in range(len(q.criteria))})
                for name, q in questions.items()})


def setup_sources(monkeypatch, *, complete=False, missing_endpoint=False):
    candidates = [Candidate('github', 'orbit', 'https://github.com/mirror/orbit', repository='mirror/orbit', score=20),
                  Candidate('html', 'Orbit releases', 'https://orbit.org/releases', score=-10, rejected=True)]
    catalogs = {c.url: Catalog(c.url, kind, enumeration_complete=complete,
        releases=[Release(v, v, c.url, kind) for v in (['2.0'] if missing_endpoint else ['1.0', '2.0'])])
        for c, kind in zip(candidates, ['github_tags', 'html_release_index'])}
    monkeypatch.setattr('upgrade_resolver.resolver.Discoverer.discover', lambda *args: candidates)
    monkeypatch.setattr('upgrade_resolver.source_scoring.CatalogReader.read', lambda self, c, *args: [catalogs[c.url]])
    return candidates, catalogs


def run(backend):
    return resolve(FakeClient(lambda *_: None), 'Orbit', '1', '2', scorer='decision', decision_backend=backend)


def test_official_html_beats_mirror_despite_heuristic_rejection(monkeypatch):
    candidates, _ = setup_sources(monkeypatch)
    backend = Backend({candidates[0].url: 2, candidates[1].url: 4})
    result = run(backend)
    assert result['selected_catalog'] == candidates[1].url
    assert result['status'] == 'partial'  # Model cannot assert HTML completeness.
    assert result['llm_calls'] == 2
    assert [v['version'] for v in result['versions']] == ['2.0']
    assert all([r['version'] for r in c['releases']] == ['2.0'] for c in result['catalogs'])
    assert all('score' not in s['source'] and 'rejected' not in s['source'] for s in backend.states)


def test_scoring_failure_never_falls_back_to_high_heuristic(monkeypatch):
    candidates, _ = setup_sources(monkeypatch)
    result = run(Backend({c.url: ModelOutputError('invalid output') for c in candidates}))
    assert result['status'] == 'unresolved'
    assert result['versions'] == []
    assert all('error' in s for s in result['scoring']['sources'])


def test_competing_unlinked_identities_abstain(monkeypatch):
    candidates, _ = setup_sources(monkeypatch)
    result = run(Backend({c.url: 4 for c in candidates}))
    assert result['status'] == 'ambiguous'
    assert result['versions'] == []


def test_missing_endpoint_stays_partial(monkeypatch):
    candidates, _ = setup_sources(monkeypatch, complete=True, missing_endpoint=True)
    result = run(Backend({candidates[0].url: 2, candidates[1].url: 4}))
    assert result['status'] == 'partial'
    assert not result['endpoint_checks']['current_found']


def test_empty_catalog_cannot_be_selected(monkeypatch):
    candidates, catalogs = setup_sources(monkeypatch)
    for catalog in catalogs.values():
        catalog.releases.clear()
    result = run(Backend({c.url: 4 for c in candidates}))
    assert result['status'] == 'unresolved'
    assert not any(s['eligible'] for s in result['scoring']['sources'])


def test_low_scores_abstain(monkeypatch):
    candidates, _ = setup_sources(monkeypatch)
    assert run(Backend({c.url: 2 for c in candidates}))['status'] == 'unresolved'


def test_decision_mode_requires_explicit_backend():
    with pytest.raises(ValueError, match='decision_backend'):
        resolve(FakeClient(lambda *_: None), 'Orbit', '1', '2', scorer='decision')


@pytest.mark.parametrize('provider', ['brave', 'duckduckgo'])
def test_only_top_five_search_results_are_visited(monkeypatch, provider):
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY', 'test')
    urls = [f'https://example.org/{i}' for i in range(8)]
    payload = {'web': {'results': [{'url': u} for u in urls]}} if provider == 'brave' else ''.join(
        f'<a class="result__a" href="{u}">result</a>' for u in urls)
    discovery = Discoverer(FakeClient(lambda *_: payload), provider)
    seen = []
    monkeypatch.setattr(discovery, 'from_url', lambda url, evidence: seen.append(url))
    discovery.search_web('Orbit', '1', '2')
    assert seen == urls[:5]


def test_authority_ranks_after_project_match_gate(monkeypatch):
    candidates, _ = setup_sources(monkeypatch)
    class UnequalBackend(Backend):
        def decide(self, state, questions):
            result = super().decide(state, questions)
            if state['source']['url'] == candidates[1].url:
                answer = result.answers['project_match']
                answer.score = 3.0
                answer.probabilities = {str(i): float(i == 3) for i in range(5)}
            else:
                answer = result.answers['authority']
                answer.score = 3.0
                answer.probabilities = {str(i): float(i == 3) for i in range(5)}
            return result
    result = run(UnequalBackend({c.url: 4 for c in candidates}))
    assert result['selected_catalog'] == candidates[1].url
    assert result['coverage']['identity'] == 'matched_by_decision_model'
