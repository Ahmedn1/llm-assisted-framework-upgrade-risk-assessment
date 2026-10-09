"""Value lists and the strict model base shared by every structured stage output.

The first group is the output contract's own vocabulary; the rest are this pipeline's extensions.
"""
from pydantic import BaseModel, ConfigDict

RISK_TYPES = ('removed_api', 'changed_behavior', 'changed_default', 'dependency_requirement', 'runtime_requirement',
              'configuration_change', 'data_migration', 'operational_risk', 'unknown')
RISK_LEVELS = ('critical', 'high', 'medium', 'low', 'unknown')  # most to least severe; also the sort order
SURFACES = ('application_code', 'configuration', 'dependencies', 'runtime', 'database', 'deployment', 'tests', 'unknown')
CATEGORIES = ('version', 'api_usage', 'configuration', 'dependency', 'runtime', 'deployment', 'database', 'behavior',
              'test_coverage', 'unknown')
OPERATORS = ('equals', 'not_equals', 'in_range', 'less_than', 'greater_than', 'contains', 'absent', 'present', 'unknown')
CONFIDENCE = ('high', 'medium', 'low')

# The output contract asks whether a condition is required, optional, alternative, disqualifying or uncertain;
# the boolean `required` in its example schema cannot express that, so both are emitted.
ROLES = ('required', 'optional', 'alternative', 'disqualifying', 'uncertain')
CHANGES = ('removed', 'deprecated', 'renamed', 'default_changed', 'behavior_changed', 'requirement_raised',
           'requirement_added', 'replacement_recommended', 'migration_step', 'known_issue', 'security_fix', 'context')
SUBJECT_KINDS = ('api', 'configuration', 'dependency', 'runtime', 'behavior', 'data', 'deployment', 'tooling', 'other')
CERTAINTY = ('explicit', 'implied', 'speculative')
VERDICTS = ('supports', 'partially_supports', 'contradicts', 'unrelated')
GROUNDING = ('grounded', 'partially_grounded', 'ungrounded')

# Most to least trusted; a third-party source never overrides an official one.
TRUST_TIERS = ('official', 'probable_official', 'official_repository_discussion', 'third_party')
TIER_RANK = {tier: rank for rank, tier in enumerate(TRUST_TIERS)}
RELEASE_TYPES = ('release_notes', 'github_release')
CHANGE_TYPES = RELEASE_TYPES + ('upgrade_guide', 'deprecation_notice')


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')
