"""TypeSafe-compatible native HTTP decision client."""
import json

from pydantic import TypeAdapter, ValidationError

from ..transport import ModelTransport
from .base import DecisionBackend
from .models import DecisionResponse, Question, State


class JevClient(ModelTransport, DecisionBackend):
    """POST to a TypeSafe-compatible /systemone endpoint, not /chat/completions."""
    def decide(self, state: State, questions: dict[str, Question | dict]) -> DecisionResponse:
        typed, serialized = self.prepare_questions(questions)
        try:
            validated_state = TypeAdapter(State).validate_python(state, strict=True)
            body = {"model": self.config.model, "state": validated_state, "questions": serialized}
            json.dumps(body, allow_nan=False)
        except (ValidationError, ValueError, TypeError):
            raise ValueError("Jev state must be a string, JSON object, or JSON array with finite numbers") from None
        payload, request_id = self.post("systemone", body)
        return self.validate_decision(payload, typed, backend="typesafe", request_id=request_id)


