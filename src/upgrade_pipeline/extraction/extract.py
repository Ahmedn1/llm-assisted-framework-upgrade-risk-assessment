"""Risk extraction: candidate sections -> verified change facts -> grouped changes -> upgrade risk paths.

LLM stages: fact extraction (per batch of sections), grouping (per upgrade), path building (per group).
Deterministic stages: section selection, quote and version verification, evidence assembly, ids, confidence
rubric, structural checks and output shape. The model cites ids; code copies evidence from verified facts.
"""
import json
import re
from collections import Counter
from datetime import datetime, timezone

from inference import ModelError
from ..common import Progress, get_logger, loose, slug
from ..schema import RISK_LEVELS
from .models import (FactBatch, Grouping, PathBatch, RiskModelOut, fact_batch_schema, grouping_schema, path_batch_schema)
from .prompts import FACTS_SYSTEM, GROUPING_SYSTEM, PATHS_SYSTEM

log = get_logger('extract')
SCHEMA_VERSION = 'risk-model-1.0'
DEFAULT_MAX_SECTIONS = 150
BATCH_CHARS = 6000
BATCH_SECTIONS = 8
MAX_GROUPING_ITEMS = 150
RELEVANT_LABELS = ('risk_candidate', 'context')
EXPERIMENTAL = re.compile(r'\b(experimental|alpha|canary|unstable)\b', re.I)
BUG_FIX = re.compile(r'^\W*fix(es|ed)?\b', re.I)
SECURITY_FIX = re.compile(r'\bCVE-\d{4}-\d+|\bpotential (?:denial[- ]of[- ]service|sql injection|xss|redos)\b', re.I)
CONFIDENCE_RUBRIC = {
    'high': 'Every cited fact is explicit and at least one comes from an official source.',
    'medium': 'Supported, but some cited facts are implied, or sources are only probable-official or repository discussion.',
    'low': 'Role is uncertain, all cited facts are speculative, or evidence comes only from third-party sources.',
}


def subject_key(subject):
    return re.sub(r'[^a-z0-9.]+', '', subject.lower().replace('()', ''))


def unique(base, used):
    candidate, n = base, 2
    while candidate in used:
        candidate, n = f'{base}_{n}', n + 1
    used.add(candidate)
    return candidate


def select_sections(candidates, max_sections):
    """Relevant sections first. When relevance was scored, not_relevant sections are excluded; sections the
    decision model could not score are kept (after scored ones) rather than silently hidden."""
    scored_run = 'relevance_scoring' in candidates
    chosen, excluded = [], Counter()
    for section in candidates['sections']:
        if section['duplicate_of']:
            excluded['duplicate'] += 1
            continue
        relevance = section.get('relevance') or {}
        if scored_run and relevance.get('status') == 'scored' and relevance.get('label') not in RELEVANT_LABELS:
            excluded['not_relevant'] += 1
            continue
        verdict = relevance.get('label') if relevance.get('status') == 'scored' else None
        rank = 0 if verdict == 'risk_candidate' else 1 if verdict == 'context' or not scored_run else 2
        chosen.append((rank, -(relevance.get('score') or 0), section['source_rank'], section['candidate_id'], section))
    chosen.sort(key=lambda row: row[:4])
    selected = [row[-1] for row in chosen[:max_sections]]
    over_budget = [row[-1]['candidate_id'] for row in chosen[max_sections:]]
    no_verdict = [s['candidate_id'] for s in selected if scored_run and (s.get('relevance') or {}).get('status') != 'scored']
    return selected, {'relevance_filter_applied': scored_run, 'selected': len(selected),
                      'excluded': dict(excluded), 'over_budget': over_budget, 'selected_without_relevance_verdict': no_verdict}


def batches(sections):
    """Pack consecutive sections of the same source so short changelog entries share one call."""
    current, size = [], 0
    for section in sorted(sections, key=lambda s: (s['source_rank'], s['candidate_id'])):
        length = len(section['text']) + len(section['heading'])
        if current and (current[0]['source_id'] != section['source_id'] or size + length > BATCH_CHARS or len(current) >= BATCH_SECTIONS):
            yield current
            current, size = [], 0
        current.append(section)
        size += length
    if current:
        yield current


class RiskExtractor:
    def __init__(self, llm, *, structured_mode='json_schema', max_output_tokens=8192,
                 token_parameter='max_completion_tokens', max_sections=DEFAULT_MAX_SECTIONS):
        if type(max_sections) is not int or max_sections < 1:
            raise ValueError('max_sections must be a positive integer')
        self.llm, self.max_sections = llm, max_sections
        self.options = {'structured_mode': structured_mode, 'max_output_tokens': max_output_tokens,
                        'token_parameter': token_parameter}
        self.calls, self.usage, self.models, self.failures = 0, [], set(), []

    def fail(self, failure):
        self.failures.append(failure)
        log.warning('%s step failed: %s', failure['stage'], failure['error'][:200])

    def ask(self, stage, system, payload, schema, base):
        self.calls += 1
        response = self.llm.complete([{'role': 'system', 'content': system},
                                      {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
                                     output_model=schema, **self.options)
        if not isinstance(response.parsed, base):
            raise ValueError(f'{stage}: expected a validated {base.__name__}')
        self.usage.append(response.usage)
        self.models.add(response.model)
        return response.parsed

    def run(self, candidates, inventory, upstream_open_questions=(), upstream_diagnostics=()):
        header = {k: candidates[k] for k in ('project', 'current_version', 'target_version')}
        versions = candidates['resolved_versions']
        fill = {'project': header['project'], 'current': header['current_version'],
                'target': header['target_version'], 'versions': ', '.join(versions)}
        sources = {s['source_id']: s for s in inventory['sources']}
        selected, selection = select_sections(candidates, self.max_sections)
        log.info('extracting from %d sections (excluded: %s; %d over budget)', len(selected),
                 selection['excluded'] or 'none', len(selection['over_budget']))
        facts, rejected = self.extract_facts(selected, sources, versions, fill)
        risk_facts = [f for f in facts if f['is_risk'] and f['change'] != 'context']
        log.info('%d facts accepted (%d risks, %d context), %d rejected', len(facts), len(risk_facts),
                 len(facts) - len(risk_facts), len(rejected))
        groups = self.group(risk_facts, fill)
        log.info('%d risk facts grouped into %d changes', len(risk_facts), len(groups))
        paths, contextual = self.build_paths(groups, {f['fact_id']: f for f in facts}, fill)
        log.info('built %d risk paths', len(paths))
        contextual = [self.context_entry(f) for f in facts if f not in risk_facts] + contextual
        model = self.assemble(header, candidates, inventory, paths, contextual, selection, rejected,
                              upstream_open_questions, upstream_diagnostics)
        trace = {**header, 'schema_version': SCHEMA_VERSION, 'selection': selection, 'facts': facts,
                 'rejected_facts': rejected, 'groups': groups, 'failures': self.failures}
        return model, trace

    # Stage A + A'
    def extract_facts(self, selected, sources, versions, fill):
        facts, rejected = [], []
        system = FACTS_SYSTEM.format(**fill)
        all_batches = list(batches(selected))
        progress = Progress(log, 'fact batches', len(all_batches))
        for batch in all_batches:
            progress.advance(f'{len(facts)} facts so far')
            by_id = {s['candidate_id']: s for s in batch}
            source = sources.get(batch[0]['source_id'], {})
            payload = {'source': {'title': source.get('title'), 'source_type': source.get('source_type'),
                                  'trust_tier': (source.get('trust') or {}).get('tier'), 'url': source.get('url')},
                       'sections': [{'candidate_id': s['candidate_id'], 'heading_path': s['heading_path'], 'heading': s['heading'],
                                     'versions': s['versions'], 'version_scope': s['version_scope'],
                                     'keyword_topics': s['topics'], 'decision_model_hint': self.hint(s), 'text': s['text']}
                                    for s in batch]}
            try:
                parsed = self.ask('facts', system, payload, fact_batch_schema(list(by_id), versions), FactBatch)
            except (ModelError, ValueError) as exc:
                self.fail({'stage': 'facts', 'candidate_ids': list(by_id), 'error': str(exc)})
                continue
            for draft in parsed.facts:
                section = by_id[draft.candidate_id]
                fact = draft.model_dump()
                reason = self.verify(fact, section)
                if reason:
                    rejected.append({**fact, 'reason': reason})
                    continue
                facts.append({'fact_id': f'f{len(facts) + 1:03d}', **fact, 'source_id': section['source_id'],
                              'source_type': source.get('source_type'), 'trust_tier': (source.get('trust') or {}).get('tier'),
                              'url': section['url'], 'heading': section['heading'], 'heading_path': section['heading_path']})
        return facts, rejected

    @staticmethod
    def hint(section):
        relevance = section.get('relevance') or {}
        if relevance.get('status') != 'scored':
            return None
        return {'label': relevance['label'], 'risk_type': relevance['risk_type'], 'note': 'Unverified model hint; the text decides.'}

    @staticmethod
    def verify(fact, section):
        if loose(fact['quote']) not in loose(section['heading'] + '\n' + section['text']):
            return 'Quote is not a contiguous passage of the cited section'
        if section['version_scope'] == 'version_heading' and not set(fact['versions']) <= set(section['versions']):
            return f"Versions {fact['versions']} contradict the section's version heading {section['versions']}"
        if not fact['versions']:
            fact['versions'] = list(section['versions'])
        return None

    # Stage B
    def group(self, facts, fill):
        """Code clusters facts sharing a normalized subject; the LLM merges clusters naming the same change
        differently (e.g. "render" and "ReactDOM.render"). Large upgrades are merged in chunks, never skipped."""
        if not facts:
            return []
        clusters = {}
        for fact in facts:
            clusters.setdefault(subject_key(fact['subject']) or fact['fact_id'], []).append(fact)
        items = [{'cluster_id': f'k{n:03d}', 'facts': members} for n, members in enumerate(clusters.values(), 1)]
        label = lambda members: f"{members[0]['subject']} ({', '.join(dict.fromkeys(m['change'] for m in members))})"
        groups = []
        for start in range(0, len(items), MAX_GROUPING_ITEMS):
            chunk = items[start:start + MAX_GROUPING_ITEMS]
            fallback = [{'label': label(i['facts']), 'fact_ids': [f['fact_id'] for f in i['facts']], 'method': 'subject_key'} for i in chunk]
            if len(chunk) == 1:
                groups += fallback
                continue
            payload = {'clusters': [{'cluster_id': i['cluster_id'], 'subjects': list(dict.fromkeys(f['subject'] for f in i['facts']))[:4],
                                     'changes': list(dict.fromkeys(f['change'] for f in i['facts'])),
                                     'versions': sorted({v for f in i['facts'] for v in f['versions']}),
                                     'explanation': i['facts'][0]['explanation'][:160], 'fact_count': len(i['facts'])} for i in chunk]}
            try:
                parsed = self.ask('grouping', GROUPING_SYSTEM.format(**fill), payload,
                                  grouping_schema([i['cluster_id'] for i in chunk]), Grouping)
            except (ModelError, ValueError) as exc:
                self.fail({'stage': 'grouping', 'error': str(exc)})
                groups += fallback
                continue
            by_id, assigned = {i['cluster_id']: i for i in chunk}, set()
            for group in parsed.groups:  # enforce a partition: first assignment wins, unassigned clusters stay alone
                ids = [c for c in dict.fromkeys(group.cluster_ids) if c not in assigned]
                if ids:
                    assigned.update(ids)
                    groups.append({'label': group.label, 'fact_ids': [f['fact_id'] for c in ids for f in by_id[c]['facts']],
                                   'method': 'llm' if len(items) <= MAX_GROUPING_ITEMS else 'llm_chunked'})
            groups += [{'label': label(i['facts']), 'fact_ids': [f['fact_id'] for f in i['facts']], 'method': 'unassigned_cluster'}
                       for i in chunk if i['cluster_id'] not in assigned]
        return groups

    # Stage C
    def build_paths(self, groups, facts, fill):
        paths, contextual = [], []
        system = PATHS_SYSTEM.format(**fill)
        progress = Progress(log, 'path groups', len(groups))
        for group in groups:
            progress.advance(group['label'][:60])
            cited = [facts[i] for i in group['fact_ids']]
            payload = {'group': group['label'], 'facts': [{k: f[k] for k in ('fact_id', 'subject', 'subject_kind', 'change', 'old_value',
                                                                            'new_value', 'versions', 'quote', 'explanation', 'certainty',
                                                                            'source_type', 'trust_tier')} for f in cited]}
            try:
                parsed = self.ask('paths', system, payload, path_batch_schema(group['fact_ids']), PathBatch)
            except (ModelError, ValueError) as exc:
                self.fail({'stage': 'paths', 'group': group['label'], 'fact_ids': group['fact_ids'], 'error': str(exc)})
                group['status'] = 'failed'
                continue
            group['status'] = 'paths' if parsed.paths else 'context_only'
            if not parsed.paths:
                contextual += [self.context_entry(f, parsed.not_a_risk_reason) for f in cited]
            paths += [(draft, facts) for draft in parsed.paths]
        return paths, contextual

    @staticmethod
    def evidence(fact):
        return {'source_id': fact['source_id'], 'source_type': fact['source_type'], 'quote_or_summary': fact['quote'],
                'relevance': fact['explanation'], 'fact_id': fact['fact_id'], 'candidate_id': fact['candidate_id'],
                'url': fact['url'], 'trust_tier': fact['trust_tier'], 'versions': fact['versions'], 'certainty': fact['certainty']}

    def context_entry(self, fact, reason=None):
        return {'fact_id': fact['fact_id'], 'statement': f"{fact['subject']}: {fact['explanation']}", 'subject': fact['subject'],
                'change': fact['change'], 'known_incompatibility': fact.get('known_incompatibility', False), 'versions': fact['versions'],
                'why_not_a_risk': reason or 'Marked as context at extraction (is_risk=false or change=context).',
                'evidence': [self.evidence(fact)]}

    @staticmethod
    def confidence(role, cited):
        tiers = {f['trust_tier'] for f in cited}
        certainty = {f['certainty'] for f in cited}
        if role == 'uncertain' or certainty == {'speculative'} or tiers <= {'third_party'}:
            return 'low'
        if certainty == {'explicit'} and 'official' in tiers:
            return 'high'
        return 'medium'

    @staticmethod
    def capped_level(level, cited, warnings):
        """Evidence bounds severity: the model may not rate a path above what its facts can carry."""
        cap, reason = None, None
        if cited and all(EXPERIMENTAL.search(' '.join([f['heading'], *f.get('heading_path', [])])) for f in cited):
            cap, reason = 'low', 'every cited fact comes from a section marked experimental, alpha, canary or unstable'
        elif cited and all(BUG_FIX.match(f.get('quote', '')) for f in cited):
            cap, reason = 'low', 'every cited fact is a bug fix shipped by the upgrade'
        elif cited and all(f.get('change') == 'security_fix' or SECURITY_FIX.search(f.get('quote', '')) for f in cited):
            cap, reason = 'low', 'every cited fact is a security fix shipped by the upgrade'
        elif cited and all(f['certainty'] != 'explicit' for f in cited):
            cap, reason = 'medium', 'no cited fact is explicit'
        if cap and level != 'unknown' and RISK_LEVELS.index(level) < RISK_LEVELS.index(cap):
            warnings.append(f'risk_level capped from {level} to {cap}: {reason}.')
            return cap
        return level

    # Stage D
    def assemble(self, header, candidates, inventory, drafts, contextual, selection, rejected, upstream, upstream_diagnostics=()):
        path_ids, risk_paths = set(), []
        for draft, facts in drafts:
            used, conditions = set(), []
            for condition in draft.conditions:
                cited = [facts[i] for i in dict.fromkeys(condition.fact_ids)]
                ambiguities = list(condition.ambiguities)
                spans = {tuple(f['versions']) for f in cited}
                if len(spans) > 1:
                    ambiguities.append(f'Cited facts tie this to different versions: {sorted(spans)}')
                conditions.append({'condition_id': unique(slug(condition.statement), used), 'statement': condition.statement,
                                   'category': condition.category, 'required': condition.condition_role == 'required',
                                   'condition_role': condition.condition_role, 'operator': condition.operator,
                                   'expected_value': condition.expected_value, 'evidence': [self.evidence(f) for f in cited],
                                   'reasoning': condition.reasoning, 'confidence': self.confidence(condition.condition_role, cited),
                                   'ambiguities': ambiguities, 'search_patterns': condition.search_patterns})
            checks = []
            if not any(c['category'] == 'version' for c in conditions):
                checks.append('No target-version condition.')
            if all(c['category'] == 'version' for c in conditions):
                checks.append('No application or deployment condition.')
            steps = [{'step': step.action, 'evidence': [self.evidence(facts[i]) for i in dict.fromkeys(step.fact_ids)]}
                     for step in draft.migration_steps]
            cited = {e['fact_id'] for c in conditions for e in c['evidence']} | {e['fact_id'] for st in steps for e in st['evidence']}
            level = self.capped_level(draft.risk_level, [facts[i] for i in cited], checks)
            kinds = {facts[i]['change'] for i in cited} | ({'known_issue'} if any(facts[i].get('known_incompatibility') for i in cited) else set())
            risk_paths.append({'path_id': unique(slug(draft.path_name), path_ids), 'path_name': draft.path_name,
                               'risk_type': draft.risk_type, 'risk_level': level,
                               'affected_surface': draft.affected_surface, 'description': draft.description,
                               'conditions': conditions, 'migration_steps': steps, 'suggested_actions': draft.suggested_actions,
                               'change_kinds': sorted(kinds), 'structural_warnings': checks})
        risk_paths.sort(key=lambda p: (RISK_LEVELS.index(p['risk_level']), p['path_name'].lower()))
        conditions = [c for p in risk_paths for c in p['conditions']]
        levels = Counter(c['confidence'] for c in conditions)
        if not conditions:
            overall = 'low'
        elif levels['high'] >= 0.6 * len(conditions) and not self.failures:
            overall = 'high'
        elif levels['low'] > len(conditions) / 2:
            overall = 'low'
        else:
            overall = 'medium'
        # Open questions are unknowns about the upgrade; diagnostics describe how this run went.
        questions, diagnostics = list(upstream), list(upstream_diagnostics)
        if selection['over_budget']:
            diagnostics.append(f"{len(selection['over_budget'])} relevant sections were not extracted (section budget {self.max_sections}).")
        if selection['selected_without_relevance_verdict']:
            diagnostics.append(f"{len(selection['selected_without_relevance_verdict'])} sections had no relevance verdict and were extracted anyway.")
        if rejected:
            diagnostics.append(f'{len(rejected)} extracted facts were rejected because their quote or versions did not match the section.')
        for failure in self.failures:
            diagnostics.append(f"Extraction {failure['stage']} step failed: {failure['error'][:200]}")
        questions += [f"Unconfirmed condition in '{p['path_name']}': {c['statement']}" for p in risk_paths
                      for c in p['conditions'] if c['condition_role'] == 'uncertain']
        types = Counter(p['risk_type'] for p in risk_paths)
        summary = (f"{header['project']} {header['current_version']} -> {header['target_version']} "
                   f"({', '.join(candidates['resolved_versions'])}): {len(risk_paths)} risk paths"
                   + (' — ' + ', '.join(f'{n} {t}' for t, n in types.most_common()) if types else '')
                   + f"; {sum(p['risk_level'] in ('critical', 'high') for p in risk_paths)} critical/high. "
                   f"{len(contextual)} contextual facts kept separately.")
        model = {**header, 'schema_version': SCHEMA_VERSION, 'generated_at': datetime.now(timezone.utc).isoformat(),
                'summary': summary, 'source_inventory': inventory['sources'], 'risk_paths': risk_paths,
                'contextual_facts': contextual, 'open_questions': questions, 'diagnostics': diagnostics, 'overall_confidence': overall,
                'confidence_rubric': CONFIDENCE_RUBRIC,
                'extraction': {'collection_path': candidates.get('collection_path'), 'resolved_versions': candidates['resolved_versions'],
                               'models': sorted(self.models),
                               'llm_calls': self.calls, 'usage': self.usage, 'selection': {k: v for k, v in selection.items() if k != 'over_budget'},
                               'sections_over_budget': len(selection['over_budget']), 'rejected_facts': len(rejected),
                               'failures': len(self.failures)}}
        # Enforce the output contract's allowed values (including code-computed confidence) before anything is written.
        RiskModelOut.model_validate(model)
        return model

