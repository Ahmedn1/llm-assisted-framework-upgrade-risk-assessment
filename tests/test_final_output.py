import json
from pathlib import Path

from jsonschema import Draft202012Validator

from upgrade_pipeline.final_output import build_final_output, final_output_json_schema
from pipeline_fixtures import extracted, run

ROOT = Path(__file__).parents[1]
# Field lists from the output contract's risk-path and final-output examples.
PDF_TOP = {'project', 'current_version', 'target_version', 'summary', 'source_inventory', 'risk_paths', 'open_questions',
           'overall_confidence'}
PDF_PATH = {'path_id', 'path_name', 'risk_type', 'risk_level', 'affected_surface', 'description', 'conditions', 'suggested_actions'}
PDF_CONDITION = {'condition_id', 'statement', 'category', 'required', 'operator', 'expected_value', 'evidence', 'confidence', 'ambiguities'}
PDF_EVIDENCE = {'source_id', 'source_type', 'quote_or_summary', 'relevance'}
PDF_SOURCE = {'source_id', 'title', 'url', 'source_type', 'retrieved_at', 'why_relevant'}


def test_final_output_contains_every_contract_field():
    verified, _, _ = run()
    output = build_final_output(verified)
    assert PDF_TOP <= set(output)
    assert all(PDF_SOURCE <= set(s) for s in output['source_inventory'])
    path = output['risk_paths'][0]
    assert PDF_PATH <= set(path)
    assert all(PDF_CONDITION <= set(c) for c in path['conditions'])
    assert all(PDF_EVIDENCE <= set(e) for c in path['conditions'] for e in c['evidence'])
    assert output['overall_confidence'] in ('high', 'medium', 'low')


def test_verified_output_embeds_summary_and_compact_verification():
    verified, _, summary = run()
    output = build_final_output(verified)
    assert output['evidence_summary'] == summary and output['provenance']['verified']
    usage = next(c for c in output['risk_paths'][0]['conditions'] if c['statement'] == 'Application calls render')
    assert usage['verification'] == {'grounding': 'grounded', 'entailment': 'supports', 'independent_sources': 1, 'conflicts': []}
    assert 'fact_id' not in usage['evidence'][0] and 'candidate_id' not in usage['evidence'][0]  # internal ids stay in working files
    assert output['risk_paths'][0]['confidence'] == 'medium'
    assert {s['source_id']: s['cited_by_risk_paths'] for s in output['source_inventory']} == {'guide': 1, 'log': 1}
    text = output['summary']
    assert text.startswith('Orbit 1 → 2 (2.0–2.1): 1 risk paths (1 medium).') and 'Main categories: changed behavior (1)' in text
    assert 'Verified: 0 high-, 1 medium- and 0 low-confidence or disputed risks' in text and 'Overall confidence: medium.' in text


def test_unverified_model_still_produces_a_valid_final_output():
    model, *_ = extracted()
    output = build_final_output(model)
    assert output['evidence_summary'] is None and not output['provenance']['verified']
    assert all(c['verification'] is None for p in output['risk_paths'] for c in p['conditions'])
    assert 'Not verified (verification not run)' in output['summary']


def test_paths_are_ordered_by_severity_then_confidence():
    verified, _, _ = run()
    second = json.loads(json.dumps(verified['risk_paths'][0]))
    second.update(path_id='second', path_name='A critical path', risk_level='critical', confidence='low')
    verified['risk_paths'].append(second)
    assert [p['path_id'] for p in build_final_output(verified)['risk_paths']] == ['second', 'deprecated_render_api']


def test_committed_schema_is_current_and_validates_output():
    committed = json.loads((ROOT / 'schemas/final_output.schema.json').read_text())
    assert committed == final_output_json_schema(), 'regenerate schemas/final_output.schema.json'
    verified, _, _ = run()
    Draft202012Validator(committed).validate(json.loads(json.dumps(build_final_output(verified))))


def test_batch_collects_each_upgrade_final_output(tmp_path, monkeypatch):
    from upgrade_pipeline.cli import main
    verified, _, _ = run()
    def run_case(item, directory, **kwargs):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'final_output.json').write_text(json.dumps(build_final_output(verified)))
        return {'summary': {'pipeline_status': 'completed_partial'}, 'files': {'final_output': 'final_output.json'}}
    monkeypatch.setattr('upgrade_pipeline.cli.run_case', run_case)
    inputs = tmp_path / 'inputs.json'
    inputs.write_text(json.dumps([{'project': 'Django', 'current_version': '3.2', 'target_version': '4.2'},
                                  {'project': 'Next.js', 'current_version': '12', 'target_version': '14'}]))
    assert main(['--inputs', str(inputs), '--output-dir', str(tmp_path / 'run'), '--no-section-relevance', '--no-extract-risks']) == 0
    assert sorted(p.name for p in (tmp_path / 'run' / 'final').iterdir()) == ['django_3.2_to_4.2.json', 'next.js_12_to_14.json']
    rows = json.loads((tmp_path / 'run' / 'summary.json').read_text())['results']
    assert all(r['final_output'].endswith('.json') for r in rows)


def test_outputs_saved_before_diagnostics_are_sorted_when_rebuilt():
    verified, _, _ = run()
    legacy = {k: v for k, v in verified.items() if k != 'diagnostics'}
    legacy['open_questions'] = ['159 relevant sections were not processed (section budget 150).',
                                '8 extracted facts were rejected because their quote or versions did not match the section.',
                                'Extraction facts step failed: Completion did not finish normally',
                                'No entailment judge ran; confidence is capped at medium.',
                                'Version 4.0.1 has candidate sections but no extracted risk: no breaking changes, or extraction missed them?',
                                'Version 4.0.2 has no candidate sections, so its risks are unknown.',
                                "Unconfirmed condition in 'X': the app relies on Y"]
    output = build_final_output(legacy)
    assert output['open_questions'] == ["Unconfirmed condition in 'X': the app relies on Y"]
    assert output['evidence_summary']['evidence_summary']['open_questions'] == output['open_questions']
    assert len(output['diagnostics']) == 4 and not any(d.startswith('Version') for d in output['diagnostics'])
