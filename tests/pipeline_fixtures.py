"""Shared builders for pipeline tests: a small Orbit project, scripted LLM and decision models, and an
extracted-then-verified risk model. Test modules import from here, never from each other."""
import json
from types import SimpleNamespace

from evidence_collector.extraction import markdown_document
from inference import ModelOutputError
from inference.decisions import DecisionBackend, validate_decision
from upgrade_pipeline.extraction import RiskExtractor
from upgrade_pipeline.normalize import normalize_case
from upgrade_pipeline.relevance import assess_sections
from upgrade_pipeline.verification import Verifier


ITEM = {'project': 'Orbit', 'current_version': '1', 'target_version': '2'}


RESOLUTION = {'identity': {'homepage': 'https://docs.orbit.dev/', 'repository': 'orbit/orbit'},
              'selected_catalog': 'https://api.github.com/repos/orbit/orbit/releases'}


def document(id, url, document_type='release_notes', bases=('linked_context',), sections=None, **extra):
    return {'id': id, 'url': url, 'title': id, 'document_type': document_type, 'retrieved_at': 't', 'content_sha256': 'h',
            'publisher_relationship': 'supporting_context', 'discovered_from': [],
            'version_associations': [{'version': '2.0', 'basis': b} for b in bases],
            'sections': sections if sections is not None else [{'heading': 'Notes', 'text': 'The API was removed.'}], **extra}


def evidence(*documents):
    return {'input': ITEM, 'selected_catalog': RESOLUTION['selected_catalog'], 'documents': list(documents),
            'version_coverage': {'2.0': {}, '2.1': {}}}


class KeywordBackend(DecisionBackend):
    """Deterministic stand-in for the decision model: 'deprecated' text is a risk, anything else is background."""
    def __init__(self, fail_on=None):
        self.states, self.fail_on = [], fail_on

    def decide(self, state, questions):
        from inference import ModelOutputError
        self.states.append(state)
        text = state['section']['text']
        if self.fail_on and self.fail_on in text:
            raise ModelOutputError('malformed decision')
        level, choice = ('4', 'changed_behavior') if 'deprecated' in text else ('1', 'not_a_risk')
        rubric = questions['risk_relevance'].criteria
        return validate_decision({'model': 'keyword', 'usage': {'input_tokens': 1, 'output_tokens': 1}, 'answers': {
            'risk_relevance': {'type': 'score', 'score': float(level), 'confidence': 0.9,
                               'legend': {str(i): c for i, c in enumerate(rubric)},
                               'probabilities': {str(i): float(str(i) == level) for i in range(len(rubric))}},
            'risk_type': {'type': 'choice', 'choice': choice, 'confidence': 0.8,
                          'probabilities': {k: float(k == choice) for k in questions['risk_type'].criteria}},
            'checkable_condition': {'type': 'noul', 'noul': 0.7}}}, questions, backend='keyword')


URL = 'https://github.com/orbit/orbit/blob/main/CHANGELOG.md'


CHANGELOG = ('## 2.1.0\n### Core\n* `render` has been deprecated. Use `createRoot` instead.\n'
             '## 2.0.0\n* Removed the `legacyRoot` option.\n* Thanks to all contributors for this release.\n')


def case(relevance=False):
    sections, _ = markdown_document(CHANGELOG, URL)
    guide = document('guide', 'https://docs.orbit.dev/upgrade', 'upgrade_guide', bases=('resolver_release_record',),
                     sections=[{'heading': 'Root API', 'text': 'ReactDOM render is deprecated in 2.1; migrate every root to createRoot.'}])
    log = document('log', URL, bases=('resolver_release_record',), sections=sections, text=CHANGELOG)
    inventory, candidates = normalize_case(RESOLUTION, evidence(log, guide))
    if relevance:
        assess_sections(candidates, inventory, KeywordBackend())
    return inventory, candidates


def fact(candidate_id, subject, change, quote, versions, is_risk=True, certainty='explicit'):
    return {'candidate_id': candidate_id, 'subject': subject, 'subject_kind': 'api', 'change': change, 'is_risk': is_risk,
            'known_incompatibility': False,
            'old_value': None, 'new_value': None, 'versions': versions, 'quote': quote,
            'explanation': f'{subject} {change}.', 'certainty': certainty}


def scripted_facts(payload):
    out = []
    for section in payload['sections']:
        text = section['text']
        if 'render` has been deprecated' in text:
            out.append(fact(section['candidate_id'], 'render', 'deprecated', 'render has been deprecated.', ['2.1']))
            out.append(fact(section['candidate_id'], 'hydrate', 'removed', 'hydrate was removed entirely.', ['2.1']))  # invented
        if 'legacyRoot' in text:
            out.append(fact(section['candidate_id'], 'legacyRoot', 'removed', 'Removed the `legacyRoot` option.', ['2.1']))  # wrong version
            out.append(fact(section['candidate_id'], 'contributors', 'context', 'Thanks to all contributors', ['2.0'], is_risk=False))
        if 'migrate every root' in text:
            out.append(fact(section['candidate_id'], 'ReactDOM.render', 'deprecated', 'ReactDOM render is deprecated in 2.1',
                            ['2.1'], certainty='implied'))
    return {'facts': out}


def scripted_paths(payload):
    ids = [f['fact_id'] for f in payload['facts']]
    by_subject = {f['subject']: f['fact_id'] for f in payload['facts']}
    condition = lambda **c: {'operator': 'present', 'ambiguities': [], 'search_patterns': [], 'reasoning': 'From the facts.', **c}
    return {'not_a_risk_reason': None, 'paths': [{
        'path_name': 'Deprecated render API', 'risk_type': 'changed_behavior', 'risk_level': 'medium',
        'affected_surface': 'application_code', 'description': 'Apps calling render get deprecation warnings.',
        'suggested_actions': ['Search for render( calls and run the test suite.'],
        'migration_steps': [{'action': 'Replace render with createRoot.', 'fact_ids': [by_subject['render']]}],
        'conditions': [condition(statement='Target interval deprecates render', category='version', condition_role='required',
                                 operator='in_range', expected_value='2.1', fact_ids=ids),
                       condition(statement='Application calls render', category='api_usage', condition_role='required',
                                 expected_value='render(', fact_ids=[by_subject['render']], search_patterns=[r'ReactDOM\.render\(']),
                       *([condition(statement='Application already uses createRoot everywhere', category='api_usage',
                                    condition_role='disqualifying', expected_value='createRoot',
                                    fact_ids=[by_subject['ReactDOM.render']])] if 'ReactDOM.render' in by_subject else [])]}]}


class FakeLLM:
    """Answers each extraction stage from its JSON payload; scripts can be replaced per test."""
    def __init__(self, fail=(), facts=None, paths=None):
        self.seen, self.fail = [], set(fail)
        self.facts, self.paths = facts or scripted_facts, paths or scripted_paths

    def complete(self, messages, *, output_model, **options):
        payload = json.loads(messages[1]['content'])
        stage = {'ScopedFactBatch': 'facts', 'ScopedGrouping': 'grouping', 'ScopedPathBatch': 'paths'}[output_model.__name__]
        self.seen.append((stage, payload))
        if stage in self.fail:
            raise ModelOutputError(f'{stage} failed')
        data = {'facts': self.facts, 'paths': self.paths,
                'grouping': lambda p: {'groups': [{'label': 'render deprecated', 'cluster_ids': [c['cluster_id'] for c in p['clusters']]}]}}[stage](payload)
        return SimpleNamespace(parsed=output_model.model_validate(data), usage={'total_tokens': 1}, model='fake')


GUIDE_TEXT = 'ReactDOM render is deprecated in 2.1; migrate every root to createRoot.'


def extracted():
    sections, _ = markdown_document(CHANGELOG, URL)
    guide = document('guide', 'https://docs.orbit.dev/upgrade', 'upgrade_guide', bases=('resolver_release_record',),
                     sections=[{'heading': 'Root API', 'text': GUIDE_TEXT}], text='Root API\n' + GUIDE_TEXT)
    log = document('log', URL, bases=('resolver_release_record',), sections=sections, text=CHANGELOG)
    collected = evidence(log, guide)
    inventory, candidates = normalize_case(RESOLUTION, collected)
    model, trace = RiskExtractor(FakeLLM()).run(candidates, inventory)
    return model, trace, collected, inventory, candidates


def judge(verdicts=None):
    """Scripted entailment judge: supports everything unless the statement contains a scripted keyword."""
    def call(state):
        statement = state['condition']['statement']
        verdict = next((v for k, v in (verdicts or {}).items() if k in statement), 'supports')
        return {'verdict': verdict, 'adds_unsupported_detail': 0.1, 'judge': 'scripted', 'note': None}
    call.name = 'scripted'
    return call


def run(verdicts=None, judge_kind='d1', mutate=None, no_judge=False):
    model, trace, collected, inventory, candidates = extracted()
    if mutate:
        mutate(model, trace)
    return Verifier(None if no_judge else judge(verdicts), judge_kind=judge_kind).run(model, trace, collected, inventory, candidates)
