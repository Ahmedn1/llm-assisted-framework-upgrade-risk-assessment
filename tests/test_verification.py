import json
from pathlib import Path

from upgrade_pipeline.extraction.models import EvidenceSummaryOut
from upgrade_pipeline.postprocess import PostprocessConfig
from upgrade_pipeline.verification import Verifier
from pipeline_fixtures import ITEM, RESOLUTION, FakeLLM, extracted, judge, run


def conditions(model):
    return {c['statement']: c for p in model['risk_paths'] for c in p['conditions']}


def test_rubric_grades_each_condition_from_its_evidence():
    model, report, summary = run()
    graded = conditions(model)
    usage = graded['Application calls render']
    assert usage['confidence'] == 'high' and usage['verification']['grounding'] == 'grounded'
    assert usage['verification']['best_trust_tier'] == 'official' and usage['verification']['entailment']['verdict'] == 'supports'
    version = graded['Target interval deprecates render']
    assert version['confidence'] == 'medium' and 'relies on implied facts' in version['verification']['confidence_reasons']
    assert version['verification']['independent_source_count'] == 2  # the changelog and the upgrade guide
    assert model['risk_paths'][0]['confidence'] == 'medium'  # weakest required condition
    assert [r['path_id'] for r in summary['evidence_summary']['medium_confidence_risks']] == ['deprecated_render_api']
    assert len(report['conditions']) == 3


def test_without_a_judge_confidence_is_capped_at_medium():
    model, _, summary = run(no_judge=True, judge_kind='none')
    assert conditions(model)['Application calls render']['confidence'] == 'medium'
    assert 'No entailment judge ran; confidence is capped at medium.' in model['diagnostics']
    assert not any('judge' in q for q in summary['evidence_summary']['open_questions'])


def test_unverifiable_quote_is_downgraded_and_listed_not_deleted():
    def fabricate(model, trace):
        usage = model['risk_paths'][0]['conditions'][1]
        usage['evidence'][0] = {**usage['evidence'][0], 'quote_or_summary': 'render was removed entirely in 2.1.'}
    model, _, summary = run(mutate=fabricate)
    usage = conditions(model)['Application calls render']
    assert usage['verification']['grounding'] == 'ungrounded' and usage['confidence'] == 'low'
    assert usage['condition_role'] == 'uncertain' and usage['required'] is False
    assert usage['verification']['preliminary_role'] == 'required'
    (disputed,) = summary['evidence_summary']['low_confidence_or_disputed_risks']
    assert 'ungrounded_condition' in disputed['reasons'] and model['risk_paths']  # still present, now disputed


def test_judge_contradiction_marks_the_condition_uncertain():
    model, _, summary = run({'calls render': 'contradicts'})
    usage = conditions(model)['Application calls render']
    assert usage['confidence'] == 'low' and usage['condition_role'] == 'uncertain'
    assert 'judge: evidence contradicts the statement' in usage['verification']['confidence_reasons']
    assert summary['evidence_summary']['low_confidence_or_disputed_risks'][0]['path_id'] == 'deprecated_render_api'


def third_party_fact(trace, **changes):
    official = next(f for f in trace['facts'] if f['subject'] == 'render')
    fact = {**official, 'fact_id': 'f900', 'change': 'removed', 'source_id': 'blog', 'trust_tier': 'third_party',
            'url': 'https://blog.example/orbit-2', 'quote': 'render was removed', **changes}
    trace['facts'].append(fact)
    return official


def test_official_source_wins_a_disagreement_and_it_is_reported():
    holder = {}
    model, report, summary = run(mutate=lambda model, trace: holder.update(official=third_party_fact(trace)))
    (conflict,) = summary['evidence_summary']['sources_with_disagreements']
    assert conflict['kind'] == 'severity' and conflict['resolution'] == 'resolved'
    assert conflict['winner_fact_id'] == holder['official']['fact_id'] and conflict['affected_paths'] == ['deprecated_render_api']
    assert {c['trust_tier'] for c in conflict['claims']} == {'official', 'third_party'}
    usage = conditions(model)['Application calls render']
    assert usage['confidence'] == 'medium' and usage['verification']['conflicts'] == ['c01']  # won, but disagreement is noted
    assert any(a.startswith('Sources disagree (severity)') for a in usage['ambiguities'])


def test_unresolvable_disagreement_is_disputed_and_becomes_an_open_question():
    # Same tier, same section scope and versions: the policy cannot pick a winner.
    model, _, summary = run(mutate=lambda model, trace: third_party_fact(trace, source_id='mirror', trust_tier='official',
                                                                         url='https://mirror.orbit.dev/changes'))
    (conflict,) = summary['evidence_summary']['sources_with_disagreements']
    assert conflict['resolution'] == 'unresolved' and conflict['winner_fact_id'] is None
    assert conditions(model)['Application calls render']['confidence'] == 'low'
    assert any(q.startswith('Unresolved disagreement about render') for q in summary['evidence_summary']['open_questions'])
    assert model['overall_confidence'] != 'high'


def test_single_source_severe_path_is_flagged():
    def severe(model, trace):
        path = model['risk_paths'][0]
        path['risk_level'] = 'critical'
        path['conditions'] = [c for c in path['conditions'] if all(e['source_id'] == 'log' for e in c['evidence'])]
    model, _, summary = run(mutate=severe)
    assert 'single_source_severe_risk' in model['risk_paths'][0]['verification_flags']
    assert any('single source' in q for q in summary['evidence_summary']['open_questions'])


def test_summary_has_the_contract_shape_and_reverification_is_stable():
    model, report, summary = run()
    assert set(summary) == {'project', 'upgrade', 'evidence_summary'} and summary['upgrade'] == '1_to_2'
    assert set(summary['evidence_summary']) == {'high_confidence_risks', 'medium_confidence_risks',
                                                'low_confidence_or_disputed_risks', 'sources_with_disagreements', 'open_questions'}
    EvidenceSummaryOut.model_validate(summary)
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).parents[1] / 'schemas/risk_model.schema.json').read_text())
    Draft202012Validator(schema).validate(json.loads(json.dumps(model)))
    _, trace, collected, inventory, candidates = extracted()
    again, _, summary_again = Verifier(judge(), judge_kind='d1').run(model, trace, collected, inventory, candidates)
    strip = lambda m: json.dumps({k: v for k, v in m.items() if k != 'verification'}, sort_keys=True)
    assert strip(again) == strip(model) and summary_again['evidence_summary'] == summary['evidence_summary']


def test_pipeline_runs_verification_after_extraction(tmp_path, monkeypatch):
    from upgrade_pipeline.runner import run_case
    _, _, collected, _, _ = extracted()
    collected.update(status='partial', failures=[], pending_urls=[], skipped=[], warnings=[], network_requests=0, llm_calls=0,
                     version_coverage={'2.0': {'document_ids': ['log'], 'document_types': ['release_notes']},
                                       '2.1': {'document_ids': ['log', 'guide'], 'document_types': ['release_notes']}})
    resolution = {**RESOLUTION, 'status': 'partial', 'versions': [{'version': '2.0'}], 'documentation_milestones': ['2'],
                  'coverage': {}, 'warnings': [], 'network_requests': 0}
    monkeypatch.setattr('upgrade_pipeline.runner.resolve', lambda *a, **k: resolution)
    monkeypatch.setattr('upgrade_pipeline.runner.collect', lambda *a, **k: collected)
    manifest = run_case(ITEM, tmp_path, offline=True, postprocess=PostprocessConfig(extraction_llm=FakeLLM(), verification={'judge': judge(), 'kind': 'd1', 'max_conditions': 300}))
    summary = json.loads((tmp_path / 'evidence_summary.json').read_text())
    assert summary['evidence_summary']['medium_confidence_risks'] and (tmp_path / 'grounding_report.json').exists()
    assert json.loads((tmp_path / 'risk_model.json').read_text())['evidence_summary'] == summary
    assert manifest['summary']['verification_judge'] == 'd1' and manifest['configuration']['risk_verification']['judge'] == 'd1'


def test_only_missing_milestone_notes_become_open_questions():
    # The fixture resolves 2.0 and 2.1; only milestone 2 matters, and its sections exist.
    _, _, summary = run(mutate=lambda model, trace: None)
    questions = summary['evidence_summary']['open_questions']
    assert not any(q.startswith('Version ') for q in questions)  # no per-version noise
    model, trace, collected, inventory, candidates = extracted()
    candidates['documentation_milestones'] = ['2', '3']
    _, _, summary = Verifier(judge(), judge_kind='d1').run(model, trace, collected, inventory, candidates)
    assert summary['evidence_summary']['open_questions'] == [
        'No release-note sections were collected for Orbit 3; changes introduced there are unknown.']


def test_single_source_severe_risks_become_one_question():
    def severe(model, trace):
        path = model['risk_paths'][0]
        path['risk_level'] = 'critical'
        path['conditions'] = [c for c in path['conditions'] if all(e['source_id'] == 'log' for e in c['evidence'])]
        model['risk_paths'] += [{**path, 'path_id': f'p{i}', 'path_name': f'Risk {i}'} for i in range(4)]
    _, _, summary = run(mutate=severe)
    single = [q for q in summary['evidence_summary']['open_questions'] if 'single source' in q]
    assert single == ["5 critical or high risks rest on a single source (flagged single_source_severe_risk); confirm them with a "
                      "second source: 'Deprecated render API'; 'Risk 0'; 'Risk 1' and 2 more."]
