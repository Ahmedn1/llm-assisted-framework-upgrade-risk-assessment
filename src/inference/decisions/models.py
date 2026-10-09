"""Shared typed questions, answers, and native decision responses."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

State = str | dict[str, JsonValue] | list[JsonValue]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class TypedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ChoiceQuestion(TypedModel):
    type: Literal["choice"] = "choice"
    instructions: State | None = None
    criteria: dict[str, State | None] = Field(min_length=1)


class ScoreQuestion(TypedModel):
    type: Literal["score"] = "score"
    instructions: State | None = None
    criteria: list[State] = Field(min_length=1)


class NoulCriteria(TypedModel):
    true: State | None = None
    false: State | None = None


class NoulQuestion(TypedModel):
    """Native probability of a yes/true answer; no Boolean threshold is implied."""
    type: Literal["noul"] = "noul"
    instructions: State | None = None
    criteria: NoulCriteria | None = None


Question = Annotated[ChoiceQuestion | ScoreQuestion | NoulQuestion, Field(discriminator="type")]
QuestionMap = Annotated[dict[str, Question], Field(min_length=1)]


class ChoiceAnswer(TypedModel):
    type: Literal["choice"]
    choice: str
    confidence: Probability
    probabilities: dict[str, Probability]


class ScoreAnswer(TypedModel):
    type: Literal["score"]
    score: float = Field(allow_inf_nan=False)
    confidence: Probability
    legend: dict[str, State]
    probabilities: dict[str, Probability]


class NoulAnswer(TypedModel):
    type: Literal["noul"]
    noul: Probability


Answer = Annotated[ChoiceAnswer | ScoreAnswer | NoulAnswer, Field(discriminator="type")]


class DecisionUsage(TypedModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class DecisionResponse(TypedModel):
    model: str
    answers: dict[str, Answer] = Field(min_length=1)
    usage: DecisionUsage
    request_id: str | None = None
    backend: str
    probability_source: Literal["native_model"] = "native_model"
    provider_metadata: dict[str, JsonValue] = Field(default_factory=dict)


