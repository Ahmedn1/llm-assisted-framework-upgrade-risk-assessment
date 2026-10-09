import logging

from upgrade_pipeline.cli import COMMANDS, main
from upgrade_pipeline.common import Progress, configure_logging, get_logger
from upgrade_pipeline.postprocess import PostprocessConfig
from upgrade_pipeline.runner import run_case
from pipeline_fixtures import ITEM, RESOLUTION, FakeLLM, KeywordBackend, extracted, judge


def test_progress_logs_first_every_tenth_and_last(caplog):
    caplog.set_level(logging.INFO, logger='upgrade')
    progress = Progress(get_logger('test'), 'items', 25)
    for _ in range(25):
        progress.advance()
    counts = [int(r.getMessage().split()[1].split('/')[0]) for r in caplog.records]
    assert counts == [1, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 25]


def test_dispatcher_sets_the_log_level_for_every_command(monkeypatch):
    seen = []
    monkeypatch.setattr('upgrade_pipeline.cli.run', lambda argv: seen.append(argv) or 0)
    monkeypatch.setitem(COMMANDS, 'run', ('upgrade_pipeline.cli', 'run', 'run'))
    assert main(['--project', 'Orbit', '--log-level', 'debug']) == 0
    assert seen[-1] == ['--project', 'Orbit'] and logging.getLogger('upgrade').level == logging.DEBUG
    assert main(['-q', '--project', 'Orbit']) == 0
    assert seen[-1] == ['--project', 'Orbit'] and logging.getLogger('upgrade').level == logging.WARNING
    configure_logging('info')


def test_a_case_logs_every_step(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger='upgrade')
    _, _, collected, _, _ = extracted()
    collected.update(status='partial', failures=[], pending_urls=[], skipped=[], warnings=[], network_requests=0, llm_calls=0,
                     version_coverage={'2.0': {'document_ids': ['log'], 'document_types': ['release_notes']},
                                       '2.1': {'document_ids': ['log', 'guide'], 'document_types': ['release_notes']}})
    resolution = {**RESOLUTION, 'status': 'partial', 'versions': [{'version': '2.0'}], 'documentation_milestones': ['2'],
                  'coverage': {}, 'warnings': [], 'network_requests': 0}
    monkeypatch.setattr('upgrade_pipeline.runner.resolve', lambda *a, **k: resolution)
    monkeypatch.setattr('upgrade_pipeline.runner.collect', lambda *a, **k: collected)
    run_case(ITEM, tmp_path, offline=True, postprocess=PostprocessConfig(
        relevance_backend=KeywordBackend(), extraction_llm=FakeLLM(), verification={'judge': judge(), 'kind': 'd1'}))
    steps = {r.name.removeprefix('upgrade.') for r in caplog.records}
    assert {'case', 'normalize', 'relevance', 'extract', 'verify', 'final'} <= steps
    messages = ' | '.join(r.getMessage() for r in caplog.records)
    assert 'fact batches 1/' in messages and 'path groups 1/' in messages and 'case completed_partial' in messages
