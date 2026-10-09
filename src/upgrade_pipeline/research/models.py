"""Structured agent actions; no provider-specific tool-calling dependency."""
from typing import Literal
from pydantic import Field

from ..schema import Strict as StrictModel


class VersionClaim(StrictModel):
    version: str
    source_id: str
    quote: str = Field(min_length=1)


class DocumentClaim(StrictModel):
    source_id: str
    versions: list[str] = Field(min_length=1)
    document_type: Literal['release_notes', 'upgrade_guide', 'api_documentation',
                           'github_release', 'github_issue', 'github_pull_request',
                           'deprecation_notice', 'registry_metadata', 'blog_post', 'supporting_document']
    quote: str = Field(min_length=20)
    relevance: str = Field(min_length=1)


class Action(StrictModel):
    action: Literal['search', 'fetch', 'finish']
    query: str = ''
    url: str = ''
    offset: int = Field(default=0, ge=0)
    link_offset: int = Field(default=0, ge=0)
    rationale: str = ''
    gaps: list[str] = Field(default_factory=list)


class ResolutionAction(Action):
    selected_source_id: str = ''
    versions: list[VersionClaim] = Field(default_factory=list)


class CollectionAction(Action):
    documents: list[DocumentClaim] = Field(default_factory=list)


class ResolutionRetrievalAction(ResolutionAction):
    """Remove unavailable search from resolution's structured actions."""
    action: Literal['fetch', 'finish']


class CollectionRetrievalAction(CollectionAction):
    """Remove unavailable search from collection's structured actions."""
    action: Literal['fetch', 'finish']


def allowed(values, *, empty=False):
    """Literal of the values observed so far; plain str when none exist yet (an empty enum is invalid JSON Schema
    and the citation validators reject anything unobserved anyway). `empty` admits '' for unused action fields."""
    values = tuple(dict.fromkeys(values))
    if not values:
        return str
    return Literal[(('',) if empty else ()) + values]


def resolution_schema(search_disabled, catalog_source_ids, version_labels):
    """Constrain catalog selection and version citations to fetched release inventories and their in-range labels."""
    from pydantic import create_model
    base = ResolutionRetrievalAction if search_disabled else ResolutionAction
    if not catalog_source_ids:
        return base
    claim = create_model('ScopedVersionClaim', __base__=VersionClaim,
                         version=(allowed(version_labels), ...), source_id=(allowed(catalog_source_ids), ...))
    return create_model('ScopedResolutionAction', __base__=base,
                        selected_source_id=(allowed(catalog_source_ids, empty=True), ''),
                        versions=(list[claim], Field(default_factory=list)))


def collection_schema(versions, search_disabled, source_ids=()):
    """Constrain model output to this run's exact resolved release labels and fetched source ids."""
    from pydantic import create_model
    version_label = Literal[tuple(versions)]
    scoped_claim = create_model('ScopedDocumentClaim', __base__=DocumentClaim,
                                source_id=(allowed(source_ids), ...),
                                versions=(list[version_label], Field(min_length=1)))
    return create_model('ScopedCollectionAction',
        __base__=CollectionRetrievalAction if search_disabled else CollectionAction,
        documents=(list[scoped_claim], Field(default_factory=list)))
