"""Upgrade-risk extraction over candidate sections."""
import json
from pathlib import Path

from ..common import write_json

from .extract import DEFAULT_MAX_SECTIONS, RiskExtractor, select_sections

__all__ = ['DEFAULT_MAX_SECTIONS', 'RiskExtractor', 'select_sections', 'write_risk_model']


def write_risk_model(output_dir, manifest, candidates, inventory, llm, upstream=((), ()), **options):
    model, trace = RiskExtractor(llm, **options).run(candidates, inventory, *upstream)
    write_json(Path(output_dir) / 'risk_model.json', model)
    write_json(Path(output_dir) / 'extracted_facts.json', trace)
    manifest['files'].update(risk_model='risk_model.json', extracted_facts='extracted_facts.json')
    return model, trace


def upstream_notes(evidence):
    """Collector results carried into the risk model: (open questions about the upgrade, run diagnostics).
    A version without any evidence is a question about the upgrade; the collector's own gaps describe the run."""
    if not evidence:
        return [], []
    questions = [f'No evidence was collected for version {v}; its changes are unknown.'
                 for v, c in evidence.get('version_coverage', {}).items() if not c.get('document_ids')]
    return questions, [f'Collection: {q}' for q in evidence.get('open_questions') or []]
