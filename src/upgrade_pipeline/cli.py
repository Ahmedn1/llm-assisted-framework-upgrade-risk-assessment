import argparse
import json
import re
import sys
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from .common import configure_logging, get_logger, write_json
from .models import (DECISION_STAGES, LLM_STAGES, ModelRegistry, add_model_arguments, resolve_decision,
                     resolve_llm)
from .postprocess import PostprocessConfig
from .report import build_index, index_row
from .runner import run_case
from .verification import JUDGES, build_judge


log = get_logger('run')


def run(argv=None):
    """upgrade-pipeline run: resolve, collect and run the post-collection steps for one project or a batch."""
    parser = argparse.ArgumentParser(prog='upgrade-pipeline run', description='Resolve upgrade versions, collect evidence, then score, extract and verify risks for one project or a batch')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--inputs', type=Path, help='JSON array of project/current_version/target_version objects')
    source.add_argument('--project')
    parser.add_argument('--current-version')
    parser.add_argument('--target-version')
    parser.add_argument('--output-dir', type=Path, default=Path('outputs/pipeline'))
    parser.add_argument('--cache-dir', type=Path, default=Path('.cache/upgrade-resolver'))
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--max-documents', type=int, default=100)
    parser.add_argument('--resolver-requests', type=int, default=100)
    parser.add_argument('--collector-requests', type=int, default=200)
    parser.add_argument('--max-urls', type=int, default=500)
    parser.add_argument('--max-depth', type=int, default=2)
    parser.add_argument('--web-search', choices=('auto', 'none', 'brave', 'duckduckgo'), default='auto')
    parser.add_argument('--evidence-search', choices=('none', 'auto', 'brave', 'duckduckgo'), default='none')
    parser.add_argument('--path', choices=('deterministic', 'llm'), default='deterministic',
                        help='Resolver and collector: heuristic deterministic path (default) or LLM-directed research')
    parser.add_argument('--scorer', choices=('heuristic', 'decision'), default='heuristic',
                        help='Version-source scorer for the deterministic resolver (decision uses the scorer decision model)')
    steps = parser.add_argument_group('Post-collection steps (on by default; --no-... to skip)')
    steps.add_argument('--section-relevance', action=argparse.BooleanOptionalAction, default=None,
                       help='Score collected evidence sections with the relevance decision model (default on)')
    steps.add_argument('--section-relevance-max-sections', type=int, default=200,
                       help='Maximum decision calls per case for section relevance, most trusted sources first')
    steps.add_argument('--extract-risks', action=argparse.BooleanOptionalAction, default=None,
                       help='Risk extraction with the extraction LLM (default on; skipped by default with --offline)')
    steps.add_argument('--extract-max-sections', type=int, default=150,
                       help='Maximum candidate sections per case sent to risk extraction, most relevant first')
    steps.add_argument('--verify-risks', action=argparse.BooleanOptionalAction, default=None,
                       help='Verification and evidence summary (default on whenever extraction runs)')
    steps.add_argument('--verify-judge', choices=JUDGES, default='decision',
                       help='Entailment judge: decision (judge decision model, default local d1), llm (judge LLM critic), '
                            'none (deterministic checks only); d1 and jev force that decision backend')
    steps.add_argument('--verify-max-conditions', type=int, default=300, help='Maximum judged conditions per case')
    parser.add_argument('--llm-max-steps', type=int, default=12, help='Maximum LLM calls per stage on the LLM path')
    parser.add_argument('--structured-mode', choices=('json_schema', 'json_object', 'prompt'), default='json_schema')
    parser.add_argument('--max-output-tokens', type=int, default=8192)
    parser.add_argument('--token-parameter', choices=('max_completion_tokens', 'max_tokens'), default='max_completion_tokens')
    add_model_arguments(parser, llm_stages=tuple(LLM_STAGES), decision_stages=tuple(DECISION_STAGES))
    args = parser.parse_args(argv)
    if args.path == 'llm' and args.offline:
        parser.error('--path llm needs inference access; use an explicitly injected local client for offline Python runs')
    if args.llm_max_steps < 1 or args.max_output_tokens < 1:
        parser.error('LLM budgets must be positive')
    if args.offline and args.refresh:
        parser.error('--offline and --refresh conflict')
    if args.scorer == 'decision' and args.path == 'llm':
        parser.error('--scorer decision applies to the deterministic resolver; the LLM path selects sources itself')
    if min(args.extract_max_sections, args.verify_max_conditions, args.section_relevance_max_sections) < 1:
        parser.error('Section and condition budgets must be positive')
    if min(args.max_documents, args.resolver_requests, args.collector_requests, args.max_urls) < 1 or args.max_depth < 0:
        parser.error('Budgets must be positive and depth nonnegative')
    # Default workflow: decision-model relevance, LLM extraction, decision-model verification. Steps that were
    # only on by default and need the network are skipped offline with a notice; explicit requests are errors.
    notes = []
    if args.section_relevance is None:
        args.section_relevance = True
    if args.extract_risks is None:
        args.extract_risks = not args.offline
        if args.offline:
            notes.append('risk extraction and verification skipped: they need an LLM, and --offline was given')
    elif args.extract_risks and args.offline:
        parser.error('--extract-risks needs inference access; run python -m upgrade_pipeline extract on saved output instead')
    if args.verify_risks is None:
        args.verify_risks = args.extract_risks
    elif args.verify_risks and not args.extract_risks:
        parser.error('--verify-risks needs risk extraction in the same run; verify saved output with python -m upgrade_pipeline verify')
    try:
        if args.inputs:
            if args.current_version or args.target_version:
                parser.error('Batch versions belong in --inputs')
            inputs = json.loads(args.inputs.read_text())
        else:
            inputs = [{'project': args.project, 'current_version': args.current_version, 'target_version': args.target_version}]
        if not isinstance(inputs, list) or not inputs:
            raise ValueError('Inputs must be a nonempty array')
        for item in inputs:
            if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k].strip() for k in ('project', 'current_version', 'target_version')):
                raise ValueError('Every case needs project, current_version and target_version strings')
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    stack = ExitStack()
    registry = ModelRegistry(stack, offline=args.offline)
    models, settings = {}, {}
    try:
        if args.scorer == 'decision':
            settings['scorer'] = resolve_decision('scorer', args)
            models['scorer'] = registry.decision(settings['scorer'])
        if args.section_relevance:
            settings['relevance'] = resolve_decision('relevance', args)
            models['relevance'] = registry.decision(settings['relevance'])
        if args.path == 'llm':
            settings['collection'] = resolve_llm('collection', args)
            models['collection'] = registry.llm(settings['collection'])
        if args.extract_risks:
            settings['extraction'] = resolve_llm('extraction', args)
            models['extraction'] = registry.llm(settings['extraction'])
        if args.verify_risks:
            judge, settings['judge'] = build_judge(args.verify_judge, registry, args, {
                'structured_mode': args.structured_mode, 'max_output_tokens': args.max_output_tokens,
                'token_parameter': args.token_parameter})
            models['verification'] = {'judge': judge, 'kind': args.verify_judge, 'max_conditions': args.verify_max_conditions}
    except ValueError as exc:
        stack.close()
        parser.error(f'{exc}. Disable a step with --no-section-relevance, --no-extract-risks or --no-verify-risks.')
    for note in notes:
        log.warning(note)
    for stage, chosen in settings.items():
        if chosen is not None:
            d = chosen.describe()
            log.info('%s uses %s %s%s%s (model from %s)', stage, d['kind'], d['model'],
                     f"@{d['revision'][:12]}" if d.get('revision') else '', f" at {d['base_url']}" if d.get('base_url') else '',
                     d['resolved_from']['model'])
    args.model_settings = {stage: chosen.describe() for stage, chosen in settings.items() if chosen is not None}
    with stack:
        return run_cases(args, inputs, models)


def run_cases(args, inputs, models):
    rows, reports = [], []
    for index, item in enumerate(inputs, 1):
        slug = re.sub(r'[^a-z0-9.-]+', '-', item['project'].lower()).strip('.-') or 'project'
        directory = args.output_dir / f'{index:02d}-{slug}'
        log.info('[%d/%d] %s %s -> %s', index, len(inputs), item['project'], item['current_version'], item['target_version'])
        result = run_case(item, directory, cache_dir=args.cache_dir, offline=args.offline, refresh=args.refresh,
                          max_documents=args.max_documents, resolver_requests=args.resolver_requests,
                          collector_requests=args.collector_requests, max_urls=args.max_urls, max_depth=args.max_depth,
                          web_search=args.web_search, evidence_search=args.evidence_search,
                          scorer=args.scorer, decision_backend=models.get('scorer'), llm_client=models.get('collection'),
                          postprocess=PostprocessConfig(
                              relevance_backend=models.get('relevance'), relevance_max_sections=args.section_relevance_max_sections,
                              extraction_llm=models.get('extraction'), verification=models.get('verification'),
                              extraction_options={'max_sections': args.extract_max_sections, 'structured_mode': args.structured_mode,
                                                  'max_output_tokens': args.max_output_tokens, 'token_parameter': args.token_parameter}),
                          model_settings=getattr(args, 'model_settings', None),
                          execution_path=args.path, llm_max_steps=args.llm_max_steps,
                          structured_mode=args.structured_mode, max_output_tokens=args.max_output_tokens,
                          token_parameter=args.token_parameter)
        row = {**item, 'directory': str(directory), **result['summary']}
        if result.get('files', {}).get('final_output'):
            # One folder holding every upgrade's deliverable, named by project and interval.
            final = args.output_dir / 'final' / f"{slug}_{item['current_version']}_to_{item['target_version']}.json"
            final.parent.mkdir(parents=True, exist_ok=True)
            final.write_text((directory / 'final_output.json').read_text())
            row['final_output'] = str(final)
            if result['files'].get('report'):
                report = final.with_suffix('.html')
                report.write_text((directory / 'report.html').read_text())
                row['report'] = str(report)
                reports.append((json.loads(final.read_text()), report.name))
                (final.parent / 'index.html').write_text(build_index([index_row(o, href) for o, href in reports]))
        rows.append(row)
        write_json(args.output_dir / 'summary.json', {'generated_at': datetime.now(timezone.utc).isoformat(),
                   'completed_cases': len(rows), 'total_cases': len(inputs), 'results': rows})
        fields = ['project', 'pipeline_status', 'resolver_status', 'version_count', 'document_count', 'versions_with_direct_evidence', 'retrieval_failures']
        if args.path == 'llm':
            fields += ['versions_with_llm_cited_evidence', 'validation_failures', 'inference_failures']
        if args.section_relevance:
            fields += ['section_relevance_scored', 'section_relevance_failed', 'section_relevance_labels']
        if args.extract_risks:
            fields += ['risk_paths', 'contextual_facts', 'overall_confidence', 'rejected_facts', 'extraction_failures']
        if args.verify_risks:
            fields += ['verification_overall_confidence', 'verification_high_confidence_risks', 'verification_medium_confidence_risks',
                       'verification_low_confidence_or_disputed_risks', 'verification_sources_with_disagreements']
        print(json.dumps({k: row.get(k) for k in fields}), flush=True)
    return 0 if all(r['pipeline_status'] == 'completed_partial' for r in rows) else 2



COMMANDS = {
    'run': ('upgrade_pipeline.cli', 'run', 'resolve, collect and run the post-collection steps (default)'),
    'normalize': ('upgrade_pipeline.normalize', 'main', 'rebuild source_inventory.json and candidate_sections.json'),
    'relevance': ('upgrade_pipeline.relevance', 'main', 'score candidate sections with the relevance decision model'),
    'extract': ('upgrade_pipeline.extraction.__main__', 'main', 'extract risk paths with the extraction LLM'),
    'verify': ('upgrade_pipeline.verification.__main__', 'main', 'verify a risk model and write evidence_summary.json'),
    'final': ('upgrade_pipeline.final_output', 'main', 'rebuild final_output.json from a saved risk model'),
    'report': ('upgrade_pipeline.report', 'main', 'write the interactive report.html (and a batch index.html)'),
    'evaluate': ('upgrade_pipeline.evaluation', 'main', 'check risk models against a gold expectations file'),
    'schemas': ('upgrade_pipeline.cli', 'schemas', 'regenerate or check schemas/*.schema.json'),
}


def schemas(argv=None):
    """upgrade-pipeline schemas: write the JSON Schemas of risk_model.json and final_output.json."""
    from .extraction.models import risk_model_json_schema
    from .final_output import final_output_json_schema
    parser = argparse.ArgumentParser(prog='upgrade-pipeline schemas', description=schemas.__doc__)
    parser.add_argument('--dir', type=Path, default=Path('schemas'))
    parser.add_argument('--check', action='store_true', help='Exit 1 if a committed schema differs instead of writing it')
    args = parser.parse_args(argv)
    stale = []
    for name, schema in (('risk_model', risk_model_json_schema()), ('final_output', final_output_json_schema())):
        path = args.dir / f'{name}.schema.json'
        text = json.dumps(schema, indent=2) + '\n'
        if args.check:
            if not path.exists() or path.read_text() != text:
                stale.append(str(path))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            print(f'wrote {path}')
    if stale:
        print('stale: ' + ', '.join(stale) + ' (run upgrade-pipeline schemas)', file=sys.stderr)
    return 1 if stale else 0


def main(argv=None):
    """Dispatch a subcommand; arguments without one run the full pipeline, as before subcommands existed."""
    import importlib
    argv = list(sys.argv[1:] if argv is None else argv)
    level = 'info'
    if '-q' in argv or '--quiet' in argv:
        level = 'warning'
        argv = [a for a in argv if a not in ('-q', '--quiet')]
    if '--log-level' in argv:
        at = argv.index('--log-level')
        level = argv[at + 1] if at + 1 < len(argv) else level
        del argv[at:at + 2]
    configure_logging(level)
    if argv[:1] in (['-h'], ['--help'], []):
        print('usage: upgrade-pipeline [COMMAND] [ARGS...]   (no command: run)\n\ncommands:')
        for name, (_, _, summary) in COMMANDS.items():
            print(f'  {name:10} {summary}')
        print('\nupgrade-pipeline COMMAND --help shows each command\'s options.')
        print('Progress is logged to stderr; --log-level debug|info|warning (or -q) applies to every command.')
        return 0
    command = argv.pop(0) if argv[0] in COMMANDS else 'run'
    module, function, _ = COMMANDS[command]
    return getattr(importlib.import_module(module), function)(argv)
