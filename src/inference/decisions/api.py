"""One-shot decision dispatch with explicit backend selection."""
from typing import Any

from ..transport import ModelConfig
from .base import DecisionBackend
from .jev import JevClient
from .models import DecisionResponse, Question


def call_decision_model(state: Any, questions: dict[str, Question | dict], *,
                        config: ModelConfig | None = None,
                        backend: DecisionBackend | None = None) -> DecisionResponse:
    """One-shot Jev HTTP call, or dispatch to an explicitly supplied native backend."""
    if (config is None) == (backend is None):
        raise ValueError("Supply either config for Jev HTTP or a decision backend")
    if backend is not None:
        return backend.decide(state, questions)
    with JevClient(config) as client:
        return client.decide(state, questions)
