from copy import deepcopy
import json

import httpx
from pydantic import BaseModel, ConfigDict, Field
import pytest

from inference import LLMClient, ModelConfig, ModelOutputError, ModelRefusalError


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    affected: bool
    source_ids: list[str]
    note: str | None = None


MESSAGES = [{"role": "user", "content": "Evaluate the supplied evidence."}]


def envelope(content='{"affected":true,"source_ids":["s1"],"note":null}', **choice_fields):
    return {"id": "test-completion", "model": "test-model", "choices": [{"message": {"content": content}, "finish_reason": "stop", **choice_fields}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8}}


def client_for(payload, requests=None):
    def handler(request):
        if requests is not None:
            requests.append(request)
        return httpx.Response(200, json=payload, headers={"x-request-id": "req-1"})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return LLMClient(ModelConfig(model="served-name", base_url="http://localhost:8000/v1", max_retries=0), http_client=http)


def test_local_openai_request_and_typed_structured_result():
    requests = []
    client = client_for(envelope(), requests)
    result = client.complete(MESSAGES, output_model=Finding, max_output_tokens=128)
    request = requests[0]
    assert str(request.url) == "http://localhost:8000/v1/chat/completions"
    assert "authorization" not in request.headers
    body = json.loads(request.content)
    assert body["model"] == "served-name"
    assert body["max_completion_tokens"] == 128
    schema = body["response_format"]["json_schema"]
    assert schema["strict"]
    assert schema["schema"]["required"] == ["affected", "source_ids", "note"]
    assert schema["schema"]["additionalProperties"] is False
    assert "default" not in schema["schema"]["properties"]["note"]
    assert "temperature" not in body
    assert isinstance(result.parsed, Finding)
    assert result.parsed.affected is True
    assert result.usage["prompt_tokens"] == 12
    assert result.request_id == "req-1"


def test_raw_schema_and_legacy_token_parameter_with_vllm_extension():
    requests = []
    client = client_for(envelope('{"choice":"archive"}'), requests)
    schema = {"type": "object", "properties": {"choice": {"enum": ["archive", "mirror"], "type": "string"}}}
    original = deepcopy(schema)
    result = client.complete(MESSAGES, json_schema=schema, token_parameter="max_tokens", max_output_tokens=64,
                             extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    body = json.loads(requests[0].content)
    assert body["max_tokens"] == 64
    assert "max_completion_tokens" not in body
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert result.parsed == {"choice": "archive"}
    assert schema == original


@pytest.mark.parametrize("mode,wire_format", [("json_object", {"type": "json_object"}), ("prompt", None)])
def test_explicit_fallback_modes_still_validate(mode, wire_format):
    requests = []
    original = deepcopy(MESSAGES)
    result = client_for(envelope(), requests).complete(MESSAGES, output_model=Finding, structured_mode=mode)
    body = json.loads(requests[0].content)
    assert body.get("response_format") == wire_format
    assert "JSON Schema" in body["messages"][-1]["content"]
    assert MESSAGES == original
    assert isinstance(result.parsed, Finding)


@pytest.mark.parametrize("text", [
    '{"affected":"true","source_ids":[],"note":null}',
    '{"affected":true,"source_ids":[]}',
    '{"affected":true,"source_ids":[],"note":null,"invented":1}',
    '```json\n{"affected":true}\n```',
    '{"affected":true,"affected":false,"source_ids":[],"note":null}',
    '{"affected":true,"source_ids":[],"note":NaN}',
    '{"affected":true,"source_ids":[],"note":1e999}',
])
def test_invalid_structured_output_is_rejected_without_repair(text):
    requests = []
    with pytest.raises(ModelOutputError):
        client_for(envelope(text), requests).complete(MESSAGES, output_model=Finding)
    assert len(requests) == 1


@pytest.mark.parametrize("finish_reason", ["length", "tool_calls", None])
def test_incomplete_or_unsupported_completion_is_rejected(finish_reason):
    with pytest.raises(ModelOutputError):
        client_for(envelope(finish_reason=finish_reason)).complete(MESSAGES, output_model=Finding)


def test_refusal_is_distinct_from_invalid_json():
    payload = envelope()
    payload["choices"][0]["message"] = {"content": None, "refusal": "Cannot answer"}
    with pytest.raises(ModelRefusalError):
        client_for(payload).complete(MESSAGES, output_model=Finding)


def test_reasoning_model_configuration_and_final_content():
    requests = []
    payload = envelope("Final answer")
    payload["choices"][0]["message"]["reasoning_content"] = "Provider reasoning"
    result = client_for(payload, requests).complete(MESSAGES, reasoning_effort="low")
    assert result.text == "Final answer"
    assert result.parsed is None
    assert json.loads(requests[0].content)["reasoning_effort"] == "low"


def test_nested_models_and_enums_validate():
    from enum import Enum
    class Kind(str, Enum):
        official = "official"
        community = "community"
    class Source(BaseModel):
        kind: Kind
    class Sources(BaseModel):
        items: list[Source]
    result = client_for(envelope('{"items":[{"kind":"official"}]}')).complete(MESSAGES, output_model=Sources)
    assert result.parsed.items[0].kind == Kind.official


def test_open_dictionaries_need_explicit_non_strict_provider_mode():
    class Map(BaseModel):
        values: dict[str, int]
    client = client_for(envelope('{"values":{"a":1}}'))
    with pytest.raises(ValueError, match="open dictionaries"):
        client.complete(MESSAGES, output_model=Map)
    assert client.complete(MESSAGES, output_model=Map, strict_schema=False).parsed.values == {"a": 1}


def test_local_custom_model_validator_runs():
    from pydantic import model_validator
    class Bounds(BaseModel):
        low: int
        high: int
        @model_validator(mode="after")
        def ordered(self):
            if self.low > self.high:
                raise ValueError("reversed")
            return self
    with pytest.raises(ModelOutputError, match="Pydantic"):
        client_for(envelope('{"low":5,"high":1}')).complete(MESSAGES, output_model=Bounds)


@pytest.mark.parametrize("field", ["model", "messages", "stream", "response_format", "n", "max_tokens", "tools"])
def test_extensions_cannot_override_core_contract(field):
    requests = []
    with pytest.raises(ValueError, match="extra_body"):
        client_for(envelope(), requests).complete(MESSAGES, extra_body={field: "override"})
    assert requests == []


def test_external_schema_references_are_rejected_before_network():
    requests = []
    schema = {"type": "object", "properties": {"item": {"$ref": "https://example.org/schema"}}}
    with pytest.raises(ValueError, match="local"):
        client_for(envelope(), requests).complete(MESSAGES, json_schema=schema)
    assert requests == []


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [{"finish_reason": "stop"}]}, envelope(" ")])
def test_malformed_completion_envelope(payload):
    with pytest.raises(ModelOutputError):
        client_for(payload).complete(MESSAGES)
