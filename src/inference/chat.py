"""OpenAI-compatible Chat Completions with locally validated structured output."""
from copy import deepcopy
from dataclasses import dataclass
import json
import math
import re
from typing import Any, Generic, Literal, TypeVar

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ValidationError
from referencing.exceptions import Unresolvable

from .transport import ModelConfig, ModelOutputError, ModelRefusalError, ModelTransport

T = TypeVar("T")
StructuredMode = Literal["json_schema", "json_object", "prompt"]


@dataclass
class LLMResponse(Generic[T]):
    text: str
    parsed: T | None
    model: str
    usage: dict[str, Any]
    finish_reason: str
    request_id: str | None
    response_id: str | None


def _strict_schema(schema: dict) -> dict:
    """Close object shapes for strict decoding; reject open dictionaries explicitly."""
    result = deepcopy(schema)

    def visit(node):
        if not isinstance(node, dict):
            return
        if node.get("type") == "object" or "properties" in node:
            additional = node.get("additionalProperties")
            if additional is True or isinstance(additional, dict):
                raise ValueError("Strict structured output cannot use open dictionaries. Use explicit fields or strict_schema=False for a supporting provider.")
            node["additionalProperties"] = False
            node["required"] = list(node.get("properties", {}))
        node.pop("default", None)
        # Single-value Literals serialize as const; strict decoders support enum more consistently.
        if "const" in node and "enum" not in node:
            node["enum"] = [node.pop("const")]
        # Only traverse schema positions; never rewrite example/default values.
        for key in ("properties", "$defs", "definitions", "patternProperties", "dependentSchemas"):
            for child in node.get(key, {}).values():
                visit(child)
        for key in ("items", "additionalProperties", "not", "if", "then", "else", "contains"):
            visit(node.get(key))
        for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            for child in node.get(key, []):
                visit(child)

    visit(result)
    return result


def _reject_external_refs(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("$ref", "$dynamicRef") and isinstance(child, str) and not child.startswith("#"):
                raise ValueError("Only local JSON Schema references are supported; bundle external schemas first.")
            _reject_external_refs(child)
    elif isinstance(value, list):
        for child in value:
            _reject_external_refs(child)


def _reject_constant(value):
    raise ValueError(f"Non-JSON numeric constant: {value}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


class LLMClient(ModelTransport):
    def complete(self, messages: list[dict[str, str]], *, output_model: type[BaseModel] | None = None,
                 json_schema: dict | None = None, schema_name: str = "result",
                 structured_mode: StructuredMode = "json_schema", strict_schema: bool = True,
                 max_output_tokens: int | None = None,
                 token_parameter: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens",
                 temperature: float | None = None, reasoning_effort: str | None = None,
                 extra_body: dict | None = None) -> LLMResponse:
        if not messages or any(not isinstance(m, dict) or set(m) != {"role", "content"}
                               or m["role"] not in ("system", "developer", "user", "assistant")
                               or not isinstance(m["content"], str) for m in messages):
            raise ValueError("messages must contain text-only role/content objects")
        if output_model is not None and json_schema is not None:
            raise ValueError("Supply output_model or json_schema, not both")
        if structured_mode not in ("json_schema", "json_object", "prompt"):
            raise ValueError("Unsupported structured_mode")
        if token_parameter not in ("max_completion_tokens", "max_tokens"):
            raise ValueError("Unsupported token_parameter")
        if max_output_tokens is not None and (isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens <= 0):
            raise ValueError("max_output_tokens must be a positive integer")
        if temperature is not None and (not math.isfinite(temperature) or not 0 <= temperature <= 2):
            raise ValueError("temperature must be finite and between 0 and 2")
        if output_model is not None and (not isinstance(output_model, type) or not issubclass(output_model, BaseModel)):
            raise ValueError("output_model must be a Pydantic BaseModel class")
        schema = output_model.model_json_schema() if output_model is not None else deepcopy(json_schema)
        validator = None
        prepared_messages = deepcopy(messages)
        body = {"model": self.config.model, "messages": prepared_messages, "stream": False}
        if schema is not None:
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise ValueError("Structured output requires a JSON Schema with an object root")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", schema_name):
                raise ValueError("schema_name must contain 1–64 letters, digits, underscores, or hyphens")
            _reject_external_refs(schema)
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError:
                raise ValueError("Invalid JSON Schema") from None
            if structured_mode == "json_schema" and strict_schema:
                schema = _strict_schema(schema)
            validator = Draft202012Validator(schema, format_checker=FormatChecker())
            if structured_mode == "json_schema":
                body["response_format"] = {"type": "json_schema", "json_schema": {"name": schema_name, "strict": strict_schema, "schema": schema}}
            else:
                if structured_mode == "json_object":
                    body["response_format"] = {"type": "json_object"}
                prepared_messages.append({"role": "user", "content": "Return only a JSON object matching this JSON Schema, without Markdown fences:\n" + json.dumps(schema)})
        elif structured_mode != "json_schema":
            raise ValueError("A schema or output_model is required for structured output modes")
        if max_output_tokens is not None:
            body[token_parameter] = max_output_tokens
        if temperature is not None:
            body["temperature"] = temperature
        if reasoning_effort is not None:
            body["reasoning_effort"] = reasoning_effort
        extra = deepcopy(extra_body or {})
        protected = {"model", "messages", "stream", "response_format", "n", "max_tokens", "max_completion_tokens", "temperature", "reasoning_effort", "tools", "tool_choice", "functions", "function_call"}
        if protected.intersection(extra):
            raise ValueError("extra_body cannot override core request fields, enable tools, or request multiple/streamed completions")
        body.update(extra)
        # Validate JSON serialization before sending, including provider extensions.
        try:
            json.dumps(body, allow_nan=False)
        except (ValueError, TypeError):
            raise ValueError("Request body must be JSON serializable with finite numbers") from None
        payload, request_id = self.post("chat/completions", body)
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ModelOutputError("Expected exactly one completion choice")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ModelOutputError("Completion has no assistant message")
        if message.get("refusal") or choice.get("finish_reason") == "content_filter":
            raise ModelRefusalError("Provider refused or filtered the requested output")
        if choice.get("finish_reason") != "stop":
            raise ModelOutputError("Completion did not finish normally; output may be truncated or contain tool calls")
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            raise ModelOutputError("Completion has no text content; reasoning-only output is not a final answer")
        parsed = None
        if validator is not None:
            try:
                parsed = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
                json.dumps(parsed, allow_nan=False)
            except ValueError:
                raise ModelOutputError("Completion is not a valid, unambiguous JSON object") from None
            try:
                error = next(validator.iter_errors(parsed), None)
            except Unresolvable:
                raise ModelOutputError("Output schema contains an unresolvable local reference") from None
            if error is not None:
                raise ModelOutputError(f"Completion failed JSON Schema validation ({error.validator})")
            if output_model is not None:
                try:
                    parsed = output_model.model_validate_json(text, strict=True)
                except ValidationError:
                    raise ModelOutputError("Completion failed Pydantic model validation") from None
        usage = payload.get("usage") or {}
        if not isinstance(usage, dict):
            raise ModelOutputError("Usage must be an object")
        return LLMResponse(text=text, parsed=parsed, model=payload.get("model") or self.config.model,
                           usage=usage, finish_reason=choice["finish_reason"], request_id=request_id,
                           response_id=payload.get("id"))


def call_llm(messages: list[dict[str, str]], *, config: ModelConfig, **kwargs) -> LLMResponse:
    """One-shot helper. Reuse LLMClient as a context manager for multiple calls."""
    with LLMClient(config) as client:
        return client.complete(messages, **kwargs)
