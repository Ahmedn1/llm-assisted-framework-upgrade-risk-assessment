"""Backend interface and shared question/response validation."""
import json
import math
from abc import ABC, abstractmethod
from typing import Any

from pydantic import TypeAdapter, ValidationError

from ..transport import ModelOutputError
from .models import (ChoiceAnswer, ChoiceQuestion, DecisionResponse, NoulQuestion,
                     Question, QuestionMap, ScoreAnswer)

_questions = TypeAdapter(QuestionMap)


def prepare_questions(questions: dict[str, Question | dict]) -> tuple[dict[str, Question], dict]:
    try:
        typed = _questions.validate_python(questions)
    except ValidationError:
        raise ValueError("Invalid decision questions. Supply named ChoiceQuestion, ScoreQuestion, or NoulQuestion values.") from None
    if any(not name.strip() for name in typed):
        raise ValueError("Question names must not be blank")
    for question in typed.values():
        if isinstance(question, ChoiceQuestion) and any(not name.strip() for name in question.criteria):
            raise ValueError("Choice names must not be blank")
    serialized = {name: q.model_dump(mode="json", exclude_none=True) for name, q in typed.items()}
    try:
        json.dumps(serialized, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("Questions must contain finite, JSON-serializable values") from None
    return typed, serialized


def validate_decision(payload: dict, questions: dict[str, Question], *, backend: str,
                      request_id: str | None = None) -> DecisionResponse:
    if not isinstance(payload, dict):
        raise ModelOutputError("Decision model must return an object")
    # Do not let provider fields override local provenance metadata.
    if set(payload) - {"model", "answers", "usage"}:
        raise ModelOutputError("Unexpected decision response fields")
    try:
        result = DecisionResponse.model_validate({**payload, "backend": backend, "request_id": request_id})
    except ValidationError:
        raise ModelOutputError("Decision response failed type/range validation") from None
    if set(result.answers) != set(questions):
        raise ModelOutputError("Decision answer names do not match requested question names")
    for name, question in questions.items():
        answer = result.answers[name]
        if answer.type != question.type:
            raise ModelOutputError("Decision answer type does not match its question")
        if isinstance(question, NoulQuestion):
            continue
        probabilities = answer.probabilities
        expected = set(question.criteria) if isinstance(question, ChoiceQuestion) else {str(i) for i in range(len(question.criteria))}
        if set(probabilities) != expected:
            raise ModelOutputError("Decision probabilities do not match the supplied criteria")
        if not math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-3):
            raise ModelOutputError("Decision probabilities do not sum approximately to one")
        if isinstance(answer, ChoiceAnswer):
            if answer.choice not in expected:
                raise ModelOutputError("Decision selected an option not supplied in the question")
            if answer.probabilities[answer.choice] + 1e-6 < max(probabilities.values()):
                raise ModelOutputError("Selected choice disagrees with the returned probabilities")
        elif isinstance(answer, ScoreAnswer):
            if answer.legend != {str(i): criterion for i, criterion in enumerate(question.criteria)}:
                raise ModelOutputError("Returned score legend differs from the requested rubric")
            expected_score = sum(int(level) * probability for level, probability in probabilities.items())
            if not 0 <= answer.score <= len(question.criteria) - 1 or not math.isclose(answer.score, expected_score, abs_tol=1e-3):
                raise ModelOutputError("Returned score is inconsistent with its probability distribution")
    return result


class DecisionBackend(ABC):
    """Common base for native decision clients."""
    prepare_questions = staticmethod(prepare_questions)
    validate_decision = staticmethod(validate_decision)

    @abstractmethod
    def decide(self, state: Any, questions: dict[str, Question | dict]) -> DecisionResponse:
        """Return validated native decisions for the supplied questions."""
        raise NotImplementedError


