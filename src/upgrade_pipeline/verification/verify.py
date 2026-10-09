"""Verification: evidence grounding, entailment, corroboration, conflicts, final confidence and the evidence summary.

Nothing is deleted. Conditions whose evidence cannot be re-verified, or that the judge finds unsupported, are
downgraded (role uncertain, confidence low) and listed under low_confidence_or_disputed_risks with reasons.
Only the entailment judge is a model; every other step is deterministic.
"""
import copy
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations

from inference import ModelError
from ..common import Progress, get_logger, loose
from ..extraction.extract import subject_key
from ..extraction.models import EvidenceSummaryOut, RiskModelOut
from upgrade_resolver.versions import numeric_version
from ..schema import TIER_RANK
from .judges import judge_state

log = get_logger('verify')
DEFAULT_MAX_CONDITIONS = 300
INCOMPATIBLE_CHANGES = {frozenset({'removed', 'deprecated'}), frozenset({'removed', 'replacement_recommended'})}
SEVERE = ('critical', 'high')
RUBRIC = {
    'high': 'Evidence re-verified in the source document; the judge says it supports the statement without adding '
            'unsupported detail; every cited fact is explicit; an official source; no unresolved or lost conflict.',
    'medium': 'Grounded and not disputed, but at least one high requirement is missing (judge not run or partial '
              'support, implied facts, probable-official or discussion sources, a resolved conflict, partial grounding).',
    'low': 'Disputed or weak: evidence not re-verifiable, judge says contradicts or unrelated, condition uncertain, '
           'only speculative facts, only third-party sources, the cited source lost a conflict, or an unresolved conflict.',
}
CONFLICT_POLICY = ('Higher trust tier wins (official > probable_official > official_repository_discussion > third_party); '
                   'then the more specific source (scoped by a version heading over a whole-document association); '
                   'then the source for the more recent version. A tie on all three is unresolved.')


def single_source_question(names):
    shown = '; '.join(f"'{n}'" for n in names[:3]) + (f' and {len(names) - 3} more' if len(names) > 3 else '')
    return (f'{len(names)} critical or high risks rest on a single source (flagged single_source_severe_risk); '
            f'confirm them with a second source: {shown}.')


class Verifier:
    def __init__(self, judge=None, *, judge_kind='none', max_conditions=DEFAULT_MAX_CONDITIONS):
        if type(max_conditions) is not int or max_conditions < 1:
            raise ValueError('max_conditions must be a positive integer')
        self.judge, self.judge_kind, self.max_conditions = judge, judge_kind, max_conditions

    def run(self, risk_model, trace, evidence, inventory, candidates):
        model = self.unverified(copy.deepcopy(risk_model))
        self.resolved = list(candidates['resolved_versions'])
        self.milestones = list(candidates.get('documentation_milestones') or [])
        self.facts = {f['fact_id']: f for f in trace['facts']}
        self.tiers = {s['source_id']: s['trust']['tier'] for s in inventory['sources']}
        self.sections = {c['candidate_id']: c for c in candidates['sections']}
        self.copies = self.duplicate_sources(candidates['sections'])
        self.documents = {d['id']: d for d in evidence['documents']}
        self.texts, self.calls, self.judge_failures, self.unchecked = {}, 0, [], 0
        conflicts = self.find_conflicts(model)
        log.info('%d source disagreements (%d unresolved); judging conditions with %s', len(conflicts),
                 sum(c['resolution'] == 'unresolved' for c in conflicts), self.judge_kind)
        progress = Progress(log, 'verified conditions', sum(len(p['conditions']) for p in model['risk_paths']))
        rows = []
        context = {'project': model['project'], 'upgrade': f"{model['current_version']} -> {model['target_version']}"}
        for path in model['risk_paths']:
            for condition in path['conditions']:
                progress.advance()
                rows.append(self.verify_condition(path, condition, conflicts, context))
            self.score_path(path)
        context_failures = [c['fact_id'] for c in model['contextual_facts'] if any(self.evidence_errors(e) for e in c['evidence'])]
        extraction_questions, extraction_diagnostics = list(model['open_questions']), list(model.get('diagnostics', []))
        summary = self.summarize(model, conflicts, context_failures)
        model['diagnostics'] = extraction_diagnostics + self.diagnostics(context_failures)
        model['evidence_summary'] = summary
        model['open_questions'] = summary['evidence_summary']['open_questions']
        model['overall_confidence'] = self.overall(model, conflicts)
        model['confidence_rubric'] = RUBRIC
        model['verification'] = {'generated_at': datetime.now(timezone.utc).isoformat(), 'judge': self.judge_kind,
                                 'judge_model': getattr(self.judge, 'name', None), 'judge_calls': self.calls,
                                 'judge_failures': len(self.judge_failures), 'conditions_not_judged': self.unchecked,
                                 'conflict_policy': CONFLICT_POLICY, 'conflicts': len(conflicts),
                                 'unresolved_conflicts': sum(c['resolution'] == 'unresolved' for c in conflicts),
                                 'contextual_facts_failing_grounding': context_failures,
                                 'extraction_open_questions': extraction_questions,
                                 'extraction_diagnostics': extraction_diagnostics}
        RiskModelOut.model_validate(model)
        body = summary['evidence_summary']
        log.info('risks: %d high, %d medium, %d low or disputed; %d open questions; overall %s',
                 len(body['high_confidence_risks']), len(body['medium_confidence_risks']),
                 len(body['low_confidence_or_disputed_risks']), len(body['open_questions']), model['overall_confidence'])
        EvidenceSummaryOut.model_validate(summary)
        report = {k: model[k] for k in ('project', 'current_version', 'target_version')} | {
            'verification': model['verification'], 'rubric': RUBRIC, 'conditions': rows, 'conflicts': conflicts,
            'judge_failures': self.judge_failures}
        return model, report, summary

    @staticmethod
    def unverified(model):
        """Restore extraction-time roles, confidence and ambiguities so re-verifying never compounds."""
        for path in model['risk_paths']:
            for condition in path['conditions']:
                previous = condition.pop('verification', None)
                if previous:
                    condition['confidence'] = previous['preliminary_confidence']
                    condition['condition_role'] = previous['preliminary_role']
                    condition['required'] = previous['preliminary_role'] == 'required'
                    condition['ambiguities'] = [a for a in condition['ambiguities'] if not a.startswith('Sources disagree (')]
            path.pop('confidence', None)
            path.pop('verification_flags', None)
        previous = model.pop('verification', None) or {}
        model.pop('evidence_summary', None)
        if 'extraction_open_questions' in previous:
            model['open_questions'] = previous['extraction_open_questions']
        if 'extraction_diagnostics' in previous:
            model['diagnostics'] = previous['extraction_diagnostics']
        return model

    # Step 1: deterministic grounding against the collected document, not the extractor's own copy.
    def text(self, source_id):
        if source_id not in self.texts:
            document = self.documents.get(source_id)
            if document and document.get('duplicate_content_of'):
                document = self.documents.get(document['duplicate_content_of'], document)
            self.texts[source_id] = loose(document.get('text') or '') if document else None
        return self.texts[source_id]

    def evidence_errors(self, item):
        errors = []
        if item['source_id'] not in self.tiers:
            errors.append('source not in the source inventory')
        text = self.text(item['source_id'])
        if text is None:
            errors.append('source document not in evidence.json')
        elif loose(item['quote_or_summary']) not in text:
            errors.append('quote not found in the source document')
        if not set(item['versions']) <= set(self.resolved):
            errors.append('versions outside the resolved interval')
        if item['fact_id'] not in self.facts:
            errors.append('fact not in extracted_facts.json')
        return errors

    # Step 3: independent sources, counting exact copies found during normalization.
    @staticmethod
    def duplicate_sources(sections):
        groups = defaultdict(set)
        for section in sections:
            root = section['duplicate_of'] or section['candidate_id']
            groups[root].add(section['source_id'])
        return {s['candidate_id']: groups[s['duplicate_of'] or s['candidate_id']] for s in sections}

    # Step 4: disagreements between facts about the same subject from different sources.
    def find_conflicts(self, model):
        by_subject = defaultdict(list)
        for fact in self.facts.values():
            by_subject[subject_key(fact['subject'])].append(fact)
        found = defaultdict(dict)
        for key, facts in by_subject.items():
            for a, b in combinations(facts, 2):
                if a['source_id'] == b['source_id']:
                    continue
                kind = self.disagreement(a, b)
                if kind:
                    found[(key, kind)].update({a['fact_id']: a, b['fact_id']: b})
        citing = defaultdict(set)
        for path in model['risk_paths']:
            for condition in path['conditions']:
                for item in condition['evidence']:
                    citing[item['fact_id']].add(path['path_id'])
        conflicts = []
        for n, ((key, kind), facts) in enumerate(sorted(found.items()), 1):
            ranked = sorted(facts.values(), key=self.precedence)
            tie = len(ranked) > 1 and self.precedence(ranked[0])[:3] == self.precedence(ranked[1])[:3]
            winner = None if tie else ranked[0]
            conflicts.append({
                'conflict_id': f'c{n:02d}', 'subject': ranked[0]['subject'], 'kind': kind,
                'claims': [{'fact_id': f['fact_id'], 'source_id': f['source_id'], 'url': f['url'], 'trust_tier': f['trust_tier'],
                            'change': f['change'], 'versions': f['versions'], 'value': f['new_value'], 'quote': f['quote']}
                           for f in ranked],
                'resolution': 'unresolved' if tie else 'resolved', 'winner_fact_id': winner and winner['fact_id'],
                'reason': ('Sources tie on trust tier, specificity and recency.' if tie else
                           f"{winner['trust_tier']} source {winner['url']} preferred under the conflict policy."),
                'affected_paths': sorted({p for f in facts for p in citing[f]})})
        return conflicts

    def disagreement(self, a, b):
        if frozenset({a['change'], b['change']}) in INCOMPATIBLE_CHANGES:
            return 'severity'
        if a['is_risk'] != b['is_risk']:
            return 'risk_vs_context'
        if a['change'] != b['change']:
            return None
        if a['new_value'] and b['new_value'] and loose(a['new_value']).lower() != loose(b['new_value']).lower():
            return 'value'
        if self.scoped(a) and self.scoped(b) and not set(a['versions']) & set(b['versions']):
            return 'versions'
        return None

    def scoped(self, fact):
        return (self.sections.get(fact['candidate_id']) or {}).get('version_scope') == 'version_heading'

    def precedence(self, fact):
        latest = max((self.resolved.index(v) for v in fact['versions'] if v in self.resolved), default=-1)
        return (TIER_RANK.get(fact['trust_tier'], 4), 0 if self.scoped(fact) else 1, -latest, fact['fact_id'])

    # Steps 2 and 5 per condition.
    def verify_condition(self, path, condition, conflicts, context):
        checks = {item['fact_id']: self.evidence_errors(item) for item in condition['evidence']}
        grounded = [item for item in condition['evidence'] if not checks[item['fact_id']]]
        grounding = 'grounded' if len(grounded) == len(condition['evidence']) else 'partially_grounded' if grounded else 'ungrounded'
        entailment = self.entail(context, path, condition, grounded or condition['evidence'])
        cited = [self.facts[item['fact_id']] for item in condition['evidence'] if item['fact_id'] in self.facts]
        sources = set()
        for item in grounded:
            sources |= self.copies.get(item['candidate_id'], {item['source_id']})
        tiers = [self.tiers.get(s) for s in sources if self.tiers.get(s)]
        best = min(tiers, key=TIER_RANK.get) if tiers else None
        ids = {f['fact_id'] for f in cited}
        touching = [c for c in conflicts if ids & {claim['fact_id'] for claim in c['claims']}]
        lost = [c for c in touching if c['resolution'] == 'resolved' and c['winner_fact_id'] not in ids]
        unresolved = [c for c in touching if c['resolution'] == 'unresolved']
        preliminary, preliminary_role = condition['confidence'], condition['condition_role']
        low, missing = [], []
        if grounding == 'ungrounded':
            low.append('evidence could not be re-verified in the source document')
        if entailment['verdict'] in ('contradicts', 'unrelated'):
            low.append(f"judge: evidence {entailment['verdict'].replace('_', ' ')} the statement")
        if condition['condition_role'] == 'uncertain':
            low.append('marked uncertain at extraction')
        if cited and all(f['certainty'] == 'speculative' for f in cited):
            low.append('only speculative facts')
        if best in (None, 'third_party'):
            low.append('only third-party sources' if best else 'no verified source')
        if lost:
            low.append('cited source lost a conflict: ' + ', '.join(c['conflict_id'] for c in lost))
        if unresolved:
            low.append('unresolved conflict: ' + ', '.join(c['conflict_id'] for c in unresolved))
        if grounding == 'partially_grounded':
            missing.append('some evidence could not be re-verified')
        if entailment['verdict'] == 'not_checked':
            missing.append('entailment not checked (confidence capped at medium)')
        elif entailment['verdict'] == 'partially_supports':
            missing.append('judge: partial support')
        if (entailment['adds_unsupported_detail'] or 0) >= 0.5:
            missing.append('statement adds detail absent from the quotes')
        if any(f['certainty'] != 'explicit' for f in cited):
            missing.append('relies on implied facts')
        if best not in (None, 'official', 'third_party'):
            missing.append(f'best source is {best}')
        if touching and not (lost or unresolved):
            missing.append('involved in a conflict it won: ' + ', '.join(c['conflict_id'] for c in touching))
        confidence = 'low' if low else 'medium' if missing else 'high'
        if grounding == 'ungrounded' or entailment['verdict'] in ('contradicts', 'unrelated'):
            if condition['condition_role'] != 'uncertain':
                low.append(f"role changed from {condition['condition_role']} to uncertain")
            condition['condition_role'], condition['required'] = 'uncertain', False
        for c in touching:
            condition['ambiguities'].append(f"Sources disagree ({c['kind']}) about {c['subject']}: {c['reason']} [{c['conflict_id']}]")
        condition['confidence'] = confidence
        condition['verification'] = {
            'grounding': grounding, 'failed_evidence': [i for i, errors in checks.items() if errors], 'entailment': entailment,
            'corroborating_sources': sorted(sources), 'independent_source_count': len(sources), 'best_trust_tier': best,
            'conflicts': [c['conflict_id'] for c in touching], 'preliminary_confidence': preliminary,
            'preliminary_role': preliminary_role, 'confidence_reasons': low or missing or ['meets every high-confidence requirement']}
        return {'path_id': path['path_id'], 'condition_id': condition['condition_id'], 'statement': condition['statement'],
                'evidence_checks': checks, 'entailment': entailment, 'preliminary_confidence': preliminary,
                'confidence': confidence, 'reasons': condition['verification']['confidence_reasons']}

    def entail(self, context, path, condition, evidence):
        if self.judge is None:
            return {'verdict': 'not_checked', 'adds_unsupported_detail': None, 'judge': None, 'note': 'no judge configured'}
        if self.calls >= self.max_conditions:
            self.unchecked += 1
            return {'verdict': 'not_checked', 'adds_unsupported_detail': None, 'judge': None, 'note': 'judge budget exhausted'}
        self.calls += 1
        try:
            return self.judge(judge_state({**context, 'path': path['path_name'], 'risk_type': path['risk_type']}, condition, evidence))
        except (ModelError, ValueError) as exc:
            self.judge_failures.append({'path_id': path['path_id'], 'condition_id': condition['condition_id'], 'error': str(exc)})
            log.warning('judge failed on %s: %s', condition['condition_id'], str(exc)[:200])
            return {'verdict': 'not_checked', 'adds_unsupported_detail': None, 'judge': None, 'note': f'judge failed: {exc}'[:300]}

    @staticmethod
    def score_path(path):
        """A path is as strong as its weakest required (or alternative) condition."""
        core = [c for c in path['conditions'] if c['condition_role'] in ('required', 'alternative')] or path['conditions']
        levels = {c['confidence'] for c in core}
        path['confidence'] = 'low' if 'low' in levels else 'high' if levels == {'high'} else 'medium'
        flags = []
        if any(c['verification']['grounding'] == 'ungrounded' for c in path['conditions']):
            flags.append('ungrounded_condition')
        if any(c['verification']['entailment']['verdict'] in ('contradicts', 'unrelated') for c in path['conditions']):
            flags.append('judge_disputed_condition')
        if any(c['verification']['conflicts'] for c in path['conditions']):
            flags.append('source_disagreement')
        sources = {s for c in path['conditions'] for s in c['verification']['corroborating_sources']}
        if path['risk_level'] in SEVERE and len(sources) == 1:
            flags.append('single_source_severe_risk')
        if not any(c['condition_role'] in ('required', 'alternative') for c in path['conditions']):
            flags.append('no_required_condition_after_verification')
        path['verification_flags'] = flags + [f'structural: {w}' for w in path['structural_warnings']]

    # Step 6
    def summarize(self, model, conflicts, context_failures):
        buckets = {'high': [], 'medium': [], 'low': []}
        disputed = {'ungrounded_condition', 'judge_disputed_condition'}
        for path in model['risk_paths']:
            reasons = list(dict.fromkeys(r for c in path['conditions'] for r in c['verification']['confidence_reasons']
                                         if c['confidence'] == path['confidence'])) + path['verification_flags']
            bucket = 'low' if path['confidence'] == 'low' or disputed & set(path['verification_flags']) else path['confidence']
            buckets[bucket].append({'path_id': path['path_id'], 'path_name': path['path_name'], 'risk_type': path['risk_type'],
                                    'risk_level': path['risk_level'], 'confidence': path['confidence'],
                                    'sources': sorted({s for c in path['conditions'] for s in c['verification']['corroborating_sources']}),
                                    'reasons': reasons[:8]})
        # Only the documentation milestones (e.g. 4.0, 4.1, 4.2) are expected to have release notes of their own;
        # patch releases without sections are normal and are not questions about the upgrade.
        sectioned = {v for c in self.sections.values() for v in c['versions']}
        questions = list(model['open_questions'])
        questions += [f"No release-note sections were collected for {model['project']} {m}; changes introduced there are unknown."
                      for m in self.milestones if not any(numeric_version(v) == numeric_version(m) for v in sectioned)]
        questions += [f"Unresolved disagreement about {c['subject']} ({c['kind']}) between "
                      f"{', '.join(sorted({claim['url'] for claim in c['claims']}))} [{c['conflict_id']}]"
                      for c in conflicts if c['resolution'] == 'unresolved']
        single = [p['path_name'] for p in model['risk_paths'] if 'single_source_severe_risk' in p['verification_flags']]
        if single:  # one question, not one per path: changelog-driven projects state most changes exactly once
            questions.append(single_source_question(single))
        return {'project': model['project'], 'upgrade': f"{model['current_version']}_to_{model['target_version']}",
                'evidence_summary': {'high_confidence_risks': buckets['high'], 'medium_confidence_risks': buckets['medium'],
                                     'low_confidence_or_disputed_risks': buckets['low'],
                                     'sources_with_disagreements': [{k: c[k] for k in ('conflict_id', 'subject', 'kind', 'claims', 'resolution',
                                                                                       'winner_fact_id', 'reason', 'affected_paths')}
                                                                    for c in conflicts],
                                     'open_questions': list(dict.fromkeys(questions))}}

    def diagnostics(self, context_failures):
        notes = []
        if self.judge is None:
            notes.append('No entailment judge ran; confidence is capped at medium.')
        if self.unchecked:
            notes.append(f'{self.unchecked} conditions were not judged (judge budget {self.max_conditions}).')
        if self.judge_failures:
            notes.append(f'The judge failed on {len(self.judge_failures)} conditions; they were not judged.')
        if context_failures:
            notes.append(f'{len(context_failures)} contextual facts cite evidence that could not be re-verified.')
        return notes

    def overall(self, model, conflicts):
        paths = model['risk_paths']
        if not paths:
            return 'low'
        counts = {level: sum(p['confidence'] == level for p in paths) for level in ('high', 'medium', 'low')}
        if counts['low'] > len(paths) / 2:
            return 'low'
        if (counts['high'] >= 0.6 * len(paths) and self.judge is not None and not self.judge_failures
                and not any(c['resolution'] == 'unresolved' for c in conflicts)):
            return 'high'
        return 'medium'
