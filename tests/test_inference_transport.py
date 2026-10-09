import httpx
import pytest

from inference import LLMClient, ModelConfig, ModelError, ModelHTTPError


def config(**kwargs):
    return ModelConfig(model="test", base_url="https://inference.example/v1", api_key="secret-token", retry_delay=0, **kwargs)


def test_retries_transient_errors_then_succeeds(monkeypatch):
    delays = []
    monkeypatch.setattr("inference.transport.time.sleep", delays.append)
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"retry-after": "2"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = LLMClient(config(), http_client=http).complete([{"role": "user", "content": "hello"}])
    assert result.text == "OK"
    assert len(requests) == 2
    assert delays == [2.0]
    assert requests[0].headers["authorization"] == "Bearer secret-token"


@pytest.mark.parametrize("status", [301, 302, 400, 401, 403, 404, 422])
def test_no_redirect_or_retry_for_configuration_errors(status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "secret-token"}, headers={"location": "https://other.example"})
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as http:
        with pytest.raises(ModelHTTPError) as exc:
            LLMClient(config(), http_client=http).post("chat/completions", {})
    assert exc.value.status_code == status
    assert len(calls) == 1
    assert "secret-token" not in str(exc.value)


def test_timeout_retries_are_bounded():
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("secret-token")
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ModelError) as exc:
            LLMClient(config(max_retries=1), http_client=http).post("chat/completions", {})
    assert len(calls) == 2
    assert "secret-token" not in str(exc.value)


def test_provider_keys_are_explicit_and_not_in_repr(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-key")
    monkeypatch.setenv("LLM_MODEL", "local-model")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert ModelConfig.from_env().api_key is None
    assert "secret-token" not in repr(config())


def test_borrowed_http_client_is_not_closed():
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))) as http:
        client = LLMClient(config(), http_client=http)
        client.close()
        assert not http.is_closed


@pytest.mark.parametrize("url", ["file:///tmp/model", "https://user:key@example.org/v1", "http://localhost:8000/v1/chat/completions", "https://example.org/v1?key=secret"])
def test_invalid_base_urls(url):
    with pytest.raises(ValueError):
        ModelConfig(model="test", base_url=url)
