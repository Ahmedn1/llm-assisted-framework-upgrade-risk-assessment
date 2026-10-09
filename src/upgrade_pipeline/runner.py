from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from upgrade_resolver.http import HttpClient
from upgrade_resolver.resolver import resolve
from evidence_collector import collect
from evidence_collector.transport import CollectionHttpClient
from .common import get_logger, write_json
from .postprocess import PostprocessConfig, run_postprocessing


def summarize(resolution, evidence):
    summary = {'resolver_status': resolution['status'], 'selected_catalog': resolution.get('selected_catalog'),
               'version_count': len(resolution['versions']), 'milestones': resolution['documentation_milestones'],
               'resolver_coverage': resolution['coverage'], 'resolver_warnings': resolution['warnings'],
               'resolver_network_requests': resolution['network_requests'],
               'collector_status': 'not_run', 'document_count': 0}
    errors = resolution.get('failures', []) + (evidence.get('failures', []) if evidence else [])
    retrieval_errors = [e for e in errors if e.get('kind', 'retrieval') == 'retrieval']
    summary.update(retrieval_failures=len(retrieval_errors), failure_examples=retrieval_errors[:5],
                   validation_failures=sum(e.get('kind') == 'validation' for e in errors),
                   inference_failures=sum(e.get('kind') == 'inference' for e in errors))
    if evidence is None:
        summary['pipeline_status'] = 'blocked_at_resolution'
        summary['reason'] = 'No selected version catalog/interval; collector was not run against ambiguous sources.'
        return summary
    docs = evidence['documents']
    # Count association strength separately from incidental text mentions.
    direct_bases = {'resolver_release_record', 'registry_version', 'release_tag_match'}
    direct = {a['version'] for d in docs for a in d['version_associations'] if a['basis'] in direct_bases}
    narrative = {a['version'] for d in docs if d['document_type'] in ('release_notes', 'github_release')
                 for a in d['version_associations'] if a['basis'] in direct_bases}
    coverage = evidence['version_coverage']
    missing = [v for v, entry in coverage.items() if not entry['document_ids']]
    metadata_only = [v for v, entry in coverage.items() if entry['document_types'] == ['registry_metadata']]
    summary.update(pipeline_status=('completed_partial' if len(missing) < len(coverage) else 'collected_unassociated') if docs else 'no_evidence',
                   collector_status=evidence['status'], document_count=len(docs),
                   document_types=dict(Counter(d['document_type'] for d in docs)),
                   versions_with_candidate_evidence=len(coverage) - len(missing),
                   versions_with_direct_evidence=len(direct), versions_with_direct_release_notes=len(narrative),
                   versions_without_evidence=missing,
                   versions_with_only_registry_metadata=metadata_only,
                   upgrade_guide_urls=[d['url'] for d in docs if d['document_type'] == 'upgrade_guide'],
                   retrieval_failures=len(retrieval_errors), failure_examples=retrieval_errors[:5],
                   pending_urls=len(evidence['pending_urls']), skipped_urls=len(evidence['skipped']),
                   collector_network_requests=evidence['network_requests'],
                   collector_warnings=evidence['warnings'],
                   llm_calls=resolution.get('llm_calls', 0) + evidence['llm_calls'])
    if evidence.get('path') == 'llm':
        cited = {a['version'] for d in docs for a in d['version_associations'] if a['basis'] == 'llm_cited_relevance'}
        release_cited = {v for d in docs for claim in d.get('relevance_evidence', [])
                         if claim.get('document_type', d['document_type']) in ('release_notes', 'github_release')
                         for v in claim['versions']}
        without_notes = [v for v in coverage if v not in release_cited]
        summary.update(versions_with_llm_cited_evidence=len(cited), versions_without_release_notes=without_notes)
        if docs and without_notes:
            summary['pipeline_status'] = 'insufficient_evidence'
            summary['reason'] = 'Candidate documents exist, but some resolved versions lack substantive release-note evidence.'
    return summary


log = get_logger('case')


def run_case(item, output_dir, *, cache_dir=Path('.cache/upgrade-resolver'), offline=False,
             refresh=False, max_documents=100, resolver_requests=100, collector_requests=200,
             max_urls=500, max_depth=2, web_search='auto', evidence_search='none',
             scorer='heuristic', decision_backend=None, execution_path='deterministic', llm_client=None,
             llm_max_steps=12, structured_mode='json_schema', max_output_tokens=8192,
             token_parameter='max_completion_tokens', postprocess=None, model_settings=None):
    """Save each stage before continuing. A partial selection can collect; ambiguity cannot.
    `postprocess` (a PostprocessConfig) selects the steps after collection; by default none run."""
    postprocess = postprocess or PostprocessConfig()
    if execution_path not in ('deterministic', 'llm'):
        raise ValueError('execution_path must be deterministic or llm')
    if type(llm_max_steps) is not int or llm_max_steps < 1 or type(max_output_tokens) is not int or max_output_tokens < 1:
        raise ValueError('LLM action and output budgets must be positive integers')
    if execution_path == 'llm' and (scorer != 'heuristic' or decision_backend is not None):
        raise ValueError('The LLM path does not use the source scoring backend')
    if offline and refresh:
        raise ValueError('offline and refresh cannot both be enabled')
    if any(type(v) is not int or v < 1 for v in (max_documents, resolver_requests, collector_requests, max_urls)):
        raise ValueError('Request/document/URL limits must be positive integers')
    if max_depth < 0:
        raise ValueError('max_depth must be nonnegative')
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {'generated_at': datetime.now(timezone.utc).isoformat(), 'input': item,
                'configuration': {'offline': offline, 'refresh': refresh, 'scorer': scorer,
                    'web_search': web_search, 'evidence_search': evidence_search,
                    'max_documents': max_documents, 'resolver_requests': resolver_requests,
                    'collector_requests': collector_requests, 'max_urls': max_urls, 'max_depth': max_depth,
                    'execution_path': execution_path, **postprocess.describe()},
                'files': {}}
    if model_settings:
        manifest['configuration']['models'] = model_settings  # resolved per stage; API keys are never recorded
    if execution_path == 'llm':
        from .research.runner import run_llm_case
        for unused in ('scorer', 'max_depth', 'evidence_search'):
            manifest['configuration'].pop(unused, None)
        manifest['configuration'].update(llm_max_steps=llm_max_steps,
            structured_mode=structured_mode, max_output_tokens=max_output_tokens, token_parameter=token_parameter)
        return run_llm_case(item, output_dir, manifest, cache_dir=cache_dir, offline=offline, refresh=refresh,
            resolver_requests=resolver_requests, collector_requests=collector_requests, max_documents=max_documents,
            max_urls=max_urls, web_search=web_search, llm_client=llm_client, llm_max_steps=llm_max_steps,
            structured_mode=structured_mode, max_output_tokens=max_output_tokens, token_parameter=token_parameter,
            postprocess=postprocess)
    stage = 'resolver'
    log.info('%s %s -> %s: resolving versions (scorer %s)', item['project'], item['current_version'], item['target_version'], scorer)
    try:
        client = HttpClient(Path(cache_dir), offline=offline, refresh=refresh, max_requests=resolver_requests)
        try:
            resolution = resolve(client, item['project'], item['current_version'], item['target_version'],
                                 seed_urls=item.get('source_urls', []), mode=item.get('version_mode', 'family'),
                                 web_search=web_search, scorer=scorer, decision_backend=decision_backend)
        finally:
            client.close()
        write_json(output_dir / 'resolution.json', resolution)
        manifest['files']['resolution'] = 'resolution.json'
        log.info('resolution %s: %d versions from %s', resolution['status'], len(resolution['versions']),
                 resolution.get('selected_catalog') or 'no catalog')
        evidence = None
        if resolution['status'] in ('resolved', 'partial') and resolution.get('selected_catalog') and resolution['versions']:
            stage = 'collector'
            log.info('collecting evidence (up to %d documents, %d requests)', max_documents, collector_requests)
            client = CollectionHttpClient(Path(cache_dir), offline=offline, refresh=refresh,
                                          max_requests=collector_requests, max_bytes=10_000_000)
            try:
                evidence = collect(client, resolution, max_documents=max_documents, max_urls=max_urls,
                                   max_depth=max_depth, search=evidence_search)
            finally:
                client.close()
            write_json(output_dir / 'evidence.json', evidence)
            manifest['files']['evidence'] = 'evidence.json'
            registry = sum(d['document_type'] == 'registry_metadata' for d in evidence['documents'])
            log.info('collected %d documents (%d registry records), %d failures, %d URLs pending',
                     len(evidence['documents']), registry, len(evidence['failures']), len(evidence['pending_urls']))
            stage = 'postprocessing'
            steps = run_postprocessing(output_dir, manifest, resolution, evidence, postprocess)
        else:
            log.warning('resolution %s: nothing to collect', resolution['status'])
        manifest['summary'] = summarize(resolution, evidence)
        if evidence is not None:
            manifest['summary'].update(steps)
        log.info('case %s', manifest['summary']['pipeline_status'])
    except Exception as exc:
        log.error('%s step failed: %s: %s', stage, type(exc).__name__, exc)
        # Batch boundary: preserve the failed stage and allow the other projects to run.
        manifest['summary'] = {'pipeline_status': 'failed', 'failed_stage': stage,
                               'error_type': type(exc).__name__, 'error': str(exc)}
    write_json(output_dir / 'manifest.json', manifest)
    return manifest
