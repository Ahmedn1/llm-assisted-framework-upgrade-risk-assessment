import pytest
from inference import LayaClient, ModelOutputError, ScoreQuestion


class Agent:
    def __init__(self, truncated=False):
        self.truncated = truncated

    def predict(self, state, questions):
        return {'model': 'native', 'usage': {'input_tokens': 10, 'output_tokens': 0, 'truncated': self.truncated},
                'answers': {'authority': {'type': 'score', 'score': 0.8, 'confidence': 0.2781,
                                         'probabilities': {'0': 0.2, '1': 0.8},
                                         'legend': {'0': 'low', '1': 'high'}, 'answer_confidence': 0.8,
                                         'action': {'act_probability': 0.7}}}}


def test_laya_preserves_native_extensions_and_scores():
    result = LayaClient(Agent(), model_name='pinned').decide({}, {'authority': ScoreQuestion(criteria=['low', 'high'])})
    assert result.backend == 'laya' and result.model == 'pinned'
    assert result.answers['authority'].score == 0.8
    assert result.provider_metadata['answer_extensions']['authority']['answer_confidence'] == 0.8


def test_laya_rejects_truncated_evidence():
    with pytest.raises(ModelOutputError, match='truncated'):
        LayaClient(Agent(True), model_name='pinned').decide({}, {'authority': ScoreQuestion(criteria=['low', 'high'])})


def test_laya_rejects_probability_inconsistent_scores():
    agent = Agent()
    original = agent.predict
    def predict(*args):
        result = original(*args)
        result['answers']['authority']['score'] = 0.4
        return result
    agent.predict = predict
    with pytest.raises(ModelOutputError, match='inconsistent'):
        LayaClient(agent, model_name='pinned').decide({}, {'authority': ScoreQuestion(criteria=['low', 'high'])})
