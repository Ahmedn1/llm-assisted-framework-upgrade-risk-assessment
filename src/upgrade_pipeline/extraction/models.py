"""Schemas for risk extraction.

Model-facing drafts are deliberately narrower than the final output: the model cites fact or section ids,
and evidence text, source types and URLs are filled in by code from verified records, so they cannot be invented.
"""
from typing import Literal

from pydantic import Field, create_model

from ..schema import (CATEGORIES, CERTAINTY, CHANGES, CONFIDENCE, GROUNDING, OPERATORS, RISK_LEVELS, RISK_TYPES, ROLES,
                      SUBJECT_KINDS, SURFACES, TRUST_TIERS, VERDICTS, Strict)


# Stage A: atomic change facts from one batch of sections.
class FactDraft(Strict):
    candidate_id: str
    subject: str = Field(min_length=1, description='Exact name of the API, setting, package, runtime or feature as written in the text')
    subject_kind: Literal[SUBJECT_KINDS]
    change: Literal[CHANGES]
    is_risk: bool = Field(description='False for contextual facts that cannot break an application or require work')
    known_incompatibility: bool = Field(description='True when a platform, environment, tool or library is not (yet) supported '
                                                    'or incompatible, or staying on the old version is recommended for it')
    old_value: str | None
    new_value: str | None = Field(description='Replacement, new default, or new minimum version, when stated')
    versions: list[str]
    quote: str = Field(min_length=10, description='Short contiguous verbatim passage from the cited section')
    explanation: str = Field(min_length=1)
    certainty: Literal[CERTAINTY]


class FactBatch(Strict):
    facts: list[FactDraft]


def fact_batch_schema(candidate_ids, versions):
    """Constrain citations to this batch's section ids and this upgrade's resolved versions."""
    fact = create_model('ScopedFactDraft', __base__=FactDraft,
                        candidate_id=(Literal[tuple(candidate_ids)], ...),
                        versions=(list[Literal[tuple(versions)]], ...))
    return create_model('ScopedFactBatch', __base__=FactBatch, facts=(list[fact], ...))


# Stage B: merge facts that describe the same change.
class FactGroup(Strict):
    label: str = Field(min_length=1)
    cluster_ids: list[str] = Field(min_length=1)


class Grouping(Strict):
    groups: list[FactGroup]


def grouping_schema(cluster_ids):
    group = create_model('ScopedFactGroup', __base__=FactGroup, cluster_ids=(list[Literal[tuple(cluster_ids)]], Field(min_length=1)))
    return create_model('ScopedGrouping', __base__=Grouping, groups=(list[group], ...))


# Stage C: risk paths for one group of facts.
class ConditionDraft(Strict):
    statement: str = Field(min_length=1)
    category: Literal[CATEGORIES]
    condition_role: Literal[ROLES]
    operator: Literal[OPERATORS]
    expected_value: str
    fact_ids: list[str] = Field(min_length=1)
    reasoning: str = Field(min_length=1)
    ambiguities: list[str]
    search_patterns: list[str] = Field(description='Literal strings or regexes to find this condition in a codebase or deployment; empty if not applicable')


class MigrationStepDraft(Strict):
    action: str = Field(min_length=1, description='A change the documentation instructs, e.g. replace render with createRoot')
    fact_ids: list[str] = Field(min_length=1)


class PathDraft(Strict):
    path_name: str = Field(min_length=1)
    risk_type: Literal[RISK_TYPES]
    risk_level: Literal[RISK_LEVELS]
    affected_surface: Literal[SURFACES]
    description: str = Field(min_length=1)
    conditions: list[ConditionDraft] = Field(min_length=1)
    migration_steps: list[MigrationStepDraft] = Field(description='Documented migration steps only, in order; empty if none')
    suggested_actions: list[str] = Field(description='Validation and search actions, e.g. search for usages, add a test')


class PathBatch(Strict):
    paths: list[PathDraft]
    not_a_risk_reason: str | None = Field(description='Set when the facts are context only and no path is produced')


def path_batch_schema(fact_ids):
    condition = create_model('ScopedConditionDraft', __base__=ConditionDraft,
                             fact_ids=(list[Literal[tuple(fact_ids)]], Field(min_length=1)))
    step = create_model('ScopedMigrationStepDraft', __base__=MigrationStepDraft,
                        fact_ids=(list[Literal[tuple(fact_ids)]], Field(min_length=1)))
    path = create_model('ScopedPathDraft', __base__=PathDraft, conditions=(list[condition], Field(min_length=1)),
                        migration_steps=(list[step], ...))
    return create_model('ScopedPathBatch', __base__=PathBatch, paths=(list[path], ...))


# Final output: the output contract's risk-path shape, validated before risk_model.json is written.


class EvidenceOut(Strict):
    source_id: str
    source_type: str | None
    quote_or_summary: str = Field(min_length=1)
    relevance: str
    fact_id: str
    candidate_id: str
    url: str
    trust_tier: Literal[TRUST_TIERS] | None
    versions: list[str]
    certainty: Literal[CERTAINTY]




class EntailmentOut(Strict):
    verdict: Literal[VERDICTS + ('not_checked',)]
    adds_unsupported_detail: float | None = Field(description='Probability the statement adds specifics absent from the evidence')
    judge: str | None
    note: str | None


class ConditionVerificationOut(Strict):
    grounding: Literal[GROUNDING]
    failed_evidence: list[str] = Field(description='fact_ids whose quote could not be re-verified in the source document')
    entailment: EntailmentOut
    corroborating_sources: list[str]
    independent_source_count: int
    best_trust_tier: Literal[TRUST_TIERS] | None
    conflicts: list[str]
    preliminary_confidence: Literal[CONFIDENCE]
    preliminary_role: Literal[ROLES]
    confidence_reasons: list[str]


class ConditionOut(Strict):
    condition_id: str
    statement: str = Field(min_length=1)
    category: Literal[CATEGORIES]
    required: bool
    condition_role: Literal[ROLES]
    operator: Literal[OPERATORS]
    expected_value: str
    evidence: list[EvidenceOut] = Field(min_length=1, description='Every condition is grounded in at least one verified quote')
    reasoning: str
    confidence: Literal[CONFIDENCE]
    ambiguities: list[str]
    search_patterns: list[str]
    verification: ConditionVerificationOut | None = None


class MigrationStepOut(Strict):
    step: str = Field(min_length=1)
    evidence: list[EvidenceOut] = Field(min_length=1)


class RiskPathOut(Strict):
    path_id: str
    path_name: str
    risk_type: Literal[RISK_TYPES]
    risk_level: Literal[RISK_LEVELS]
    affected_surface: Literal[SURFACES]
    description: str
    conditions: list[ConditionOut] = Field(min_length=1)
    migration_steps: list[MigrationStepOut]
    suggested_actions: list[str]
    change_kinds: list[Literal[CHANGES]] = Field(description='Changes of every fact this path cites, computed by code '
                                                              '(e.g. removed, deprecated, migration_step); includes known_issue '
                                                              'when any cited fact is a known incompatibility')
    structural_warnings: list[str]
    confidence: Literal[CONFIDENCE] | None = None
    verification_flags: list[str] = Field(default_factory=list)


class ContextualFactOut(Strict):
    fact_id: str
    statement: str
    subject: str
    change: Literal[CHANGES]
    known_incompatibility: bool
    versions: list[str]
    why_not_a_risk: str
    evidence: list[EvidenceOut] = Field(min_length=1)


class RiskRef(Strict):
    path_id: str
    path_name: str
    risk_type: Literal[RISK_TYPES]
    risk_level: Literal[RISK_LEVELS]
    confidence: Literal[CONFIDENCE]
    sources: list[str]
    reasons: list[str]


class ClaimOut(Strict):
    fact_id: str
    source_id: str
    url: str
    trust_tier: Literal[TRUST_TIERS] | None
    change: Literal[CHANGES]
    versions: list[str]
    value: str | None
    quote: str


class DisagreementOut(Strict):
    conflict_id: str
    subject: str
    kind: Literal['severity', 'versions', 'value', 'risk_vs_context']
    claims: list[ClaimOut] = Field(min_length=2)
    resolution: Literal['resolved', 'unresolved']
    winner_fact_id: str | None
    reason: str
    affected_paths: list[str]


class EvidenceSummaryBody(Strict):
    high_confidence_risks: list[RiskRef]
    medium_confidence_risks: list[RiskRef]
    low_confidence_or_disputed_risks: list[RiskRef]
    sources_with_disagreements: list[DisagreementOut]
    open_questions: list[str]


class EvidenceSummaryOut(Strict):
    project: str
    upgrade: str
    evidence_summary: EvidenceSummaryBody


class RiskModelOut(Strict):
    project: str
    current_version: str
    target_version: str
    schema_version: str
    generated_at: str
    summary: str
    source_inventory: list[dict]
    risk_paths: list[RiskPathOut]
    contextual_facts: list[ContextualFactOut]
    open_questions: list[str] = Field(description='Unknowns about the upgrade itself')
    diagnostics: list[str] = Field(default_factory=list, description='How the run went: budgets, rejected extractions, failed calls')
    overall_confidence: Literal[CONFIDENCE]
    confidence_rubric: dict[Literal[CONFIDENCE], str]
    extraction: dict
    evidence_summary: EvidenceSummaryOut | None = None
    verification: dict | None = None


def risk_model_json_schema():
    schema = RiskModelOut.model_json_schema()
    schema['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
    schema['title'] = 'Upgrade risk model'
    return schema
