"""Verify saved risk models: python -m upgrade_pipeline verify [--judge decision|llm|none] DIR..."""
import argparse
import json
from contextlib import ExitStack
from pathlib import Path

from ..models import ModelRegistry, add_model_arguments
from . import DEFAULT_MAX_CONDITIONS, JUDGES, build_judge, write_verification


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directories', nargs='+', type=Path, help='Case directories containing risk_model.json')
    parser.add_argument('--judge', choices=JUDGES, default='decision',
                        help='Entailment judge: decision (judge decision model, default local d1; run with .venv-d1), '
                             'llm (judge LLM critic), or none (deterministic checks only); d1/jev force that backend')
    parser.add_argument('--max-conditions', type=int, default=DEFAULT_MAX_CONDITIONS)
    parser.add_argument('--offline', action='store_true', help='Load only cached d1 weights')
    add_model_arguments(parser, llm_stages=('judge',), decision_stages=('judge',))
    args = parser.parse_args(argv)
    if args.max_conditions < 1:
        parser.error('--max-conditions must be positive')
    with ExitStack() as stack:
        try:
            judge, _ = build_judge(args.judge, ModelRegistry(stack, offline=args.offline), args)
        except ValueError as exc:
            parser.error(f'--judge {args.judge}: {exc}')
        for directory in args.directories:
            read = lambda name: json.loads((directory / name).read_text())
            model, summary = write_verification(directory, {'files': {}}, read('risk_model.json'), read('extracted_facts.json'),
                                                read('evidence.json'), read('source_inventory.json'), read('candidate_sections.json'),
                                                judge, args.judge, args.max_conditions)
            body = summary['evidence_summary']
            print(json.dumps({'directory': str(directory), 'overall_confidence': model['overall_confidence'],
                              **{k: len(body[k]) for k in ('high_confidence_risks', 'medium_confidence_risks',
                                                           'low_confidence_or_disputed_risks', 'sources_with_disagreements',
                                                           'open_questions')}}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
