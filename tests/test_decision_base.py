import pytest
from inference import DecisionBackend, JevClient, OpenD1Client, LayaClient


def test_all_native_clients_share_abstract_base():
    for client in (JevClient, OpenD1Client, LayaClient):
        assert issubclass(client, DecisionBackend)
        assert client.prepare_questions is DecisionBackend.prepare_questions
        assert client.validate_decision is DecisionBackend.validate_decision
    with pytest.raises(TypeError):
        DecisionBackend()
