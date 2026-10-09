from copy import deepcopy
import json

import httpx
import pytest

from inference import (
    ChoiceQuestion, JevClient, ModelConfig, ModelOutputError, NoulQuestion,
    OpenD1Client, ScoreQuestion, call_decision_model,
)


def questions():
    return {
        "source": ChoiceQuestion(instructions="Which source should be investigated?", criteria={"archive": "Project release archive", "mirror": "Repository mirror", "unknown": "Insufficient evidence"}),
        "authority": ScoreQuestion(instructions="How authoritative is this source?", criteria=["Unverified", "Project-maintained", "Canonical release archive"]),
        "same_project": NoulQuestion(instructions="Do these references identify the same project?"),
    }


def response():
    return {
        "model": "jev-version-test",
        "answers": {
            "source": {"type": "choice", "choice": "archive", "confidence": 0.7, "probabilities": {"archive": 0.8, "mirror": 0.1, "unknown": 0.1}},
            "authority": {"type": "score", "score": 1.7, "confidence": 0.8, "legend": {"0": "Unverified", "1": "Project-maintained", "2": "Canonical release archive"}, "probabilities": {"0": 0.1, "1": 0.1, "2": 0.8}},
            "same_project": {"type": "noul", "noul": 0.95},
        },
        "usage": {"input_tokens": 120, "output_tokens": 0},
    }


def client_for(payload, requests=None):
    def handler(request):
        if requests is not None:
            requests.append(request)
        return httpx.Response(200, json=payload, headers={"x-request-id": "decision-req"})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return JevClient(ModelConfig(model="jev-latest", base_url="https://api.typesafe.ai/v1", api_key="test-key", max_retries=0), http_client=http)


def test_jev_native_request_and_probabilities_preserved():
    requests = []
    result = client_for(response(), requests).decide({"title": "Release notes"}, questions())
    request = requests[0]
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    assert request.headers["authorization"] == "Bearer test-key"
    body = json.loads(request.content)
    assert set(body) == {"model", "state", "questions"}
    assert body["questions"]["same_project"]["type"] == "noul"
    assert result.answers["source"].confidence == 0.7
    assert result.answers["source"].probabilities["archive"] == 0.8
    assert result.answers["authority"].score == 1.7
    assert result.answers["same_project"].noul == 0.95
    assert result.probability_source == "native_model"
    assert result.model == "jev-version-test"
    assert result.request_id == "decision-req"


@pytest.mark.parametrize("mutation", [
    lambda p: p["answers"].pop("same_project"),
    lambda p: p["answers"].update(invented={"type": "noul", "noul": 0.5}),
    lambda p: p["answers"]["source"].update(choice="invented"),
    lambda p: p["answers"]["source"].update(choice="mirror"),
    lambda p: p["answers"]["source"].update(confidence=1.2),
    lambda p: p["answers"]["source"]["probabilities"].update(archive=0.5),
    lambda p: p["answers"]["source"]["probabilities"].update(invented=0),
    lambda p: p["answers"]["same_project"].update(noul=-1),
    lambda p: p["answers"].update(same_project={"type": "choice", "choice": "yes", "confidence": 0.5, "probabilities": {"yes": 1}}),
    lambda p: p["answers"]["authority"].update(score=0.2),
    lambda p: p["answers"]["authority"]["legend"].update({"0": "Different rubric"}),
    lambda p: p["usage"].update(input_tokens=-1),
])
def test_inconsistent_decisions_are_rejected(mutation):
    payload = response()
    mutation(payload)
    with pytest.raises(ModelOutputError):
        client_for(payload).decide("state", questions())


@pytest.mark.parametrize("bad_questions", [{}, {"q": {"type": "unknown"}}, {"q": {"type": "choice", "criteria": {}}}, {"": {"type": "noul"}}])
def test_invalid_questions_fail_before_network(bad_questions):
    requests = []
    with pytest.raises(ValueError):
        client_for(response(), requests).decide("state", bad_questions)
    assert requests == []


def test_raw_question_dictionaries_are_supported():
    raw = {name: value.model_dump(exclude_none=True) for name, value in questions().items()}
    assert client_for(response()).decide("state", raw).answers["source"].choice == "archive"


def test_open_d1_calls_native_system_one_without_chat_or_download():
    class LocalModel:
        calls = []
        def system_one(self, state, questions, **kwargs):
            self.calls.append((state, questions, kwargs))
            result = response()
            result.pop("model")
            return result
    model = LocalModel()
    backend = OpenD1Client(model, model_name="LiquidAI/d1-3B")
    state = {"text": "release notes"}
    original = deepcopy(state)
    result = call_decision_model(state, questions(), backend=backend)
    assert result.model == "LiquidAI/d1-3B"
    assert result.backend == "open_d1"
    assert result.usage.output_tokens == 0
    assert len(model.calls) == 1
    assert model.calls[0][1]["source"]["criteria"]["archive"] == "Project release archive"
    assert state == original


def test_open_d1_multimodal_arguments_pass_through():
    calls = []
    class LocalModel:
        def system_one(self, state, questions, **kwargs):
            calls.append((state, kwargs))
            return {"answers": {"visible": {"type": "noul", "noul": 0.8}}, "usage": {"input_tokens": 100, "output_tokens": 0}}
    image = object()
    backend = OpenD1Client(LocalModel(), model_name="local-d1")
    backend.decide(None, {"visible": NoulQuestion(instructions="Is an image present?")}, images=[image])
    assert calls[0][0] is None
    assert calls[0][1]["images"][0] is image


def test_native_d1_score_constraints_checked_before_inference():
    class LocalModel:
        def system_one(self, *args):
            raise AssertionError("should not run")
    with pytest.raises(ValueError, match="2–10"):
        OpenD1Client(LocalModel(), model_name="local").decide("state", {"rating": ScoreQuestion(criteria=["only"])} )


def test_backend_or_config_required_without_implicit_provider_selection():
    with pytest.raises(ValueError, match="either"):
        call_decision_model("state", questions())


def test_jev_rejects_non_json_and_scalar_state():
    for state in (None, 42, {"bad": float("nan")}):
        with pytest.raises(ValueError):
            client_for(response()).decide(state, questions())
