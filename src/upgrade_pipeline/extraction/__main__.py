"""Extract risk models from saved case directories: python -m upgrade_pipeline extract DIR..."""
import argparse
import json
from contextlib import ExitStack
from pathlib import Path

from ..models import ModelRegistry, add_model_arguments, resolve_llm
from . import DEFAULT_MAX_SECTIONS, upstream_notes, write_risk_model


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directories', nargs='+', type=Path, help='Case directories containing candidate_sections.json')
    parser.add_argument('--max-sections', type=int, default=DEFAULT_MAX_SECTIONS)
    parser.add_argument('--structured-mode', choices=('json_schema', 'json_object', 'prompt'), default='json_schema')
    parser.add_argument('--max-output-tokens', type=int, default=8192)
    parser.add_argument('--token-parameter', choices=('max_completion_tokens', 'max_tokens'), default='max_completion_tokens')
    add_model_arguments(parser, llm_stages=('extraction',))
    args = parser.parse_args(argv)
    if args.max_sections < 1 or args.max_output_tokens < 1:
        parser.error('Budgets must be positive')
    with ExitStack() as stack:
        try:
            llm = ModelRegistry(stack).llm(resolve_llm('extraction', args))
        except ValueError as exc:
            parser.error(str(exc))
        for directory in args.directories:
            read = lambda name: json.loads((directory / name).read_text())
            evidence = read('evidence.json') if (directory / 'evidence.json').exists() else None
            manifest = {'files': {}}
            model, _ = write_risk_model(directory, manifest, read('candidate_sections.json'), read('source_inventory.json'), llm,
                                     upstream_notes(evidence), max_sections=args.max_sections,
                                     structured_mode=args.structured_mode, max_output_tokens=args.max_output_tokens,
                                     token_parameter=args.token_parameter)
            print(json.dumps({'directory': str(directory), 'risk_paths': len(model['risk_paths']),
                              'contextual_facts': len(model['contextual_facts']), 'overall_confidence': model['overall_confidence'],
                              **{k: model['extraction'][k] for k in ('llm_calls', 'rejected_facts', 'failures', 'sections_over_budget')}}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
