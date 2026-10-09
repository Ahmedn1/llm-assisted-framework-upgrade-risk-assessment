import json
from upgrade_pipeline.runner import run_case
from upgrade_pipeline.cli import main


ITEM = {'project': 'Orbit', 'current_version': '1', 'target_version': '2'}


def test_ambiguous_resolution_blocks_collection_and_saves_artifact(tmp_path, monkeypatch):
    result = {'status': 'ambiguous', 'versions': [], 'documentation_milestones': [], 'coverage': {}, 'warnings': [], 'network_requests': 0}
    monkeypatch.setattr('upgrade_pipeline.runner.resolve', lambda *a, **k: result)
    def no_collection(*a, **k):
        raise AssertionError('Collector must not run')
    monkeypatch.setattr('upgrade_pipeline.runner.collect', no_collection)
    manifest = run_case(ITEM, tmp_path, offline=True)
    assert manifest['summary']['pipeline_status'] == 'blocked_at_resolution'
    assert json.loads((tmp_path / 'resolution.json').read_text()) == result
    assert not (tmp_path / 'evidence.json').exists()


def test_partial_resolution_can_collect_and_reports_metadata_only(tmp_path, monkeypatch):
    resolution = {'status': 'partial', 'selected_catalog': 'https://example.org', 'versions': [{'version': '2'}],
                  'documentation_milestones': ['2'], 'coverage': {}, 'warnings': [], 'network_requests': 0}
    evidence = {'status': 'partial', 'documents': [{'id': 'd', 'url': 'https://example.org/2', 'document_type': 'registry_metadata',
                'version_associations': [{'version': '2', 'basis': 'registry_version'}]}],
                'version_coverage': {'2': {'document_ids': ['d'], 'document_types': ['registry_metadata']}},
                'failures': [], 'pending_urls': [], 'skipped': [], 'warnings': [], 'network_requests': 0, 'llm_calls': 0}
    monkeypatch.setattr('upgrade_pipeline.runner.resolve', lambda *a, **k: resolution)
    monkeypatch.setattr('upgrade_pipeline.runner.collect', lambda *a, **k: evidence)
    manifest = run_case(ITEM, tmp_path, offline=True)
    assert manifest['summary']['versions_with_only_registry_metadata'] == ['2']
    assert manifest['summary']['versions_with_direct_evidence'] == 1
    assert (tmp_path / 'evidence.json').exists()


def test_batch_continues_after_one_failed_case(tmp_path, monkeypatch):
    calls = []
    def resolve(*args, **kwargs):
        calls.append(args[1])
        raise ValueError('fixture failure')
    monkeypatch.setattr('upgrade_pipeline.runner.resolve', resolve)
    inputs = tmp_path / 'inputs.json'; inputs.write_text(json.dumps([ITEM, {**ITEM, 'project': 'Other'}]))
    assert main(['--inputs', str(inputs), '--output-dir', str(tmp_path / 'results'), '--offline', '--no-section-relevance']) == 2
    summary = json.loads((tmp_path / 'results/summary.json').read_text())
    assert calls == ['Orbit', 'Other']
    assert summary['completed_cases'] == 2
    assert all(r['failed_stage'] == 'resolver' for r in summary['results'])


def test_unassociated_documents_are_not_reported_as_useful_version_coverage():
    from upgrade_pipeline.runner import summarize
    resolution = {'status': 'partial', 'versions': [{'version': '2'}], 'documentation_milestones': ['2'],
                  'coverage': {}, 'warnings': [], 'network_requests': 0}
    evidence = {'status': 'partial', 'documents': [{'id': 'd', 'url': 'https://example.org', 'document_type': 'upgrade_guide', 'version_associations': []}],
                'version_coverage': {'2': {'document_ids': [], 'document_types': []}}, 'failures': [],
                'pending_urls': [], 'skipped': [], 'warnings': [], 'network_requests': 0, 'llm_calls': 0}
    summary = summarize(resolution, evidence)
    assert summary['pipeline_status'] == 'collected_unassociated'
    assert summary['versions_with_candidate_evidence'] == 0


def test_input_version_mode_reaches_the_resolver(tmp_path, monkeypatch):
    seen = {}
    def resolve(*args, **kwargs):
        seen.update(kwargs)
        return {'status': 'ambiguous', 'versions': [], 'documentation_milestones': [], 'coverage': {}, 'warnings': [], 'network_requests': 0}
    monkeypatch.setattr('upgrade_pipeline.runner.resolve', resolve)
    run_case({**ITEM, 'version_mode': 'exact'}, tmp_path, offline=True)
    assert seen['mode'] == 'exact'
    run_case(ITEM, tmp_path, offline=True)
    assert seen['mode'] == 'family'
