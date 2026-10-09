"""Artifact-producing alternative pipeline; model configuration is explicit."""
from pathlib import Path

from evidence_collector.transport import CollectionHttpClient
from inference import LLMClient
from upgrade_resolver.versions import validate_interval
from ..common import write_json
from .agent import ResearchAgent


def run_llm_case(item, output_dir, manifest, *, cache_dir, offline, refresh, resolver_requests,
                 collector_requests, max_documents, max_urls, web_search, llm_client=None,
                 llm_max_steps=12, structured_mode='json_schema', max_output_tokens=8192,
                 token_parameter='max_completion_tokens', postprocess=None):
    from ..postprocess import PostprocessConfig, run_postprocessing
    from ..runner import summarize
    postprocess = postprocess or PostprocessConfig()
    steps = {}
    stage = 'llm_configuration'
    owned = None
    client = None
    agent = None
    try:
        validate_interval(item['current_version'], item['target_version'], item.get('version_mode', 'family'))
        if not item['project'].strip() or len(item['project']) > 150:
            raise ValueError('Project name must contain 1–150 characters')
        if offline and llm_client is None:
            raise ValueError('Offline CLI cannot make inference calls; inject an explicit local/mock client through Python')
        if llm_client is None:
            from ..models import resolve_llm
            owned = LLMClient(resolve_llm('collection').config())
            llm_client = owned
        client = CollectionHttpClient(Path(cache_dir), offline=offline, refresh=refresh,
            max_requests=resolver_requests, max_bytes=10_000_000)
        agent = ResearchAgent(client, llm_client, search=web_search, max_steps=llm_max_steps,
            max_sources=max_urls, max_documents=max_documents, structured_mode=structured_mode,
            max_output_tokens=max_output_tokens, token_parameter=token_parameter,
            checkpoint=lambda trace: write_json(output_dir / 'agent-trace.json', trace))
        stage = 'llm_resolver'
        resolution = agent.resolution(item)
        write_json(output_dir / 'resolution.json', resolution)
        manifest['files']['resolution'] = 'resolution.json'
        evidence = None
        if resolution['versions'] and resolution['selected_catalog']:
            stage = 'llm_collector'
            client.max_requests = client.request_count + collector_requests
            evidence = agent.collection(item, resolution)
            write_json(output_dir / 'evidence.json', evidence)
            manifest['files']['evidence'] = 'evidence.json'
            steps = run_postprocessing(output_dir, manifest, resolution, evidence, postprocess,
                                       list(agent.retriever.sources.values()))
        manifest['summary'] = summarize(resolution, evidence)
        manifest['summary']['llm_calls'] = agent.calls
        manifest['summary']['execution_path'] = 'llm'
        manifest['summary']['llm_usage'] = [entry['usage'] for entry in agent.trace if 'usage' in entry]
        manifest['summary'].update(steps)
    except Exception as exc:
        manifest['summary'] = {'pipeline_status': 'failed', 'failed_stage': stage,
            'error_type': type(exc).__name__, 'error': str(exc), 'execution_path': 'llm',
            'llm_calls': agent.calls if agent else 0}
    finally:
        if agent:
            write_json(output_dir / 'agent-trace.json', agent.trace)
            write_json(output_dir / 'research-sources.json', list(agent.retriever.sources.values()))
            manifest['files'].update(trace='agent-trace.json', research_sources='research-sources.json')
        if owned:
            owned.close()
        if client:
            client.close()
    write_json(output_dir / 'manifest.json', manifest)
    return manifest
