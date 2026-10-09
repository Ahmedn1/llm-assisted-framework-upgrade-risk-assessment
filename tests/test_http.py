import json
import httpx
import pytest

from upgrade_resolver.http import FetchError, HttpClient, validate_public_url


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1", "http://[::1]", "http://10.0.0.1", "http://localhost", "http://user:password@example.org", "http://example.org:8080"])
def test_non_public_urls_are_rejected(url):
    with pytest.raises(FetchError):
        validate_public_url(url, resolve_dns=False)


def test_cache_roundtrip_and_offline_miss(tmp_path, monkeypatch):
    monkeypatch.setattr("upgrade_resolver.http.socket.getaddrinfo", lambda *args: [(None, None, None, None, ("93.184.216.34", 443))])
    client = HttpClient(tmp_path)
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"versions": ["1", "2"]})))
    assert client.json("https://example.org/releases") == {"versions": ["1", "2"]}
    client.close()
    offline = HttpClient(tmp_path, offline=True)
    assert offline.json("https://example.org/releases") == {"versions": ["1", "2"]}
    assert offline.request_count == 0
    with pytest.raises(FetchError, match="Offline cache miss"):
        offline.json("https://example.org/other")
    assert offline.sources["https://example.org/releases"]["from_cache"]
    offline.close()


def test_tampered_cache_is_rejected(tmp_path):
    import hashlib
    url = "https://example.org/releases"
    key = hashlib.sha256((url + "\napplication/json").encode()).hexdigest()
    (tmp_path / (key + ".json")).write_text(json.dumps({"url": url, "body": "modified", "content_sha256": "wrong"}))
    client = HttpClient(tmp_path, offline=True)
    with pytest.raises(FetchError, match="integrity"):
        client.get(url)
    client.close()


def test_github_token_is_not_forwarded_to_redirect_destination(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr("upgrade_resolver.http.socket.getaddrinfo", lambda *args: [(None, None, None, None, ("93.184.216.34", 443))])
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://example.org/releases"}) if len(requests) == 1 else httpx.Response(200, json=[])
    client = HttpClient(tmp_path)
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    client.get("https://api.github.com/repos/acme/orbit")
    assert requests[0].headers["authorization"] == "Bearer test-token"
    assert "authorization" not in requests[1].headers
    client.close()


def test_conditional_refresh_reuses_body_and_preserves_retrieval_time(tmp_path, monkeypatch):
    monkeypatch.setattr('upgrade_resolver.http.socket.getaddrinfo', lambda *args: [(None, None, None, None, ('93.184.216.34', 443))])
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, text='original', headers={'etag': '"v1"'}) if len(requests) == 1 else httpx.Response(304)
    first = HttpClient(tmp_path)
    first.client.close(); first.client = httpx.Client(transport=httpx.MockTransport(respond))
    original = first.get('https://example.org/doc'); first.close()
    second = HttpClient(tmp_path, refresh=True)
    second.client.close(); second.client = httpx.Client(transport=httpx.MockTransport(respond))
    refreshed = second.get('https://example.org/doc'); second.close()
    assert requests[1].headers['if-none-match'] == '"v1"'
    assert refreshed['body'] == 'original'
    assert refreshed['retrieved_at'] == original['retrieved_at']
    assert refreshed['validated_at']


def test_collector_does_not_retry_before_long_retry_after(tmp_path, monkeypatch):
    from evidence_collector.transport import CollectionHttpClient
    monkeypatch.setattr('upgrade_resolver.http.socket.getaddrinfo', lambda *args: [(None, None, None, None, ('93.184.216.34', 443))])
    client = CollectionHttpClient(tmp_path, min_interval=0)
    client.client.close(); client.client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={'retry-after': '120'})))
    with pytest.raises(FetchError, match='deferred'):
        client.get('https://example.org/doc')
    assert client.request_count == 1
    client.close()


@pytest.mark.parametrize('status,headers,retries,requests', [(403, {'x-ratelimit-remaining': '0'}, 2, 1), (429, {}, 1, 2), (403, {}, 0, 3)])
def test_collection_defers_host_but_preserves_cache(tmp_path, monkeypatch, status, headers, retries, requests):
    from evidence_collector.transport import CollectionHttpClient
    monkeypatch.setattr('upgrade_resolver.http.socket.getaddrinfo', lambda *args: [(None, None, None, None, ('93.184.216.34', 443))])
    monkeypatch.setattr('evidence_collector.transport.time.sleep', lambda _: None)
    client = CollectionHttpClient(tmp_path, min_interval=0, retries=retries)
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text='cached') if r.url.path == '/cached' else httpx.Response(status, headers=headers)))
    client.get('https://example.org/cached')
    for i in range(4):
        with pytest.raises(FetchError):
            client.get(f'https://example.org/fail/{i}')
    assert client.request_count == requests + 1
    assert client.get('https://example.org/cached')['body'] == 'cached'
    assert client.request_count == requests + 1
    client.close()
