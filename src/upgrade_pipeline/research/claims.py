"""Keep independently validated evidence; one bad claim cannot erase good ones."""
from ..common import names_version
from ..schema import RELEASE_TYPES


class EvidenceAccumulator:
    def __init__(self, retriever, versions, max_documents):
        self.retriever, self.versions, self.max_documents = retriever, set(versions), max_documents
        self.documents = {}

    def ingest(self, claims):
        errors = []
        for index, claim in enumerate(claims):
            try:
                if not set(claim.versions) <= self.versions:
                    raise ValueError(f'Document associations must use resolved version labels exactly: {sorted(self.versions)}')
                source, corrected_from = self.locate(claim)
                if claim.document_type in RELEASE_TYPES:
                    # A release-note role is per version; a generic line must not cover every release in range.
                    context = claim.quote + '\n' + self.retriever.version_context(source, claim.quote)
                    unnamed = [v for v in claim.versions if not names_version(context, v)]
                    if unnamed:
                        raise ValueError(f'Release-note claim versions {unnamed} are not named in the quote, its version heading '
                                         'or the page title. Quote the passage under that version heading, or use another document_type.')
                created = source['id'] not in self.documents
                if created and len(self.documents) >= self.max_documents:
                    raise ValueError('Document budget exceeded')
                doc = self.documents.setdefault(source['id'], {
                    'id': source['id'], 'url': source['url'], 'retrieval_url': source['retrieval_url'],
                    'aliases': [], 'document_type': claim.document_type, 'document_types': [],
                    'title': source['title'], 'text': source['text'], 'sections': source['sections'],
                    'content_sha256': source['content_sha256'], 'response_sha256': source['response_sha256'],
                    'retrieved_at': source['retrieved_at'], 'publisher_relationship': 'llm_selected_unverified',
                    'authority_inherited': False, 'applicability': 'unreviewed', 'version_associations': [],
                    'discovered_from': [], 'metadata': {'projection': source['projection']}, 'relevance_evidence': []})
                if created:
                    # Same text at another URL (mirror, redirect target, raw vs. rendered): keep provenance, store text once.
                    original = next((d for d in self.documents.values() if d is not doc and 'duplicate_content_of' not in d
                                     and d['content_sha256'] == doc['content_sha256']), None)
                    if original:
                        doc.update(duplicate_content_of=original['id'], text='', sections=[])
                if claim.document_type not in doc['document_types']:
                    doc['document_types'].append(claim.document_type)
                evidence = {'quote': claim.quote, 'rationale': claim.relevance, 'versions': claim.versions,
                            'document_type': claim.document_type, 'citation_matching': 'contiguous_text_whitespace_normalized'}
                if corrected_from:
                    evidence['cited_source_corrected_from'] = corrected_from
                if evidence not in doc['relevance_evidence']:
                    doc['relevance_evidence'].append(evidence)
                for version in claim.versions:
                    association = {'version': version, 'basis': 'llm_cited_relevance', 'status': 'unreviewed'}
                    if association not in doc['version_associations']:
                        doc['version_associations'].append(association)
            except ValueError as exc:
                errors.append({'claim_index': index, 'source_id': claim.source_id, 'error': str(exc)})
        return errors

    def release_note_versions(self):
        return {v for doc in self.documents.values() for e in doc['relevance_evidence']
                if e['document_type'] in RELEASE_TYPES for v in e['versions']}

    def locate(self, claim):
        # Overlapping official pages (release post vs. upgrade guide) invite misattributed source ids;
        # a quote verified verbatim in another observed source is still grounded, so record the correction.
        try:
            return self.retriever.cite_body(claim.source_id, claim.quote), None
        except ValueError as original:
            for source_id in self.retriever.sources:
                if source_id != claim.source_id:
                    try:
                        return self.retriever.cite_body(source_id, claim.quote), claim.source_id
                    except ValueError:
                        pass
            raise original
