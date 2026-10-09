import pytest
from conftest import FakeClient
from upgrade_resolver.http import FetchError
from web_search import search_web


DDG = '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Forbit.org%2Freleases">Orbit releases</a>'


def test_auto_without_key_uses_duckduckgo(monkeypatch):
    monkeypatch.delenv('BRAVE_SEARCH_API_KEY', raising=False)
    client = FakeClient(lambda *_: DDG)
    result = search_web(client, 'Orbit 1 to 2')
    assert result.provider == 'duckduckgo'
    assert result.results[0].url == 'https://orbit.org/releases'
    assert len(client.calls) == 1


def test_brave_success_does_not_fallback(monkeypatch):
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY', 'test')
    client = FakeClient(lambda *_: {'web': {'results': [{'url': 'https://orbit.org', 'title': 'Orbit'}]}})
    result = search_web(client, 'Orbit')
    assert result.provider == 'brave' and result.results[0].title == 'Orbit'
    assert len(client.calls) == 1


@pytest.mark.parametrize('failure', [FetchError('HTTP 401'), FetchError('HTTP 429'), FetchError('timeout'), {'error': 'unavailable'}, {'web': {'results': 'invalid'}}, '{invalid'])
def test_unavailable_brave_falls_back(monkeypatch, failure):
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY', 'test')
    client = FakeClient(lambda url, _: failure if 'brave.com' in url else DDG)
    result = search_web(client, 'Orbit')
    assert result.provider == 'duckduckgo' and result.warnings
    assert len(client.calls) == 2


def test_empty_brave_results_are_valid(monkeypatch):
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY', 'test')
    client = FakeClient(lambda *_: {'web': {'results': []}})
    assert search_web(client, 'Orbit').results == []
    assert len(client.calls) == 1


def test_dual_failure_preserves_reason(monkeypatch):
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY', 'test')
    client = FakeClient(lambda url, _: FetchError('HTTP 403') if 'brave.com' in url else '<form id="challenge-form"></form>')
    with pytest.raises(FetchError, match='Brave unavailable.*bot challenge'):
        search_web(client, 'Orbit')


def test_none_does_not_fetch():
    client = FakeClient(lambda *_: None)
    assert search_web(client, 'Orbit', provider='none').provider == 'none'
    assert not client.calls


def test_explicit_brave_offline_uses_cached_response_without_key(monkeypatch):
    monkeypatch.delenv('BRAVE_SEARCH_API_KEY', raising=False)
    client = FakeClient(lambda *_: {'web': {'results': []}})
    client.offline = True
    assert search_web(client, 'Orbit', provider='brave').provider == 'brave'
