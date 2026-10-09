import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from conftest import FakeClient
from inference import LLMClient, ModelConfig
from upgrade_pipeline.research import ResearchAgent
from upgrade_pipeline.research.models import ResolutionAction as Action, CollectionAction
from upgrade_pipeline.research.retrieval import Retriever
from upgrade_resolver.http import FetchError

URL = 'https://orbit.org/releases'
SID = 'src_' + hashlib.sha256(URL.encode()).hexdigest()[:16]
ITEM = {'project': 'Orbit', 'current_version': '1', 'target_version': '2'}
QUOTE = 'Orbit 2.0 and 2.1 require new config.'


def route(url, accept):
    if url.endswith('robots.txt'):
        return 'User-agent: *\nAllow: /'
    if url == URL:
        return '<title>Orbit releases</title><main><h1>Versions</h1><p>1.0 2.0 2.1 3.0</p><a href="/releases/1.0">1.0</a><a href="/releases/2.0">2.0</a><a href="/releases/2.1">2.1</a><a href="/releases/3.0">3.0</a><p>' + QUOTE + '</p></main>'
    if 'duckduckgo.com' in url:
        return '<a class="result__a" href="' + URL + '">Orbit</a>'


def finish_versions(**changes):
    return Action(action='finish', selected_source_id=SID, rationale='Official project release page',
        versions=[{'version': v, 'source_id': SID, 'quote': v} for v in ['1.0', '2.0', '2.1']], **changes)


def finish_docs(**changes):
    return CollectionAction(action='finish', documents=[{'source_id': SID, 'versions': ['2.0', '2.1'],
        'document_type': 'upgrade_guide', 'quote': QUOTE, 'relevance': 'Both versions require configuration migration', **changes}])


def covered_finish_docs(**changes):
    # A finish is only accepted early once every resolved version has release-note evidence.
    action = finish_docs(**changes)
    action.documents.append(action.documents[0].model_copy(update={'document_type': 'release_notes'}))
    return action


class ScriptedModel:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return SimpleNamespace(parsed=next(self.actions), model='fixture', usage={'total_tokens': 12}, request_id=None)


def test_real_inference_adapter_runs_both_stages_and_groups_shared_document(monkeypatch):
    monkeypatch.delenv('BRAVE_SEARCH_API_KEY', raising=False)
    actions = iter([Action(action='search', query='Orbit releases'), Action(action='fetch', url=URL), finish_versions(), covered_finish_docs()])
    requests = []
    def model_response(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={'model': 'fixture', 'usage': {'total_tokens': 12},
            'choices': [{'finish_reason': 'stop', 'message': {'content': next(actions).model_dump_json()}}]})
    with httpx.Client(transport=httpx.MockTransport(model_response)) as transport:
        with LLMClient(ModelConfig(model='fixture', base_url='http://localhost:8000/v1'), http_client=transport) as llm:
            agent = ResearchAgent(FakeClient(route), llm, max_steps=3)
            resolution = agent.resolution(ITEM)
            evidence = agent.collection(ITEM, resolution)
    assert [v['version'] for v in resolution['versions']] == ['2.0', '2.1']
    assert resolution['status'] == 'partial'
    assert evidence['documents_by_version']['2.0'][0]['id'] == evidence['documents_by_version']['2.1'][0]['id']
    assert resolution['llm_calls'] + evidence['llm_calls'] == 4
    assert all(r['response_format']['type'] == 'json_schema' for r in requests)
    assert evidence['documents'][0]['relevance_evidence'][0]['quote'] == QUOTE


def test_unfetched_citations_are_rejected_and_budget_exhaustion_is_unresolved():
    agent = ResearchAgent(FakeClient(route), ScriptedModel([finish_versions()]), max_steps=1)
    result = agent.resolution(ITEM)
    assert result['status'] == 'unresolved' and not result['versions']
    assert agent.trace[0]['error']
    assert 'budget exhausted' in result['warnings'][-1]


def test_bad_citation_can_be_repaired_in_next_action():
    invalid = finish_versions()
    invalid.versions[0].quote = 'invented 1.0'
    agent = ResearchAgent(FakeClient(route), ScriptedModel([Action(action='fetch', url=URL), invalid, finish_versions()]), max_steps=3)
    assert agent.resolution(ITEM)['versions']
    assert 'Citation must quote' in agent.trace[1]['error']


def test_collection_rejects_unresolved_version_associations():
    agent = ResearchAgent(FakeClient(route), ScriptedModel([finish_docs(versions=['9.0'])]), max_steps=1)
    agent.retriever.fetch(URL)
    evidence = agent.collection(ITEM, {'versions': [{'version': '2.0'}], 'selected_catalog': URL, 'input': ITEM, 'status': 'partial', 'coverage': {}})
    assert not evidence['documents']
    assert 'resolved version labels' in agent.trace[-1]['error']


def test_unseen_page_tail_cannot_be_cited_until_fetched():
    retriever = Retriever(FakeClient(lambda url, _: 'User-agent: *\nAllow: /' if url.endswith('robots.txt') else '<main><p>' + 'x' * 13000 + '</p><p>TAIL EVIDENCE</p></main>'))
    observed = retriever.fetch(URL)
    with pytest.raises(ValueError, match='observed fetch'):
        retriever.cite(observed['source_id'], 'TAIL EVIDENCE')
    retriever.fetch(URL, offset=12000)
    assert retriever.cite(observed['source_id'], 'TAIL EVIDENCE')


def test_private_retrieval_and_robots_disallow_are_blocked():
    client = FakeClient(lambda *_: 'User-agent: *\nDisallow: /')
    retriever = Retriever(client)
    with pytest.raises(FetchError):
        retriever.fetch('http://127.0.0.1/secrets')
    assert not client.calls
    with pytest.raises(FetchError, match='robots.txt disallows'):
        retriever.fetch(URL)
    assert len(client.calls) == 1


def test_pipeline_saves_compatible_artifacts_without_deterministic_resolver(tmp_path, monkeypatch):
    from upgrade_pipeline.runner import run_case
    class Client(FakeClient):
        def __init__(self, *args, **kwargs):
            super().__init__(route)
        def close(self):
            pass
    monkeypatch.setattr('upgrade_pipeline.research.runner.CollectionHttpClient', Client)
    monkeypatch.setattr('upgrade_pipeline.runner.resolve', lambda *a, **k: pytest.fail('deterministic resolver invoked'))
    model = ScriptedModel([Action(action='fetch', url=URL), finish_versions(), covered_finish_docs()])
    result = run_case(ITEM, tmp_path, execution_path='llm', llm_client=model, llm_max_steps=2)
    assert result['summary']['pipeline_status'] == 'completed_partial'
    assert result['summary']['llm_calls'] == 3
    for name in ['resolution.json', 'evidence.json', 'agent-trace.json', 'research-sources.json', 'manifest.json']:
        assert (tmp_path / name).exists()


def test_registry_projection_keeps_available_versions():
    retriever = Retriever(FakeClient(lambda *_: {'info': {'name': 'Orbit'}, 'releases': {'1.0': [{}], '2.0': [{}], '3.0': []}}))
    result = retriever.fetch('https://pypi.org/pypi/Orbit/json')
    assert result['projection'] == 'registry_inventory'
    assert '2.0' in result['text'] and '3.0' not in result['text']


def test_blocked_search_is_not_retried_and_removed_from_action_schema():
    from upgrade_pipeline.research.models import ResolutionRetrievalAction
    client = FakeClient(lambda *_: '<form id="challenge-form"></form>')
    model = ScriptedModel([Action(action='search', query='Orbit'),
                           Action(action='search', query='Orbit official releases'),
                           Action(action='finish', gaps=['Search unavailable'])])
    agent = ResearchAgent(client, model, search='duckduckgo', max_steps=3)
    assert agent.resolution(ITEM)['status'] == 'unresolved'
    assert len(client.calls) == 1
    assert model.calls[1][1]['output_model'] is ResolutionRetrievalAction
    assert [f['kind'] for f in agent.failures] == ['retrieval', 'validation']
    assert not json.loads(model.calls[1][0][1]['content'])['search_available']


def test_upgrade_heading_is_not_a_release_inventory():
    client = FakeClient(lambda url, _: 'User-agent: *\nAllow: /' if url.endswith('robots.txt') else
                        '<title>How to Upgrade to Orbit 2</title><main><h1>How to Upgrade to Orbit 2</h1><p>Use the new configuration API.</p></main>')
    finish = Action(action='finish', selected_source_id=SID, rationale='Official guide',
                    versions=[{'version': '2', 'source_id': SID, 'quote': 'How to Upgrade to Orbit 2'}])
    agent = ResearchAgent(client, ScriptedModel([Action(action='fetch', url=URL), finish]), max_steps=2)
    assert agent.resolution(ITEM)['status'] == 'unresolved'
    assert 'not a recognized release inventory' in agent.trace[-1]['error']


def test_inventory_finish_cannot_omit_observed_patch_release():
    finish = finish_versions()
    finish.versions = [v for v in finish.versions if v.version != '2.1']
    agent = ResearchAgent(FakeClient(route), ScriptedModel([Action(action='fetch', url=URL), finish]), max_steps=2)
    assert not agent.resolution(ITEM)['versions']
    assert '2.1' in agent.trace[-1]['error']


def test_long_title_is_not_substantive_document_evidence():
    quote = 'Official Orbit release notes and upgrade documentation'
    client = FakeClient(lambda url, _: 'User-agent: *\nAllow: /' if url.endswith('robots.txt') else
                        f'<title>{quote}</title><main><h1>{quote}</h1><p>Actual body information.</p></main>')
    retriever = Retriever(client)
    source = retriever.fetch(URL)
    with pytest.raises(ValueError, match='Title-only'):
        retriever.cite_body(source['source_id'], quote)
    assert retriever.cite_body(source['source_id'], 'Actual body information.')


def test_sentence_period_after_version_is_allowed():
    def with_sentence(url, accept):
        result = route(url, accept)
        return result.replace('1.0 2.0 2.1 3.0', 'Release 1.0. Release 2.0. Release 2.1.') if url == URL else result
    finish = finish_versions()
    for claim in finish.versions:
        claim.quote = 'Release ' + claim.version + '.'
    agent = ResearchAgent(FakeClient(with_sentence), ScriptedModel([Action(action='fetch', url=URL), finish]), max_steps=2)
    assert len(agent.resolution(ITEM)['versions']) == 2


def test_summary_separates_cross_stage_retrieval_validation_and_inference_errors():
    from upgrade_pipeline.runner import summarize
    resolution = {'status': 'partial', 'versions': [{'version': '2.0'}], 'documentation_milestones': ['2'],
                  'coverage': {}, 'warnings': [], 'network_requests': 1,
                  'failures': [{'kind': 'retrieval', 'stage': 'resolution', 'error': 'bot challenge'},
                               {'kind': 'validation', 'error': 'invalid version'}]}
    evidence = {'status': 'partial', 'path': 'llm', 'documents': [],
                'version_coverage': {'2.0': {'document_ids': [], 'document_types': []}},
                'failures': [{'kind': 'validation', 'error': 'bad quote'}, {'kind': 'inference', 'error': 'HTTP 401'}],
                'pending_urls': [], 'skipped': [], 'warnings': [], 'network_requests': 0, 'llm_calls': 2}
    summary = summarize(resolution, evidence)
    assert summary['retrieval_failures'] == 1
    assert summary['validation_failures'] == 2
    assert summary['inference_failures'] == 1
    assert summary['failure_examples'][0]['stage'] == 'resolution'
    assert summary['versions_with_llm_cited_evidence'] == 0


def collection_resolution():
    return {'versions': [{'version': '2.0'}, {'version': '2.1'}], 'selected_catalog': URL,
            'input': ITEM, 'status': 'partial', 'coverage': {}}


def test_stage_schemas_do_not_allow_each_others_results():
    from pydantic import ValidationError
    from upgrade_pipeline.research.models import ResolutionAction, CollectionAction
    assert 'documents' not in ResolutionAction.model_json_schema()['properties']
    assert 'versions' not in CollectionAction.model_json_schema()['properties']
    assert 'selected_source_id' not in CollectionAction.model_json_schema()['properties']
    with pytest.raises(ValidationError):
        CollectionAction(action='finish', versions=[])
    with pytest.raises(ValidationError):
        ResolutionAction(action='finish', documents=[])


def test_empty_collection_is_rejected_then_agent_fetches_and_recovers():
    model = ScriptedModel([
        CollectionAction(action='finish', gaps=['Inventory is enough']),
        CollectionAction(action='fetch', url=URL), finish_docs()])
    agent = ResearchAgent(FakeClient(route), model, max_steps=3)
    # A stale resolver observation must not become the collector's objective.
    agent.observations.append({'result': {'accepted': True, 'resolver_only_marker': True}})
    evidence = agent.collection(ITEM, collection_resolution())
    assert len(evidence['documents']) == 1
    assert evidence['llm_calls'] == 3
    assert evidence['failures'][0]['kind'] == 'validation'
    assert 'Premature empty collection' in evidence['failures'][0]['error']
    state = json.loads(model.calls[0][0][1]['content'])
    assert not state['recent_observations']
    assert state['context']['evidence_checklist']['release_notes_for_versions'] == ['2.0', '2.1']
    assert 'Resolution is COMPLETE' in model.calls[0][0][0]['content']
    assert issubclass(model.calls[0][1]['output_model'], CollectionAction)


def test_collection_can_finish_empty_when_action_budget_is_exhausted():
    model = ScriptedModel([CollectionAction(action='fetch', url='https://orbit.org/missing'),
                           CollectionAction(action='finish', gaps=['Release documentation could not be fetched'])])
    agent = ResearchAgent(FakeClient(route), model, max_steps=2)
    evidence = agent.collection(ITEM, collection_resolution())
    assert not evidence['documents']
    assert evidence['failures'][0]['kind'] == 'retrieval'
    assert agent.trace[-1]['result']['accepted']


def test_collection_can_finish_empty_when_http_budget_is_exhausted():
    client = FakeClient(route)
    client.max_requests = client.request_count = 1
    agent = ResearchAgent(client, ScriptedModel([CollectionAction(action='finish', gaps=['HTTP budget exhausted'])]), max_steps=12)
    evidence = agent.collection(ITEM, collection_resolution())
    assert not evidence['documents'] and not evidence['failures']
    assert evidence['llm_calls'] == 1


def test_collection_does_not_require_redundant_fetch_when_evidence_exists():
    from upgrade_pipeline.research.models import CollectionRetrievalAction
    model = ScriptedModel([covered_finish_docs()])
    client = FakeClient(route)
    agent = ResearchAgent(client, model, search='none', max_steps=12)
    agent.retriever.fetch(URL)
    requests = len(client.calls)
    result = agent.collection(ITEM, collection_resolution())
    assert result['documents'] and len(client.calls) == requests
    assert issubclass(model.calls[0][1]['output_model'], CollectionRetrievalAction)


def test_collection_schema_restricts_versions_to_resolved_labels():
    from pydantic import ValidationError
    from upgrade_pipeline.research.models import collection_schema
    schema = collection_schema(['2.0', '2.1'], False)
    assert schema.model_validate(finish_docs().model_dump())
    with pytest.raises(ValidationError):
        schema.model_validate(finish_docs(versions=['2']).model_dump())


def test_valid_claim_survives_bad_sibling_and_budget_exhaustion():
    action = finish_docs()
    invalid = action.documents[0].model_copy(update={'quote': 'This sentence was never in the fetched document.'})
    action.documents.append(invalid)
    agent = ResearchAgent(FakeClient(route), ScriptedModel([action]), max_steps=1)
    agent.retriever.fetch(URL)
    result = agent.collection(ITEM, collection_resolution())
    assert len(result['documents']) == 1
    assert len(result['documents'][0]['relevance_evidence']) == 1
    assert 'claim_index' in result['failures'][0]['error']
    assert result['rejected_claims'][0]['claim_index'] == 1
    assert agent.trace[-1]['result']['accepted']


def test_evidence_on_fetch_actions_is_saved_and_multiple_roles_are_allowed():
    action = finish_docs()
    action.documents.append(action.documents[0].model_copy(update={'document_type': 'deprecation_notice'}))
    fetch = CollectionAction(action='fetch', url='https://orbit.org/missing', documents=action.documents)
    agent = ResearchAgent(FakeClient(route), ScriptedModel([fetch]), max_steps=1)
    agent.retriever.fetch(URL)
    result = agent.collection(ITEM, collection_resolution())
    doc = result['documents'][0]
    assert set(doc['document_types']) == {'upgrade_guide', 'deprecation_notice'}
    assert len(doc['relevance_evidence']) == 2
    assert result['failures'][0]['kind'] == 'retrieval'


def test_whitespace_variation_is_allowed_but_skipped_passages_are_not():
    client = FakeClient(lambda url, _: 'User-agent: *\nAllow: /' if url.endswith('robots.txt') else
                        '<title>Upgrade</title><main><p>First substantive sentence.</p><p>Important condition.</p><p>Last substantive sentence.</p></main>')
    retriever = Retriever(client)
    source = retriever.fetch(URL)
    assert retriever.cite_body(source['source_id'], 'First substantive sentence.\n\nImportant condition.')
    with pytest.raises(ValueError, match='observed fetch'):
        retriever.cite_body(source['source_id'], 'First substantive sentence.\nLast substantive sentence.')


def test_release_note_role_does_not_leak_to_other_version_associations():
    from upgrade_pipeline.runner import summarize
    action = finish_docs()
    action.documents.append(action.documents[0].model_copy(update={'document_type': 'release_notes', 'versions': ['2.0']}))
    agent = ResearchAgent(FakeClient(route), ScriptedModel([action]), max_steps=1)
    agent.retriever.fetch(URL)
    resolution = {**collection_resolution(), 'documentation_milestones': ['2'], 'warnings': [], 'network_requests': 0}
    evidence = agent.collection(ITEM, resolution)
    summary = summarize(resolution, evidence)
    assert summary['versions_without_release_notes'] == ['2.1']
    assert 'release_notes' not in evidence['version_coverage']['2.1']['document_types']


GUIDE = 'https://orbit.org/upgrade'


def two_page_route(url, accept):
    if url == GUIDE:
        return '<title>Upgrade guide</title><main><p>Guide-only sentence: rename config.cache to config.store.</p></main>'
    return route(url, accept)


def test_quote_found_in_another_observed_source_is_reattributed():
    action = finish_docs(quote='Guide-only sentence: rename config.cache to config.store.')
    agent = ResearchAgent(FakeClient(two_page_route), ScriptedModel([action]), max_steps=1)
    agent.retriever.fetch(URL)
    guide = agent.retriever.fetch(GUIDE)
    result = agent.collection(ITEM, collection_resolution())
    assert [d['id'] for d in result['documents']] == [guide['source_id']]
    assert result['documents'][0]['relevance_evidence'][0]['cited_source_corrected_from'] == SID


def test_stitched_quote_error_shows_where_source_diverges():
    client = FakeClient(lambda url, _: 'User-agent: *\nAllow: /' if url.endswith('robots.txt') else
                        '<title>Upgrade</title><main><p>First substantive sentence keeps going.</p><p>Important condition.</p></main>')
    retriever = Retriever(client)
    source = retriever.fetch(URL)
    with pytest.raises(ValueError, match="first 40 characters match.*'Important condition") as error:
        retriever.cite_body(source['source_id'], 'First substantive sentence keeps going. Invented continuation.')
    assert 'Invented continuation' in str(error.value)


def test_unrepaired_claims_are_reported_after_one_repair_round():
    action = covered_finish_docs()
    action.documents.append(action.documents[0].model_copy(update={'quote': 'This sentence was never in the fetched document.'}))
    model = ScriptedModel([action, action])
    agent = ResearchAgent(FakeClient(route), model, max_steps=12)
    agent.retriever.fetch(URL)
    result = agent.collection(ITEM, collection_resolution())
    assert result['llm_calls'] == 2 and len(result['documents']) == 1
    assert result['rejected_claims'][0]['claim_index'] == 2
    assert 'Repair only these claims' in result['failures'][0]['error']


def test_rejected_finishes_do_not_evict_fetched_text():
    bad = CollectionAction(action='finish', documents=[{'source_id': SID, 'versions': ['2.0'], 'document_type': 'upgrade_guide',
        'quote': 'This sentence was never in the fetched document.', 'relevance': 'x'}])
    model = ScriptedModel([CollectionAction(action='fetch', url=URL)] + [CollectionAction(action='finish', gaps=['none'])] * 4 + [bad])
    agent = ResearchAgent(FakeClient(route), model, max_steps=6)
    agent.collection(ITEM, collection_resolution())
    state = json.loads(model.calls[-1][0][1]['content'])
    assert QUOTE in json.dumps(state['recent_observations'])


def test_finish_is_rejected_while_versions_lack_release_notes_and_budget_remains():
    partial = finish_docs(document_type='release_notes', versions=['2.0'])
    model = ScriptedModel([partial, CollectionAction(action='fetch', url=URL), covered_finish_docs()])
    agent = ResearchAgent(FakeClient(route), model, max_steps=12)
    agent.retriever.fetch(URL)
    result = agent.collection(ITEM, collection_resolution())
    assert "lack release-note evidence: ['2.1']" in agent.trace[0]['error']
    assert agent.trace[-1]['result']['accepted'] and result['llm_calls'] == 3
    assert result['version_coverage']['2.1']['document_ids']


def test_partial_release_note_coverage_is_accepted_at_last_action():
    agent = ResearchAgent(FakeClient(route), ScriptedModel([finish_docs(document_type='release_notes', versions=['2.0'])]), max_steps=1)
    agent.retriever.fetch(URL)
    result = agent.collection(ITEM, collection_resolution())
    assert agent.trace[-1]['result']['accepted'] and not result['version_coverage']['2.1']['document_ids']


def test_raw_markdown_changelog_keeps_jsx_text_and_outlines_in_range_versions():
    changelog = '## 3.0.0\n\n- Adds `<Portal>` API.\n\n## 2.1.0\n\n- Removes `<Legacy>` root.\n\n## 2.0.0\n\n- New root API.\n\n## 1.0.0\n\n- Initial.\n'
    retriever = Retriever(FakeClient(lambda url, _: 'User-agent: *\nAllow: /' if url.endswith('robots.txt') else changelog))
    retriever.interval = ('1', '2', 'family')
    result = retriever.fetch('https://raw.githubusercontent.com/orbit/orbit/main/CHANGELOG.md')
    assert result['projection'] == 'markdown_text' and '`<Legacy>` root' in result['text']
    assert [o['heading'] for o in result['version_outline']] == ['2.1.0', '2.0.0', '1.0.0']
    assert changelog[result['version_outline'][0]['offset']:].startswith('2.1.0')
    assert retriever.cite_body(result['source_id'], 'Removes `<Legacy>` root.')


CHANGELOG = '## 2.1.0 (May 1)\n\n### Core\n\n* Removes `legacyRoot` option.\n\n## 2.0.0 (April 1)\n\n* New root API.\n\nGeneric note for everyone.\n'


def changelog_retriever():
    retriever = Retriever(FakeClient(lambda url, _: 'User-agent: *\nAllow: /' if url.endswith('robots.txt') else CHANGELOG))
    return retriever, retriever.fetch('https://raw.githubusercontent.com/orbit/orbit/main/CHANGELOG.md')['source_id']


def test_quote_may_lead_with_its_version_headings():
    retriever, sid = changelog_retriever()
    assert retriever.cite_body(sid, '## 2.1.0 (May 1)\n\n### Core\n\n* Removes `legacyRoot` option.')
    with pytest.raises(ValueError, match='heading-only'):
        retriever.cite_body(sid, '## 2.1.0 (May 1)\n\n### Core')


def test_release_note_claim_must_be_tied_to_each_version_it_names():
    from upgrade_pipeline.research.claims import EvidenceAccumulator
    from upgrade_pipeline.research.models import DocumentClaim
    retriever, sid = changelog_retriever()
    claim = {'source_id': sid, 'document_type': 'release_notes', 'relevance': 'x'}
    accumulator = EvidenceAccumulator(retriever, ['2.0', '2.1'], 10)
    errors = accumulator.ingest([
        DocumentClaim(**claim, versions=['2.1'], quote='* Removes `legacyRoot` option.'),       # named by its heading
        DocumentClaim(**claim, versions=['2.0', '2.1'], quote='Generic note for everyone.'),  # only under 2.0.0
        DocumentClaim(**{**claim, 'document_type': 'upgrade_guide'}, versions=['2.0', '2.1'], quote='Generic note for everyone.')])
    assert accumulator.release_note_versions() == {'2.1'}
    assert len(errors) == 1 and "['2.1'] are not named" in errors[0]['error']


def test_identical_content_at_another_url_is_flagged_like_the_heuristic_path():
    mirror = 'https://mirror.orbit.org/releases'
    agent = ResearchAgent(FakeClient(lambda url, accept: route(URL if url == mirror else url, accept)),
                          ScriptedModel([covered_finish_docs()]), max_steps=1)
    agent.retriever.fetch(URL)
    copy = agent.retriever.fetch(mirror)
    agent.llm.actions = iter([CollectionAction(action='finish', documents=[
        *covered_finish_docs().documents, *[d.model_copy(update={'source_id': copy['source_id']}) for d in covered_finish_docs().documents]])])
    result = agent.collection(ITEM, collection_resolution())
    original, duplicate = result['documents']
    assert duplicate['duplicate_content_of'] == original['id'] and not duplicate['sections'] and not duplicate['text']
    grouped = {d['id']: d for d in result['documents_by_version']['2.0']}
    assert grouped[duplicate['id']]['sections'] == original['sections']
    assert len(duplicate['relevance_evidence']) == 2


def test_collector_schemas_only_allow_observed_source_ids_and_inventory_labels():
    from pydantic import ValidationError
    agent = ResearchAgent(FakeClient(route), ScriptedModel([]), max_steps=1)
    agent.retriever.interval = ('1', '2', 'family')
    assert agent.action_schema('resolution', None).model_fields['selected_source_id'].annotation is str  # nothing fetched yet
    agent.retriever.fetch(URL)
    resolution = agent.action_schema('resolution', None)
    assert resolution.model_validate(finish_versions().model_dump())
    for bad in (finish_versions().model_copy(update={'selected_source_id': 'src_invented'}),
                Action(action='finish', selected_source_id=SID, rationale='r',
                       versions=[{'version': '3.0', 'source_id': SID, 'quote': '3.0'}])):  # out-of-range label
        with pytest.raises(ValidationError):
            resolution.model_validate(bad.model_dump())
    assert resolution.model_validate(Action(action='fetch', url=URL).model_dump())  # unused id field may stay empty
    collection = agent.action_schema('collection', {'versions': ['2.0', '2.1']})
    assert collection.model_validate(finish_docs().model_dump())
    with pytest.raises(ValidationError):
        collection.model_validate(finish_docs(source_id='src_invented').model_dump())


def test_single_value_literals_are_sent_as_enums():
    from inference.chat import _strict_schema
    from upgrade_pipeline.research.models import collection_schema
    schema = _strict_schema(collection_schema(['2.0'], True, [SID]).model_json_schema())
    claim = next(d for name, d in schema['$defs'].items() if name.startswith('ScopedDocumentClaim'))
    assert claim['properties']['source_id'] == {'enum': [SID], 'title': 'Source Id', 'type': 'string'}
    assert claim['properties']['versions']['items']['enum'] == ['2.0']
