import json

import pytest

from evidence_collector.extraction import html_document, markdown_document
from upgrade_pipeline.normalize import normalize_case
from upgrade_pipeline.postprocess import PostprocessConfig
from upgrade_pipeline.relevance import assess_sections
from pipeline_fixtures import ITEM, RESOLUTION, KeywordBackend, document, evidence

CHANGELOG = ('# Changelog\n## 3.0.0\n### Core\n* Removed `legacyRoot`.\n## 2.1.0\n### Core\n* `render` has been deprecated.\n'
             '## 2.0.0\n* Breaking change: new root API.\n## 1.0.0\n* Initial.\n')


def by_url(inventory):
    return {s['url']: s for s in inventory['sources']}


def test_trust_tiers_come_from_recorded_identity_not_the_model():
    inventory, _ = normalize_case(RESOLUTION, evidence(
        document('a', 'https://blog.orbit.dev/2.0', 'upgrade_guide'),
        document('b', 'https://github.com/orbit/orbit/blob/main/CHANGELOG.md'),
        document('c', 'https://raw.githubusercontent.com/oldorg/orbit/main/CHANGELOG.md'),
        document('d', 'https://github.com/orbit/orbit/pull/7', 'github_pull_request'),
        document('e', 'https://someblog.example/orbit-2', 'blog_post', bases=('text_mention',))))
    sources = by_url(inventory)
    assert sources['https://blog.orbit.dev/2.0']['source_type'] == 'official_migration_guide'
    assert sources['https://github.com/orbit/orbit/blob/main/CHANGELOG.md']['source_type'] == 'official_changelog'
    assert sources['https://raw.githubusercontent.com/oldorg/orbit/main/CHANGELOG.md']['trust']['tier'] == 'probable_official'
    assert sources['https://github.com/orbit/orbit/pull/7']['trust']['tier'] == 'official_repository_discussion'
    assert sources['https://someblog.example/orbit-2']['source_type'] == 'community_writeup'
    assert [s['trust']['tier'] for s in inventory['sources']][-1] == 'third_party'
    assert inventory['sources'][0]['rank'] == 1 and inventory['sources'][0]['trust']['tier'] == 'official'
    assert set(inventory['sources'][0]) >= {'source_id', 'title', 'url', 'source_type', 'retrieved_at', 'why_relevant'}


def test_registry_record_supplies_identity_when_resolver_has_none():
    registry = {'url': 'https://registry.npmjs.org/orbit', 'projection': 'registry_inventory',
                'text': json.dumps({'homepage': 'https://orbit.dev/', 'repository': {'url': 'git+https://github.com/orbit/orbit.git'}})}
    resolution = {'identity': None, 'selected_catalog': registry['url']}
    inventory, _ = normalize_case(resolution, evidence(document('a', 'https://orbit.dev/blog/v2'),
                                                       document('b', 'https://github.com/orbit/orbit/releases/tag/v2.0')), [registry])
    assert {s['trust']['tier'] for s in inventory['sources']} == {'official'}


def test_unassociated_and_link_only_third_party_documents_are_excluded_with_reasons():
    inventory, candidates = normalize_case(RESOLUTION, evidence(
        document('a', 'https://developer.mozilla.org/Map', 'api_documentation'),
        document('b', 'https://docs.orbit.dev/api', 'api_documentation', bases=())))
    assert not inventory['sources'] and not candidates['sections']
    reasons = {e['source_id']: e['reason'] for e in inventory['excluded_sources']}
    assert 'link-following' in reasons['a'] and 'No association' in reasons['b']


@pytest.mark.parametrize('legacy', [False, True])
def test_changelog_sections_are_scoped_to_their_version_heading(legacy):
    sections, _ = markdown_document(CHANGELOG, 'https://github.com/orbit/orbit/blob/main/CHANGELOG.md')
    if legacy:  # evidence collected before heading paths and topics were recorded
        sections = [{k: v for k, v in s.items() if k not in ('heading_path', 'topics')} for s in sections]
    doc = document('log', 'https://github.com/orbit/orbit/blob/main/CHANGELOG.md', sections=sections, text=CHANGELOG)
    inventory, candidates = normalize_case(RESOLUTION, evidence(doc))
    scoped = {c['text'].strip(): (c['versions'], c['topics'], c['version_scope']) for c in candidates['sections']}
    assert scoped == {'* `render` has been deprecated.': (['2.1'], ['deprecation'], 'version_heading'),
                      '* Breaking change: new root API.': (['2.0'], ['breaking_change'], 'version_heading')}
    assert inventory['sources'][0]['out_of_range_sections'] == 2


def test_html_subsection_keeps_its_release_heading():
    _, _, sections, _, _ = html_document('<title>Releases</title><main><h2>2.1.0</h2><h3>Core</h3><p>Now throws on legacy roots.</p>'
                                         '<h2>3.0.0</h2><h3>Core</h3><p>Removed legacy roots.</p></main>', 'https://docs.orbit.dev/releases')
    _, candidates = normalize_case(RESOLUTION, evidence(document('r', 'https://docs.orbit.dev/releases', sections=sections)))
    assert [(c['versions'], c['topics']) for c in candidates['sections']] == [(['2.1'], ['behavior_change'])]


def test_repeated_passage_points_at_the_most_trusted_copy():
    passage = 'react-dom: ReactDOM.render has been deprecated and will warn in the next release.'
    guide = document('guide', 'https://docs.orbit.dev/upgrade', 'upgrade_guide', sections=[{'heading': 'Deprecations', 'text': passage}])
    copy = document('copy', 'https://github.com/orbit/orbit/blob/main/CHANGELOG.md',
                    sections=[{'heading': 'Deprecations', 'text': '* `' + passage.replace(':', '`:')}])
    _, candidates = normalize_case(RESOLUTION, evidence(copy, guide))
    first, second = candidates['sections']
    assert first['source_id'] == 'guide' and first['duplicate_of'] is None
    assert second['duplicate_of'] == first['candidate_id'] and candidates['summary']['duplicates'] == 1


def test_llm_rationales_become_why_relevant():
    doc = document('a', 'https://docs.orbit.dev/upgrade', 'upgrade_guide', bases=('llm_cited_relevance',),
                   relevance_evidence=[{'quote': 'q', 'rationale': 'Official 2.0 migration steps.', 'versions': ['2.0']}])
    inventory, _ = normalize_case(RESOLUTION, {**evidence(doc), 'path': 'llm'})
    assert inventory['sources'][0]['why_relevant'].startswith('Official 2.0 migration steps. (1 verified quote(s).)')
    assert inventory['collection_path'] == 'llm'


def relevance_case():
    passage = 'react-dom: ReactDOM.render has been deprecated and will warn in the next release.'
    guide = document('guide', 'https://docs.orbit.dev/upgrade', 'upgrade_guide', sections=[
        {'heading': 'Deprecations', 'text': passage}, {'heading': 'Migration', 'text': 'Thanks to everyone who helped migrate.'}])
    copy = document('copy', 'https://github.com/orbit/orbit/blob/main/CHANGELOG.md', sections=[{'heading': 'Deprecations', 'text': passage}])
    return normalize_case(RESOLUTION, evidence(copy, guide))


def test_section_relevance_labels_sections_without_removing_them():
    inventory, candidates = relevance_case()
    backend = KeywordBackend()
    record = assess_sections(candidates, inventory, backend)
    sections = {(s['source_id'], s['heading']): s['relevance'] for s in candidates['sections']}
    assert sections[('guide', 'Deprecations')]['label'] == 'risk_candidate'
    assert sections[('guide', 'Deprecations')]['risk_type'] == 'changed_behavior'
    assert sections[('guide', 'Migration')]['label'] == 'not_relevant'
    assert sections[('copy', 'Deprecations')]['inherited_from'] == 'guide#s0'  # duplicate is not re-scored
    assert record['decision_calls'] == 2 and record['labels'] == {'risk_candidate': 1, 'context': 0, 'not_relevant': 1}
    assert len(candidates['sections']) == 3 and candidates['relevance_scoring']['rubric_version']
    state = backend.states[0]
    assert state['source']['trust_tier'] == 'official' and 'untrusted' in state['instruction']


def test_section_relevance_records_failures_and_respects_budget():
    inventory, candidates = relevance_case()
    record = assess_sections(candidates, inventory, KeywordBackend(fail_on='deprecated'), max_sections=1)
    assert record['failed'] == 1 and record['not_scored'] == 1 and record['scored'] == 0
    statuses = sorted(s['relevance']['status'] for s in candidates['sections'])
    assert statuses == ['failed', 'failed', 'not_scored']  # the duplicate inherits its original's failure


def test_pipeline_writes_relevance_for_either_path(tmp_path, monkeypatch):
    from upgrade_pipeline.runner import run_case
    doc = document('guide', 'https://docs.orbit.dev/upgrade', 'upgrade_guide', bases=('resolver_release_record',),
                   sections=[{'heading': 'Deprecations', 'text': 'render has been deprecated.'}])
    collected = {**evidence(doc), 'status': 'partial', 'failures': [], 'pending_urls': [], 'skipped': [], 'warnings': [],
                 'network_requests': 0, 'llm_calls': 0,
                 'version_coverage': {'2.0': {'document_ids': ['guide'], 'document_types': ['upgrade_guide']},
                                      '2.1': {'document_ids': [], 'document_types': []}}}
    resolution = {**RESOLUTION, 'status': 'partial', 'versions': [{'version': '2.0'}], 'documentation_milestones': ['2'],
                  'coverage': {}, 'warnings': [], 'network_requests': 0}
    monkeypatch.setattr('upgrade_pipeline.runner.resolve', lambda *a, **k: resolution)
    monkeypatch.setattr('upgrade_pipeline.runner.collect', lambda *a, **k: collected)
    manifest = run_case(ITEM, tmp_path, offline=True, postprocess=PostprocessConfig(relevance_backend=KeywordBackend()))
    written = json.loads((tmp_path / 'candidate_sections.json').read_text())
    assert written['sections'][0]['relevance']['label'] == 'risk_candidate'
    assert manifest['summary']['section_relevance_scored'] == 1 and manifest['configuration']['section_relevance']


def test_d1_loader_reports_missing_dependencies_instead_of_crashing(monkeypatch):
    import builtins
    from upgrade_pipeline.models import load_d1
    real_import = builtins.__import__
    def no_torch(name, *args, **kwargs):
        if name == 'torch':
            raise ImportError('missing', name='torch')
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', no_torch)
    with pytest.raises(ValueError, match='venv-d1'):
        load_d1()


def test_manifest_records_which_decision_model_was_used(tmp_path, monkeypatch):
    from upgrade_pipeline.runner import run_case
    monkeypatch.setattr('upgrade_pipeline.runner.resolve', lambda *a, **k: {'status': 'ambiguous', 'versions': [],
                        'documentation_milestones': [], 'coverage': {}, 'warnings': [], 'network_requests': 0})
    backend = KeywordBackend()
    backend.model_name = 'LiquidAI/d1-3B@abc'
    manifest = run_case(ITEM, tmp_path, offline=True, postprocess=PostprocessConfig(relevance_backend=backend))
    assert manifest['configuration']['decision_backend'] == {'adapter': 'KeywordBackend', 'model': 'LiquidAI/d1-3B@abc'}


def test_heading_only_sections_are_dropped_and_do_not_break_legacy_scoping():
    changelog = '## 2.1.0\n### Fixes\n* Fixed a crash.\n## 2.0.0\n### Breaking Changes\n\n### Core\n* Removed `legacyRoot`.\n'
    sections, _ = markdown_document(changelog, URL_LOG)
    legacy = [{k: v for k, v in s.items() if k not in ('heading_path', 'topics')} for s in sections]
    _, candidates = normalize_case(RESOLUTION, evidence(document('log', URL_LOG, sections=legacy, text=changelog)))
    assert 'Breaking Changes' not in [c['heading'] for c in candidates['sections']]
    assert {c['heading']: c['versions'] for c in candidates['sections']}['Core'] == ['2.0']


URL_LOG = 'https://github.com/orbit/orbit/blob/main/CHANGELOG.md'


def test_only_milestone_and_latest_registry_records_feed_extraction():
    record = lambda v: {**document(f'reg{v}', f'https://pypi.org/pypi/orbit/{v}/json', 'registry_metadata', bases=()),
                        'version_associations': [{'version': v, 'basis': 'registry_version'}],
                        'sections': [{'heading': 'requires', 'text': 'Requires asgiref>=3.6 dependency.'}]}
    collected = {**evidence(record('2.0'), record('2.0.1'), record('2.1')), 'version_coverage': {'2.0': {}, '2.0.1': {}, '2.1': {}}}
    _, candidates = normalize_case({**RESOLUTION, 'documentation_milestones': ['2']}, collected)
    assert sorted(c['source_id'] for c in candidates['sections']) == ['reg2.0', 'reg2.1']  # 2.0 milestone, 2.1 latest
    assert candidates['summary']['patch_registry_records_skipped'] == 1
