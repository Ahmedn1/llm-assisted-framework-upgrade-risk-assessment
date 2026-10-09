"""Steps after collection, shared by both collection paths: normalize, score relevance, extract, verify, finalize.

Each step records its outcome in the manifest. A failing step is recorded and never discards the collected
evidence or the output of earlier steps.
"""
from dataclasses import dataclass, field
from pathlib import Path

from .common import write_json
from .models import describe
from .extraction import upstream_notes, write_risk_model
from .final_output import write_final_output
from .report import write_report
from .normalize import write_normalized
from .relevance import assess_sections, summary as relevance_summary
from .verification import write_verification


@dataclass
class PostprocessConfig:
    """Models and budgets for the post-collection steps; a step runs only when its model is supplied."""
    relevance_backend: object = None
    relevance_max_sections: int = 200
    extraction_llm: object = None
    extraction_options: dict = field(default_factory=dict)  # max_sections and structured-output settings
    verification: dict | None = None  # {'judge': judge or None, 'kind': str, 'max_conditions': int}

    def __post_init__(self):
        if type(self.relevance_max_sections) is not int or self.relevance_max_sections < 1:
            raise ValueError('relevance_max_sections must be a positive integer')
        if self.verification is not None and self.extraction_llm is None:
            raise ValueError('Risk verification needs risk extraction in the same run')

    def describe(self):
        described = {'section_relevance': self.relevance_backend is not None, 'risk_extraction': self.extraction_llm is not None}
        if self.relevance_backend is not None:
            described.update(section_relevance_max_sections=self.relevance_max_sections,
                             decision_backend=describe(self.relevance_backend))
        if self.extraction_llm is not None:
            described['risk_extraction_options'] = self.extraction_options
        if self.verification is not None:
            described['risk_verification'] = {'judge': self.verification.get('kind', 'none'),
                                              'max_conditions': self.verification.get('max_conditions', 300)}
        return described


def run_postprocessing(output_dir, manifest, resolution, evidence, config, research_sources=()):
    """Run every configured step after collection; return the summary fields they contribute."""
    output_dir = Path(output_dir)
    try:
        inventory, candidates = write_normalized(output_dir, manifest, resolution, evidence, research_sources)
    except Exception as exc:
        manifest['normalization_error'] = f'{type(exc).__name__}: {exc}'
        return summary_fields(manifest)
    if config.relevance_backend is not None:
        score_relevance(output_dir, manifest, inventory, candidates, config)
    extracted = extract(output_dir, manifest, evidence, inventory, candidates, config) if config.extraction_llm is not None else None
    if extracted:
        model = extracted[0]
        if config.verification is not None:
            model = verify(output_dir, manifest, evidence, inventory, candidates, *extracted, config.verification) or model
        finalize(output_dir, manifest, model)
    return summary_fields(manifest)


def summary_fields(manifest):
    return {**manifest.get('section_relevance', {}), **manifest.get('risk_extraction', {}),
            **{'verification_' + k: v for k, v in manifest.get('risk_verification', {}).items()}}


def score_relevance(output_dir, manifest, inventory, candidates, config):
    try:
        record = assess_sections(candidates, inventory, config.relevance_backend, config.relevance_max_sections)
        write_json(output_dir / 'candidate_sections.json', candidates)
        manifest['section_relevance'] = relevance_summary(record)
    except Exception as exc:
        manifest['section_relevance_error'] = f'{type(exc).__name__}: {exc}'


def extract(output_dir, manifest, evidence, inventory, candidates, config):
    """Risk extraction; runs after relevance so it can skip sections judged not relevant."""
    try:
        model, trace = write_risk_model(output_dir, manifest, candidates, inventory, config.extraction_llm,
                                        upstream_notes(evidence), **config.extraction_options)
    except Exception as exc:
        manifest['risk_extraction_error'] = f'{type(exc).__name__}: {exc}'
        return None
    manifest['risk_extraction'] = {'risk_paths': len(model['risk_paths']), 'contextual_facts': len(model['contextual_facts']),
                                   'overall_confidence': model['overall_confidence'],
                                   'extraction_llm_calls': model['extraction']['llm_calls'],
                                   'rejected_facts': model['extraction']['rejected_facts'],
                                   'extraction_failures': model['extraction']['failures']}
    return model, trace


def verify(output_dir, manifest, evidence, inventory, candidates, model, trace, verification):
    """Verification: grounding, entailment, conflicts and evidence summary."""
    try:
        verified, summary = write_verification(output_dir, manifest, model, trace, evidence, inventory, candidates,
                                               verification.get('judge'), verification.get('kind', 'none'),
                                               verification.get('max_conditions', 300))
    except Exception as exc:
        manifest['risk_verification_error'] = f'{type(exc).__name__}: {exc}'
        return None
    body = summary['evidence_summary']
    manifest['risk_verification'] = {'overall_confidence': verified['overall_confidence'], 'judge': verification.get('kind'),
                                     **{k: len(body[k]) for k in ('high_confidence_risks', 'medium_confidence_risks',
                                                                  'low_confidence_or_disputed_risks', 'sources_with_disagreements')}}
    return verified


def finalize(output_dir, manifest, model):
    """Final deliverable, built from the verified model when verification ran."""
    try:
        output = write_final_output(output_dir, manifest, model)
    except Exception as exc:
        manifest['final_output_error'] = f'{type(exc).__name__}: {exc}'
        return
    try:
        write_report(output_dir, manifest, output)
    except Exception as exc:
        manifest['report_error'] = f'{type(exc).__name__}: {exc}'
