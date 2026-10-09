import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_collector.extraction import markdown_document
from upgrade_pipeline.evaluation import evaluate
from upgrade_pipeline.extraction import RiskExtractor, select_sections
from upgrade_pipeline.extraction.models import RiskModelOut, fact_batch_schema
from upgrade_pipeline.postprocess import PostprocessConfig
from pipeline_fixtures import (CHANGELOG, ITEM, RESOLUTION, URL, FakeLLM, KeywordBackend, case, document, evidence, fact,
                               scripted_facts)


def test_relevance_verdicts_decide_which_sections_are_extracted():
    _, plain = case()
    selected, info = select_sections(plain, 10)
    assert not info['relevance_filter_applied'] and len(selected) == 3
    _, scored = case(relevance=True)  # KeywordBackend: only 'deprecated' sections are relevant
    selected, info = select_sections(scored, 10)
    assert info['relevance_filter_applied'] and info['excluded']['not_relevant'] == 1
    assert {s['candidate_id'] for s in selected} == {'log#s0', 'guide#s0'}
    scored['sections'][0]['relevance'] = {'status': 'failed', 'error': 'x'}
    selected, info = select_sections(scored, 1)
    assert info['over_budget'] and info['selected_without_relevance_verdict'] == []
    assert selected[0]['relevance']['label'] == 'risk_candidate'  # scored relevant sections outrank unscored ones


def test_extraction_grounds_every_condition_in_verified_facts():
    inventory, candidates = case()
    model, trace = RiskExtractor(FakeLLM()).run(candidates, inventory, ['No evidence collected for version 2.0.'])
    rejected = {r['subject']: r['reason'] for r in trace['rejected_facts']}
    assert 'contiguous' in rejected['hydrate'] and 'version heading' in rejected['legacyRoot']
    (path,) = model['risk_paths']
    version, usage, disqualifier = path['conditions']
    assert version['required'] and version['condition_role'] == 'required' and version['category'] == 'version'
    assert disqualifier['condition_role'] == 'disqualifying' and not disqualifier['required']
    assert usage['evidence'][0] == {**usage['evidence'][0], 'source_id': 'log', 'quote_or_summary': 'render has been deprecated.',
                                    'source_type': 'official_changelog', 'trust_tier': 'official'}
    assert usage['confidence'] == 'high' and version['confidence'] == 'medium'  # one cited fact is only implied
    assert usage['search_patterns'] == [r'ReactDOM\.render\(']
    assert path['path_id'] == 'deprecated_render_api' and not path['structural_warnings']
    assert [c['subject'] for c in model['contextual_facts']] == ['contributors']
    assert 'No evidence collected for version 2.0.' in model['open_questions']
    assert any('2 extracted facts were rejected' in d for d in model['diagnostics'])
    assert not any('rejected' in q for q in model['open_questions'])  # run diagnostics are not questions about the upgrade
    assert set(model) >= {'project', 'current_version', 'target_version', 'summary', 'source_inventory', 'risk_paths',
                          'open_questions', 'overall_confidence'}


def test_stage_failures_degrade_to_diagnostics():
    inventory, candidates = case()
    extractor = RiskExtractor(FakeLLM(fail={'grouping', 'paths'}))
    model, trace = extractor.run(candidates, inventory)
    assert not model['risk_paths'] and model['overall_confidence'] == 'low'
    assert {g['method'] for g in trace['groups']} == {'subject_key'}  # deterministic grouping fallback
    assert any('paths step failed' in d for d in model['diagnostics']) and not model['open_questions']


def test_fact_schema_limits_citations_to_the_batch_and_interval():
    schema = fact_batch_schema(['log#s0'], ['2.0', '2.1'])
    good = fact('log#s0', 'render', 'deprecated', 'render has been deprecated.', ['2.1'])
    assert schema.model_validate({'facts': [good]})
    for bad in ({**good, 'candidate_id': 'other#s1'}, {**good, 'versions': ['3.0']}):
        with pytest.raises(Exception):
            schema.model_validate({'facts': [bad]})


def test_pipeline_extracts_only_relevant_sections(tmp_path, monkeypatch):
    from upgrade_pipeline.runner import run_case
    _, candidates = case()
    sections, _ = markdown_document(CHANGELOG, URL)
    doc = document('log', URL, bases=('resolver_release_record',), sections=sections, text=CHANGELOG)
    collected = {**evidence(doc), 'status': 'partial', 'failures': [], 'pending_urls': [], 'skipped': [], 'warnings': [],
                 'network_requests': 0, 'llm_calls': 0,
                 'version_coverage': {'2.0': {'document_ids': ['log'], 'document_types': ['release_notes']},
                                      '2.1': {'document_ids': ['log'], 'document_types': ['release_notes']}}}
    resolution = {**RESOLUTION, 'status': 'partial', 'versions': [{'version': '2.0'}], 'documentation_milestones': ['2'],
                  'coverage': {}, 'warnings': [], 'network_requests': 0}
    monkeypatch.setattr('upgrade_pipeline.runner.resolve', lambda *a, **k: resolution)
    monkeypatch.setattr('upgrade_pipeline.runner.collect', lambda *a, **k: collected)
    llm = FakeLLM()
    manifest = run_case(ITEM, tmp_path, offline=True, postprocess=PostprocessConfig(relevance_backend=KeywordBackend(), extraction_llm=llm))
    sent = [s['text'] for stage, payload in llm.seen if stage == 'facts' for s in payload['sections']]
    assert sent and all('deprecated' in text for text in sent)  # not_relevant sections never reach the LLM
    model = json.loads((tmp_path / 'risk_model.json').read_text())
    assert model['risk_paths'] and (tmp_path / 'extracted_facts.json').exists()
    assert manifest['summary']['risk_paths'] == 1 and manifest['configuration']['risk_extraction']


CONTRACT_ALLOWED = {  # the output contract's allowed values
    ('RiskPathOut', 'risk_type'): 'removed_api | changed_behavior | changed_default | dependency_requirement | runtime_requirement | configuration_change | data_migration | operational_risk | unknown',
    ('RiskPathOut', 'risk_level'): 'critical | high | medium | low | unknown',
    ('RiskPathOut', 'affected_surface'): 'application_code | configuration | dependencies | runtime | database | deployment | tests | unknown',
    ('ConditionOut', 'category'): 'version | api_usage | configuration | dependency | runtime | deployment | database | behavior | test_coverage | unknown',
    ('ConditionOut', 'operator'): 'equals | not_equals | in_range | less_than | greater_than | contains | absent | present | unknown',
    ('ConditionOut', 'confidence'): 'high | medium | low',
    ('RiskModelOut', 'overall_confidence'): 'high | medium | low',
}


def test_output_schema_uses_exactly_the_contract_allowed_values():
    from upgrade_pipeline.extraction.models import risk_model_json_schema
    schema = risk_model_json_schema()
    definitions = {**schema['$defs'], 'RiskModelOut': schema}
    for (definition, field), allowed in CONTRACT_ALLOWED.items():
        assert definitions[definition]['properties'][field]['enum'] == [v.strip() for v in allowed.split('|')], field
    assert definitions['ConditionOut']['properties']['required']['type'] == 'boolean'
    assert definitions['ConditionOut']['properties']['evidence']['minItems'] == 1


def test_committed_json_schema_is_current_and_validates_real_output():
    from pathlib import Path
    from jsonschema import Draft202012Validator
    from upgrade_pipeline.extraction.models import risk_model_json_schema
    committed = json.loads((Path(__file__).parents[1] / 'schemas/risk_model.schema.json').read_text())
    assert committed == risk_model_json_schema(), 'regenerate schemas/risk_model.schema.json'
    inventory, candidates = case()
    model, _ = RiskExtractor(FakeLLM()).run(candidates, inventory)
    Draft202012Validator(committed).validate(json.loads(json.dumps(model)))


def test_assembly_rejects_values_outside_the_allowed_lists(monkeypatch):
    inventory, candidates = case()
    monkeypatch.setattr(RiskExtractor, 'confidence', staticmethod(lambda role, cited: 'very_high'))
    with pytest.raises(Exception, match='confidence'):
        RiskExtractor(FakeLLM()).run(candidates, inventory)


GOLD = json.loads((Path(__file__).parents[1] / 'evals/react-17-18-distinctions.json').read_text())
PDF_DISTINCTIONS = ['Required risk conditions', 'Alternative risk paths', 'Removed APIs or features',
                    'Changed defaults or behavior changes', 'Runtime and dependency requirements', 'Configuration changes',
                    'Migration steps', 'Known incompatibilities', 'Conditions that are suspected but not clearly supported',
                    'Contextual facts that are useful but not actual risk conditions']


def ev(change='deprecated'):
    return {'source_id': 's', 'source_type': 'official_migration_guide', 'quote_or_summary': 'quote', 'relevance': 'r',
            'fact_id': 'f', 'candidate_id': 's#s0', 'url': 'u', 'trust_tier': 'official', 'versions': ['18.0.0'], 'certainty': 'explicit'}


def cond(statement, category='api_usage', role='required', confidence='high', expected='x'):
    return {'condition_id': slug_(statement), 'statement': statement, 'category': category, 'required': role == 'required',
            'condition_role': role, 'operator': 'present', 'expected_value': expected, 'evidence': [ev()], 'reasoning': 'r',
            'confidence': confidence, 'ambiguities': [], 'search_patterns': []}


def slug_(text):
    return ''.join(ch if ch.isalnum() else '_' for ch in text.lower())[:40]


def gold_path(name, risk_type, usage, *, level='high', steps=(), kinds=('deprecated',), conditions=None):
    return {'path_id': slug_(name), 'path_name': name, 'risk_type': risk_type, 'risk_level': level,
            'affected_surface': 'application_code', 'description': name,
            'conditions': conditions or [cond('React 18.0.0 is in the upgrade interval', 'version', expected='18.0.0'), cond(usage, expected=usage)],
            'migration_steps': [{'step': s, 'evidence': [ev()]} for s in steps], 'suggested_actions': [],
            'change_kinds': list(kinds), 'structural_warnings': []}


def model_with(paths, context):
    model = {'project': 'React', 'current_version': '17', 'target_version': '18', 'schema_version': 'risk-model-1.0',
             'generated_at': 't', 'summary': 's', 'source_inventory': [], 'risk_paths': paths, 'contextual_facts': context,
             'open_questions': [], 'overall_confidence': 'medium',
             'confidence_rubric': {'high': 'h', 'medium': 'm', 'low': 'l'}, 'extraction': {}}
    RiskModelOut.model_validate(model)  # the ideal answer must be expressible in the validated output schema
    return model


def fact_(subject, statement, change='context'):
    return {'fact_id': 'c', 'statement': statement, 'subject': subject, 'change': change, 'versions': ['18.0.0'],
            'known_incompatibility': change == 'known_issue',
            'why_not_a_risk': 'context', 'evidence': [ev()]}


def ideal_react_model():
    return model_with([
        gold_path('ReactDOM.render deprecated', 'changed_behavior', 'Application calls ReactDOM.render',
                  steps=['Replace ReactDOM.render with createRoot from react-dom/client'], kinds=('deprecated', 'migration_step')),
        gold_path('ReactDOM.hydrate deprecated', 'changed_behavior', 'Application calls ReactDOM.hydrate'),
        gold_path('unmountComponentAtNode deprecated', 'changed_behavior', 'Application calls unmountComponentAtNode',
                  steps=['Call root.unmount() instead'], kinds=('deprecated', 'migration_step')),
        gold_path('renderToNodeStream deprecated', 'changed_behavior', 'Server uses renderToNodeStream'),
        gold_path('render callback removed', 'removed_api', 'Application passes a callback to render', kinds=('removed',)),
        gold_path('Automatic batching of all updates', 'changed_behavior', 'Updates in timeouts rely on separate renders', kinds=('behavior_changed',)),
        gold_path('Hydration mismatches become errors', 'changed_behavior', 'Server markup differs from client', kinds=('behavior_changed',)),
        gold_path('Internet Explorer no longer supported', 'runtime_requirement', 'Application must support IE',
                  level='critical', kinds=('requirement_raised', 'known_issue')),
        gold_path('TypeScript definitions require @types/react 18', 'dependency_requirement', 'Project uses @types/react 17',
                  kinds=('requirement_raised',)),
        gold_path('Test environment needs IS_REACT_ACT_ENVIRONMENT', 'configuration_change', 'Tests do not set IS_REACT_ACT_ENVIRONMENT',
                  level='medium', kinds=('migration_step',)),
        gold_path('Strict Mode may later preserve state across remounts', 'changed_behavior', 'x', level='low',
                  conditions=[cond('A future feature may preserve state across remounts', 'behavior', 'uncertain', 'low')]),
    ], [fact_('React Native', 'React 18 will ship in a future version of React Native.', 'known_issue'),
        fact_('Server Components', 'Server Components remain experimental.'),
        fact_('memory', 'Improved memory usage.'),
        fact_('undefined', 'Components can now render undefined.')])


def test_gold_set_covers_every_extraction_distinction():
    assert sorted({c['distinction'] for c in GOLD['checks']}) == sorted(PDF_DISTINCTIONS)
    assert all(c.get('source') for c in GOLD['checks'])


def test_output_format_can_express_every_distinction():
    report = evaluate(ideal_react_model(), GOLD)
    assert report['passed'] == report['total'] == len(GOLD['checks']), [r for r in report['results'] if not r['passed']]


def test_checks_fail_a_vague_single_path_extraction():
    vague = model_with([gold_path('There are breaking changes', 'unknown',
                                  'Application uses ReactDOM.render, hydrate, unmountComponentAtNode, renderToNodeStream, '
                                  'Server Components and Internet Explorer', level='high')], [])
    report = evaluate(vague, GOLD)
    failed = {r['id'] for r in report['results'] if not r['passed']}
    assert {'deprecated_root_apis_are_separate_paths', 'render_to_create_root', 'react_native_not_yet_supported',
            'server_components_future_release', 'memory_usage_context', 'internet_explorer_dropped'} <= failed


def test_evaluate_cli_reports_failures(tmp_path, capsys):
    from upgrade_pipeline.evaluation import main
    (tmp_path / 'risk_model.json').write_text(json.dumps(ideal_react_model()))
    gold = Path(__file__).parents[1] / 'evals/react-17-18-distinctions.json'
    assert main([str(gold), str(tmp_path)]) == 0
    assert '16/16 checks passed' in capsys.readouterr().out


def test_evidence_caps_severity_and_flags_incompatibilities():
    implied = {'heading': 'Root API', 'heading_path': [], 'certainty': 'implied'}
    warnings = []
    assert RiskExtractor.capped_level('critical', [implied], warnings) == 'medium' and 'no cited fact is explicit' in warnings[0]
    experimental = {'heading': 'Core', 'heading_path': ['18.0.0', 'Server Components (Experimental)'], 'certainty': 'explicit'}
    assert RiskExtractor.capped_level('high', [experimental], []) == 'low'
    assert RiskExtractor.capped_level('high', [experimental, {**implied, 'certainty': 'explicit'}], []) == 'high'
    assert RiskExtractor.capped_level('unknown', [implied], []) == 'unknown'
    def flagging(payload):
        out = scripted_facts(payload)
        for f in out['facts']:
            f['known_incompatibility'] = f['subject'] == 'render'
        return out
    inventory, candidates = case()
    model, _ = RiskExtractor(FakeLLM(facts=flagging)).run(candidates, inventory)
    assert 'known_issue' in model['risk_paths'][0]['change_kinds']


def test_grouping_merges_subject_clusters_and_chunks_large_upgrades(monkeypatch):
    from upgrade_pipeline.extraction import extract
    facts = [{'fact_id': f'f{i:03d}', 'subject': s, 'change': 'deprecated', 'versions': ['2.1'], 'explanation': 'x'}
             for i, s in enumerate(['render', 'render()', 'ReactDOM.render', 'hydrate', 'hydrate'], 1)]
    seen = []
    class Grouper:
        def complete(self, messages, *, output_model, **options):
            clusters = json.loads(messages[1]['content'])['clusters']
            seen.append([c['subjects'] for c in clusters])
            ids = [c['cluster_id'] for c in clusters]
            data = {'groups': [{'label': 'render', 'cluster_ids': ids[:2]}]}  # merge render + ReactDOM.render
            return SimpleNamespace(parsed=output_model.model_validate(data), usage={}, model='fake')
    fill = {'project': 'Orbit', 'current': '1', 'target': '2', 'versions': '2.0, 2.1'}
    groups = RiskExtractor(Grouper()).group(facts, fill)
    assert seen == [[['render', 'render()'], ['ReactDOM.render'], ['hydrate']]]  # code merged spelling variants first
    assert [(g['fact_ids'], g['method']) for g in groups] == [(['f001', 'f002', 'f003'], 'llm'), (['f004', 'f005'], 'unassigned_cluster')]
    monkeypatch.setattr(extract, 'MAX_GROUPING_ITEMS', 2)
    seen.clear()
    groups = RiskExtractor(Grouper()).group(facts, fill)
    assert len(seen) == 1 and {g['method'] for g in groups} == {'llm_chunked', 'subject_key'}  # a 1-cluster chunk needs no call


def test_bug_fixes_cannot_be_severe_risks():
    fix = {'heading': 'React DOM', 'heading_path': ['18.0.0'], 'certainty': 'explicit',
           'quote': '* Fix a crash when rendering `ZonedDateTime` in the tree.'}
    warnings = []
    assert RiskExtractor.capped_level('critical', [fix], warnings) == 'low' and 'bug fix' in warnings[0]
    assert RiskExtractor.capped_level('high', [fix, {**fix, 'quote': 'render has been deprecated.'}], []) == 'high'


def test_security_fixes_shipped_by_the_upgrade_cannot_be_severe_risks():
    advisory = {'heading': '4.2.26', 'heading_path': [], 'certainty': 'explicit', 'change': 'behavior_changed',
                'quote': 'CVE-2025-64459: Potential SQL injection via _connector keyword argument in QuerySet and Q objects'}
    warnings = []
    assert RiskExtractor.capped_level('critical', [advisory], warnings) == 'low' and 'security fix' in warnings[0]
    assert RiskExtractor.capped_level('high', [{**advisory, 'quote': 'x', 'change': 'security_fix'}], []) == 'low'
    assert RiskExtractor.capped_level('high', [advisory, {**advisory, 'quote': 'USE_TZ now defaults to True.'}], []) == 'high'
