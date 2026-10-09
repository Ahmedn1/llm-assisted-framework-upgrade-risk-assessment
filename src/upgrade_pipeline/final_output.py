"""The per-upgrade deliverable, final_output.json, in the output contract's shape.

risk_model.json is the working file (extraction metadata, fact and section ids, full verification detail).
The final output keeps the contract's fields at every level plus the agreed extensions, and is validated
before writing. Its summary is composed by code from the validated model, so it states nothing new.
"""
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field

from .common import get_logger, write_json
from .extraction.models import EvidenceSummaryOut
from .schema import (CATEGORIES, CHANGES, CONFIDENCE, GROUNDING, OPERATORS, RISK_LEVELS, RISK_TYPES, ROLES, SURFACES,
                     TRUST_TIERS, VERDICTS, Strict)

SCHEMA_VERSION = 'final-output-1.0'
CONFIDENCE_ORDER = ('high', 'medium', 'low', None)


class FinalEvidence(Strict):
    source_id: str
    source_type: str | None
    quote_or_summary: str = Field(min_length=1)
    relevance: str
    url: str
    versions: list[str]
    trust_tier: Literal[TRUST_TIERS] | None


class FinalVerification(Strict):
    grounding: Literal[GROUNDING]
    entailment: Literal[VERDICTS + ('not_checked',)]
    independent_sources: int
    conflicts: list[str]


class FinalCondition(Strict):
    condition_id: str
    statement: str
    category: Literal[CATEGORIES]
    required: bool
    condition_role: Literal[ROLES]
    operator: Literal[OPERATORS]
    expected_value: str
    evidence: list[FinalEvidence] = Field(min_length=1)
    reasoning: str
    confidence: Literal[CONFIDENCE]
    ambiguities: list[str]
    search_patterns: list[str]
    verification: FinalVerification | None = Field(description='Null when the risk model was not verified')


class FinalMigrationStep(Strict):
    step: str
    evidence: list[FinalEvidence] = Field(min_length=1)


class FinalRiskPath(Strict):
    path_id: str
    path_name: str
    risk_type: Literal[RISK_TYPES]
    risk_level: Literal[RISK_LEVELS]
    affected_surface: Literal[SURFACES]
    description: str
    conditions: list[FinalCondition] = Field(min_length=1)
    suggested_actions: list[str]
    migration_steps: list[FinalMigrationStep]
    change_kinds: list[Literal[CHANGES]]
    confidence: Literal[CONFIDENCE] | None = Field(description='Path confidence from verification; null if not verified')
    flags: list[str] = Field(description='Verification flags and structural warnings')


class FinalContextualFact(Strict):
    statement: str
    subject: str
    change: Literal[CHANGES]
    known_incompatibility: bool
    versions: list[str]
    why_not_a_risk: str
    evidence: list[FinalEvidence] = Field(min_length=1)


class FinalSource(Strict):
    source_id: str
    title: str
    url: str
    source_type: str
    retrieved_at: str | None
    why_relevant: str
    trust_tier: Literal[TRUST_TIERS]
    trust_basis: str
    versions: list[str]
    cited_by_risk_paths: int


class Provenance(Strict):
    schema_version: str
    generated_at: str
    collection_path: str | None
    extraction_models: list[str]
    verified: bool
    verification_judge: str | None
    working_files: list[str]


class FinalOutput(Strict):
    project: str
    current_version: str
    target_version: str
    summary: str
    source_inventory: list[FinalSource]
    risk_paths: list[FinalRiskPath]
    contextual_facts: list[FinalContextualFact]
    open_questions: list[str] = Field(description='Unknowns about the upgrade itself')
    diagnostics: list[str] = Field(description='How the run went: budgets, rejected extractions, failed model calls')
    overall_confidence: Literal[CONFIDENCE]
    evidence_summary: EvidenceSummaryOut | None
    provenance: Provenance


def final_output_json_schema():
    schema = FinalOutput.model_json_schema()
    schema['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
    schema['title'] = 'Upgrade risk final output'
    return schema


def evidence(item):
    return {k: item.get(k) for k in ('source_id', 'source_type', 'quote_or_summary', 'relevance', 'url', 'versions', 'trust_tier')}


def condition(c):
    v = c.get('verification')
    return {**{k: c[k] for k in ('condition_id', 'statement', 'category', 'required', 'condition_role', 'operator', 'expected_value',
                                 'reasoning', 'confidence', 'ambiguities', 'search_patterns')},
            'evidence': [evidence(e) for e in c['evidence']],
            'verification': {'grounding': v['grounding'], 'entailment': v['entailment']['verdict'],
                             'independent_sources': v['independent_source_count'], 'conflicts': v['conflicts']} if v else None}


def summary(model, paths, verified):
    """Deterministic: every number and name below is read from the validated model."""
    versions = model['extraction'].get('resolved_versions') or []  # resolver order is ascending
    span = f" ({versions[0]}–{versions[-1]})" if len(versions) > 1 else f' ({versions[0]})' if versions else ''
    levels = Counter(p['risk_level'] for p in paths)
    types = Counter(p['risk_type'] for p in paths)
    text = (f"{model['project']} {model['current_version']} → {model['target_version']}{span}: {len(paths)} risk paths"
            + (' (' + ', '.join(f'{levels[l]} {l}' for l in RISK_LEVELS if levels[l]) + ')' if paths else '') + '.')
    if types:
        text += ' Main categories: ' + ', '.join(f'{t.replace("_", " ")} ({n})' for t, n in types.most_common(4)) + '.'
    severe = [p for p in paths if p['risk_level'] in ('critical', 'high')][:3]
    if severe:
        text += ' Most severe: ' + '; '.join(f"{p['path_name']} ({p['risk_level']})" for p in severe) + '.'
    if verified:
        body = model['evidence_summary']['evidence_summary']
        text += (f" Verified: {len(body['high_confidence_risks'])} high-, {len(body['medium_confidence_risks'])} medium- and "
                 f"{len(body['low_confidence_or_disputed_risks'])} low-confidence or disputed risks; "
                 f"{len(body['sources_with_disagreements'])} source disagreements.")
    else:
        text += ' Not verified (verification not run); confidence is preliminary.'
    return text + f" {len(model['open_questions'])} open questions. Overall confidence: {model['overall_confidence']}."


# Models saved before diagnostics existed mixed run notes into open_questions. These patterns are the exact
# messages earlier versions wrote; they are sorted out when such a model is rebuilt.
LEGACY_DIAGNOSTIC = re.compile(r'relevant sections were not (?:processed|extracted)|extracted facts were rejected|'
                               r'^Extraction \w+ step failed|sections had no relevance verdict|^No entailment judge ran|'
                               r'conditions were not judged|^The judge failed|contextual facts cite evidence')
LEGACY_SINGLE = re.compile(r"^'(.*)' is a \w+ risk supported by a single source")
LEGACY_DROPPED = re.compile(r'^Version \S+ has (?:candidate sections but no extracted risk|no candidate sections)')


def split_legacy_notes(model):
    """(open questions, diagnostics); models with a diagnostics field are already split."""
    if 'diagnostics' in model:
        return model['open_questions'], model['diagnostics']
    from .verification.verify import single_source_question
    questions = [q for q in model['open_questions'] if not LEGACY_DIAGNOSTIC.search(q) and not LEGACY_DROPPED.search(q)]
    single = [m.group(1) for q in questions for m in [LEGACY_SINGLE.match(q)] if m]
    questions = [q for q in questions if not LEGACY_SINGLE.match(q)] + ([single_source_question(single)] if single else [])
    return questions, [q for q in model['open_questions'] if LEGACY_DIAGNOSTIC.search(q)]


def build_final_output(model):
    questions, diagnostics = split_legacy_notes(model)
    model = {**model, 'open_questions': questions}
    if model.get('evidence_summary'):
        body = model['evidence_summary']['evidence_summary']
        model['evidence_summary'] = {**model['evidence_summary'], 'evidence_summary': {**body, 'open_questions': questions}}
    verified = model.get('evidence_summary') is not None
    rank = lambda p: (RISK_LEVELS.index(p['risk_level']), CONFIDENCE_ORDER.index(p.get('confidence')), p['path_name'].lower())
    paths = sorted(model['risk_paths'], key=rank)
    cited = Counter()
    for p in paths:
        sources = {e['source_id'] for c in p['conditions'] for e in c['evidence']} | \
                  {e['source_id'] for s in p.get('migration_steps', []) for e in s['evidence']}
        cited.update(sources)
    output = {
        'project': model['project'], 'current_version': model['current_version'], 'target_version': model['target_version'],
        'summary': summary(model, paths, verified),
        'source_inventory': [{**{k: s.get(k) for k in ('source_id', 'title', 'url', 'source_type', 'retrieved_at', 'why_relevant')},
                              'trust_tier': s['trust']['tier'], 'trust_basis': s['trust']['basis'], 'versions': s.get('versions', []),
                              'cited_by_risk_paths': cited[s['source_id']]} for s in model['source_inventory']],
        'risk_paths': [{**{k: p[k] for k in ('path_id', 'path_name', 'risk_type', 'risk_level', 'affected_surface', 'description',
                                             'suggested_actions', 'change_kinds')},
                        'conditions': [condition(c) for c in p['conditions']],
                        'migration_steps': [{'step': s['step'], 'evidence': [evidence(e) for e in s['evidence']]}
                                            for s in p.get('migration_steps', [])],
                        'confidence': p.get('confidence'),
                        'flags': p.get('verification_flags') or list(p.get('structural_warnings', []))} for p in paths],
        'contextual_facts': [{**{k: c[k] for k in ('statement', 'subject', 'change', 'versions', 'why_not_a_risk')},
                              'known_incompatibility': c.get('known_incompatibility', False),
                              'evidence': [evidence(e) for e in c['evidence']]} for c in model['contextual_facts']],
        'open_questions': questions, 'diagnostics': diagnostics,
        'overall_confidence': model['overall_confidence'],
        'evidence_summary': model.get('evidence_summary'),
        'provenance': {'schema_version': SCHEMA_VERSION, 'generated_at': datetime.now(timezone.utc).isoformat(),
                       'collection_path': model['extraction'].get('collection_path'),
                       'extraction_models': model['extraction'].get('models', []), 'verified': verified,
                       'verification_judge': (model.get('verification') or {}).get('judge'),
                       'working_files': ['risk_model.json', 'extracted_facts.json', 'candidate_sections.json', 'source_inventory.json',
                                         'evidence.json'] + (['grounding_report.json', 'evidence_summary.json'] if verified else [])}}
    FinalOutput.model_validate(output)
    return output


def write_final_output(output_dir, manifest, model):
    output = build_final_output(model)
    write_json(Path(output_dir) / 'final_output.json', output)
    manifest['files']['final_output'] = 'final_output.json'
    get_logger('final').info('wrote %s: %d risk paths, overall confidence %s', Path(output_dir) / 'final_output.json',
                             len(output['risk_paths']), output['overall_confidence'])
    return output


def main(argv=None):
    """Rebuild final_output.json from saved risk models: python -m upgrade_pipeline final DIR..."""
    directories = [Path(p) for p in (argv if argv is not None else sys.argv[1:])]
    if not directories:
        print(main.__doc__, file=sys.stderr)
        return 2
    for directory in directories:
        output = write_final_output(directory, {'files': {}}, json.loads((directory / 'risk_model.json').read_text()))
        print(json.dumps({'directory': str(directory), 'risk_paths': len(output['risk_paths']),
                          'verified': output['provenance']['verified'], 'overall_confidence': output['overall_confidence']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
