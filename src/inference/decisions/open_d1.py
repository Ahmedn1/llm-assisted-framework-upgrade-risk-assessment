"""Adapter for an explicitly loaded LiquidAI d1 model."""
from copy import deepcopy
import json
from typing import Any

from pydantic import JsonValue, TypeAdapter, ValidationError

from ..transport import ModelOutputError
from .base import DecisionBackend
from .models import DecisionResponse, Question, ScoreQuestion


class OpenD1Client(DecisionBackend):
    """Wrap an already-loaded model exposing LiquidAI's system_one method.

    Loading weights, device selection, and remote-code trust belong to the caller.
    This adapter does not install Torch/Transformers or download/execute model code.
    """
    def __init__(self, model: Any, *, model_name: str):
        if not callable(getattr(model, "system_one", None)) or not model_name.strip():
            raise ValueError("Supply an initialized system_one model and its model_name")
        self.model = model
        self.model_name = model_name

    def decide(self, state: JsonValue, questions: dict[str, Question | dict], **backend_options) -> DecisionResponse:
        typed, serialized = self.prepare_questions(questions)
        for question in typed.values():
            if isinstance(question, ScoreQuestion) and not 2 <= len(question.criteria) <= 10:
                raise ValueError("Open d1 supports score rubrics with 2–10 levels")
        try:
            TypeAdapter(JsonValue).validate_python(state, strict=True)
            json.dumps(state, allow_nan=False)
        except (ValidationError, ValueError, TypeError):
            raise ValueError("Open d1 state must be a finite JSON value; pass images/audio as backend options") from None
        if {"state", "questions"}.intersection(backend_options):
            raise ValueError("backend_options cannot override state or questions")
        payload = self.model.system_one(deepcopy(state), serialized, **backend_options)
        if not isinstance(payload, dict):
            raise ModelOutputError("Open d1 system_one must return an object")
        # LiquidAI's documented envelope omits model; preserve its native answers.
        payload = {"model": self.model_name, **payload}
        return self.validate_decision(payload, typed, backend="open_d1")


