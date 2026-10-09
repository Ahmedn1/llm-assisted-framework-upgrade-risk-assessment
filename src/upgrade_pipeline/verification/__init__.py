"""Evidence grounding and verification of extracted risk models."""
from pathlib import Path

from ..common import write_json

from .judges import JUDGES, build_judge
from .verify import DEFAULT_MAX_CONDITIONS, Verifier

__all__ = ['DEFAULT_MAX_CONDITIONS', 'JUDGES', 'Verifier', 'build_judge', 'write_verification']


def write_verification(output_dir, manifest, risk_model, trace, evidence, inventory, candidates, judge=None,
                       judge_kind='none', max_conditions=DEFAULT_MAX_CONDITIONS):
    model, report, summary = Verifier(judge, judge_kind=judge_kind, max_conditions=max_conditions).run(
        risk_model, trace, evidence, inventory, candidates)
    write_json(Path(output_dir) / 'risk_model.json', model)
    write_json(Path(output_dir) / 'grounding_report.json', report)
    write_json(Path(output_dir) / 'evidence_summary.json', summary)
    manifest['files'].update(risk_model='risk_model.json', grounding_report='grounding_report.json',
                             evidence_summary='evidence_summary.json')
    return model, summary
