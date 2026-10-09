import os
from argparse import Namespace
from contextlib import ExitStack

import pytest

from upgrade_pipeline.models import (D1_MODEL, D1_REVISION, ModelRegistry, resolve_decision, resolve_llm)

PROJECT = ['--project', 'Orbit', '--current-version', '1', '--target-version', '2']


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith(('LLM_', 'DECISION_')):
            monkeypatch.delenv(name)


def set_env(monkeypatch, **values):
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_stage_llm_values_override_generic_ones(monkeypatch):
    set_env(monkeypatch, LLM_MODEL_NAME='general', LLM_BASE_URL='https://api.one/v1', LLM_API_KEY='k1',
            LLM_EXTRACTION_MODEL_NAME='strong')
    extraction, collection = resolve_llm('extraction'), resolve_llm('collection')
    assert (extraction.model, extraction.base_url, extraction.api_key) == ('strong', 'https://api.one/v1', 'k1')
    assert extraction.sources == {'model': 'LLM_EXTRACTION_MODEL_NAME', 'base_url': 'LLM_BASE_URL', 'api_key': 'LLM_API_KEY'}
    assert collection.model == 'general'


def test_separate_stage_endpoint_never_borrows_the_generic_model_or_key(monkeypatch):
    set_env(monkeypatch, LLM_MODEL_NAME='general', LLM_BASE_URL='https://api.one/v1', LLM_API_KEY='k1',
            LLM_JUDGE_BASE_URL='https://api.two/v1')
    with pytest.raises(ValueError, match='LLM_JUDGE_MODEL_NAME'):
        resolve_llm('judge')
    monkeypatch.setenv('LLM_JUDGE_MODEL_NAME', 'critic')
    judge = resolve_llm('judge')
    assert (judge.model, judge.base_url, judge.api_key) == ('critic', 'https://api.two/v1', None)  # k1 is not sent to api.two
    monkeypatch.setenv('LLM_JUDGE_API_KEY', 'k2')
    assert resolve_llm('judge').api_key == 'k2'


def test_precedence_is_stage_arg_then_stage_env_then_generic_arg_then_generic_env(monkeypatch):
    set_env(monkeypatch, LLM_MODEL_NAME='generic-env', LLM_BASE_URL='https://api/v1')
    args = Namespace(llm_model='generic-arg', llm_base_url=None, extraction_llm_model=None, extraction_llm_base_url=None)
    assert resolve_llm('extraction', args).model == 'generic-arg'
    monkeypatch.setenv('LLM_EXTRACTION_MODEL_NAME', 'stage-env')
    assert resolve_llm('extraction', args).model == 'stage-env'
    args.extraction_llm_model = 'stage-arg'
    assert resolve_llm('extraction', args).model == 'stage-arg'


def test_missing_llm_names_the_variables_and_legacy_name_still_works(monkeypatch):
    with pytest.raises(ValueError, match='LLM_MODEL_NAME and LLM_BASE_URL'):
        resolve_llm('extraction')
    set_env(monkeypatch, LLM_MODEL='old-style', LLM_BASE_URL='https://api/v1')
    settings = resolve_llm('extraction')
    assert settings.model == 'old-style' and settings.sources['model'] == 'LLM_MODEL (deprecated)'
    from inference import ModelConfig
    assert ModelConfig.from_env().model == 'old-style'
    monkeypatch.setenv('LLM_MODEL_NAME', 'new-style')
    assert ModelConfig.from_env().model == 'new-style'


def test_decision_model_defaults_to_pinned_local_d1():
    settings = resolve_decision('relevance')
    assert (settings.kind, settings.model, settings.revision, settings.base_url) == ('d1', D1_MODEL, D1_REVISION, None)
    assert settings.sources['backend'] == 'default'


def test_decision_base_url_selects_jev_and_stage_groups_override(monkeypatch):
    monkeypatch.setenv('DECISION_BASE_URL', 'https://decide/v1')
    with pytest.raises(ValueError, match='needs a model and base URL'):
        resolve_decision('scorer')
    set_env(monkeypatch, DECISION_MODEL_NAME='jev-latest', DECISION_API_KEY='dk')
    scorer = resolve_decision('scorer')
    assert (scorer.kind, scorer.model, scorer.api_key) == ('jev', 'jev-latest', 'dk')
    assert scorer.sources['backend'] == 'inferred from DECISION_BASE_URL'
    monkeypatch.setenv('DECISION_JUDGE_BACKEND', 'd1')  # judge stays local while other stages use jev
    judge = resolve_decision('judge')
    assert (judge.kind, judge.base_url, judge.api_key) == ('d1', None, None)


def test_unpinned_custom_d1_model_is_refused(monkeypatch):
    monkeypatch.setenv('DECISION_RELEVANCE_MODEL_NAME', 'someone/fork-of-d1')
    with pytest.raises(ValueError, match='pin a reviewed commit'):
        resolve_decision('relevance')
    monkeypatch.setenv('DECISION_RELEVANCE_MODEL_REVISION', 'abc123')
    assert resolve_decision('relevance').revision == 'abc123'
    assert resolve_decision('judge').model == D1_MODEL  # other stages unaffected


def test_descriptions_never_contain_keys(monkeypatch):
    set_env(monkeypatch, LLM_MODEL_NAME='m', LLM_BASE_URL='https://api/v1', LLM_API_KEY='secret-key')
    settings = resolve_llm('extraction')
    assert 'secret-key' not in repr(settings) and 'secret-key' not in str(settings.describe())
    assert settings.describe()['api_key_set'] is True


def test_registry_opens_each_distinct_model_once(monkeypatch):
    loads = []
    monkeypatch.setattr('upgrade_pipeline.models.local.load_d1',
                        lambda model, revision, *, offline=False: loads.append((model, offline)) or object())
    with ExitStack() as stack:
        registry = ModelRegistry(stack, offline=True)
        assert registry.decision(resolve_decision('relevance')) is registry.decision(resolve_decision('judge'))
        assert loads == [(D1_MODEL, True)]
        monkeypatch.setenv('LLM_MODEL_NAME', 'm')
        monkeypatch.setenv('LLM_BASE_URL', 'https://api/v1')
        with pytest.raises(ValueError, match='network'):
            registry.llm(resolve_llm('extraction'))


class FakeClient:
    instances = []

    def __init__(self, config):
        self.config = config
        FakeClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


@pytest.fixture
def cli(monkeypatch):
    """Run the pipeline CLI with fake model clients and capture what run_case receives."""
    from upgrade_pipeline.cli import main
    FakeClient.instances = []
    seen, loads = {}, []
    monkeypatch.setattr('inference.LLMClient', FakeClient)
    monkeypatch.setattr('inference.JevClient', FakeClient)
    monkeypatch.setattr('upgrade_pipeline.models.local.load_d1',
                        lambda model, revision, *, offline=False: loads.append(offline) or FakeClient(model))
    monkeypatch.setattr('upgrade_pipeline.cli.run_case',
                        lambda item, directory, **kw: seen.update(kw, **vars(kw['postprocess'])) or {'summary': {'pipeline_status': 'completed_partial'}})
    def call(*extra):
        seen.clear(), loads.clear()
        FakeClient.instances = []
        return main(PROJECT + list(extra))
    call.seen, call.loads = seen, loads
    return call


def test_default_workflow_is_heuristic_collection_d1_scoring_llm_extraction_d1_verification(cli, monkeypatch, tmp_path, capsys):
    set_env(monkeypatch, LLM_MODEL_NAME='extractor', LLM_BASE_URL='https://api/v1')
    assert cli('--output-dir', str(tmp_path)) == 0
    seen = cli.seen
    assert seen['scorer'] == 'heuristic' and seen['decision_backend'] is None and seen['llm_client'] is None
    assert seen['relevance_backend'] is not None and seen['extraction_llm'].config.model == 'extractor'
    assert seen['verification']['kind'] == 'decision' and seen['verification']['judge'].backend is seen['relevance_backend']
    assert cli.loads == [False]  # one d1 instance for relevance and verification
    assert set(seen['model_settings']) == {'relevance', 'extraction', 'judge'}
    assert all('secret' not in str(v) for v in seen['model_settings'].values())
    err = capsys.readouterr().err
    assert f'relevance uses d1 {D1_MODEL}@{D1_REVISION[:12]}' in err and 'extraction uses llm extractor' in err


def test_offline_skips_default_llm_steps_but_rejects_explicit_ones(cli, tmp_path, capsys):
    assert cli('--output-dir', str(tmp_path), '--offline') == 0
    assert cli.seen['extraction_llm'] is None and cli.seen['verification'] is None and cli.loads == [True]
    assert 'risk extraction and verification skipped' in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli('--output-dir', str(tmp_path), '--offline', '--extract-risks')


def test_missing_llm_for_a_default_step_explains_how_to_skip_it(cli, tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli('--output-dir', str(tmp_path))
    err = capsys.readouterr().err
    assert 'LLM_MODEL_NAME and LLM_BASE_URL' in err and '--no-extract-risks' in err
    assert cli('--output-dir', str(tmp_path), '--no-extract-risks') == 0 and cli.seen['verification'] is None


def test_stage_specific_arguments_and_separate_judge_endpoint(cli, monkeypatch, tmp_path):
    set_env(monkeypatch, LLM_MODEL_NAME='general', LLM_BASE_URL='https://api/v1',
            LLM_JUDGE_MODEL_NAME='critic', LLM_JUDGE_BASE_URL='https://other/v1', LLM_JUDGE_API_KEY='k2')
    assert cli('--output-dir', str(tmp_path), '--path', 'llm', '--extraction-llm-model', 'strong',
               '--verify-judge', 'llm', '--no-section-relevance') == 0
    seen = cli.seen
    assert seen['llm_client'].config.model == 'general' and seen['extraction_llm'].config.model == 'strong'
    critic = seen['verification']['judge'].llm
    assert (critic.config.model, critic.config.base_url) == ('critic', 'https://other/v1')
    assert seen['model_settings']['judge']['resolved_from']['base_url'] == 'LLM_JUDGE_BASE_URL'


def test_scorer_and_relevance_can_use_different_decision_backends(cli, monkeypatch, tmp_path):
    set_env(monkeypatch, DECISION_SCORER_BACKEND='jev', DECISION_SCORER_MODEL_NAME='jev-latest',
            DECISION_SCORER_BASE_URL='https://decide/v1')
    assert cli('--output-dir', str(tmp_path), '--scorer', 'decision', '--no-extract-risks') == 0
    assert cli.seen['decision_backend'].config.model == 'jev-latest'
    assert cli.seen['relevance_backend'] is not cli.seen['decision_backend'] and cli.loads == [False]
    with pytest.raises(SystemExit):
        cli('--output-dir', str(tmp_path), '--no-extract-risks', '--verify-risks')
