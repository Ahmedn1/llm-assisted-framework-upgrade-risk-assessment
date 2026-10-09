import httpx
import pytest

from upgrade_resolver.http import FetchError, HttpClient


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr("upgrade_resolver.http.socket.getaddrinfo", lambda *args: [(None, None, None, None, ("93.184.216.34", 443))])


def test_response_limit_is_enforced(tmp_path, public_dns):
    client = HttpClient(tmp_path, max_bytes=4)
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"12345")))
    with pytest.raises(FetchError, match="exceeds"):
        client.get("https://example.org/releases")
    assert not list(tmp_path.glob("*.json"))
    client.close()


def test_request_budget_stops_additional_requests(tmp_path, public_dns):
    client = HttpClient(tmp_path, max_requests=1)
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[])))
    client.get("https://example.org/one")
    with pytest.raises(FetchError, match="budget"):
        client.get("https://example.org/two")
    assert client.request_count == 1
    client.close()


def test_rate_limit_is_not_cached_as_an_empty_catalog(tmp_path, public_dns):
    client = HttpClient(tmp_path)
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429, json={"message": "rate limited"})))
    with pytest.raises(FetchError, match="429"):
        client.get("https://example.org/releases")
    assert not list(tmp_path.glob("*.json"))
    client.close()
