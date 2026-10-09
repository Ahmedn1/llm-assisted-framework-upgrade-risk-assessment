"""Model settings per pipeline stage, from CLI arguments and environment variables.

Each model kind has a generic group and one group per stage; stage values override generic ones:

    LLM       LLM_MODEL_NAME, LLM_BASE_URL, LLM_API_KEY
              LLM_<STAGE>_MODEL_NAME, ..._BASE_URL, ..._API_KEY          stages: COLLECTION, EXTRACTION, JUDGE
    Decision  DECISION_BACKEND (d1|jev), DECISION_MODEL_NAME, DECISION_MODEL_REVISION, DECISION_BASE_URL, DECISION_API_KEY
              DECISION_<STAGE>_BACKEND, ..._MODEL_NAME, ..._MODEL_REVISION, ..._BASE_URL, ..._API_KEY
                                                                          stages: SCORER, RELEVANCE, JUDGE

Precedence: stage argument > stage env > generic argument > generic env > default. API keys are read from
the environment only. A stage that sets its own base URL, or a decision backend different from the generic
one, is a different provider: its model name and key come from that stage group only, so a key or model is
never sent to a provider it was not configured for. The decision
backend defaults to the local d1 model; a configured base URL without an explicit backend selects jev.
"""
import os
import time
from dataclasses import dataclass, field

from ..common import get_logger

log = get_logger('models')

LLM_STAGES = {
    'collection': 'LLM-path resolution and evidence collection (--path llm)',
    'extraction': 'risk extraction',
    'judge': 'verification critic (--verify-judge llm)',
}
DECISION_STAGES = {
    'scorer': 'resolver version-source scoring (--scorer decision)',
    'relevance': 'section relevance scoring of collected evidence',
    'judge': 'verification entailment judge (--verify-judge decision)',
}
DECISION_KINDS = ('d1', 'jev')
D1_MODEL = 'LiquidAI/d1-3B'
D1_REVISION = '051bcc464b01b9f92942b364d9586b0ef5912432'  # reviewed commit; remote code runs from this snapshot only


def env(name):
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


def pick(*candidates):
    """First (value, source) whose value is set."""
    return next(((value, source) for value, source in candidates if value), (None, None))


def arg(args, name):
    return getattr(args, name, None) if args is not None else None


@dataclass(frozen=True)
class LLMSettings:
    stage: str
    model: str
    base_url: str
    api_key: str | None = field(repr=False)
    sources: dict = field(compare=False, hash=False)

    def config(self):
        from inference import ModelConfig
        return ModelConfig(model=self.model, base_url=self.base_url, api_key=self.api_key)

    def describe(self):
        return {'kind': 'llm', 'model': self.model, 'base_url': self.base_url, 'api_key_set': bool(self.api_key),
                'resolved_from': self.sources}


@dataclass(frozen=True)
class DecisionSettings:
    stage: str
    kind: str
    model: str
    revision: str | None
    base_url: str | None
    api_key: str | None = field(repr=False)
    sources: dict = field(compare=False, hash=False)

    def describe(self):
        return {'kind': self.kind, 'model': self.model, 'revision': self.revision, 'base_url': self.base_url,
                'api_key_set': bool(self.api_key), 'resolved_from': self.sources}


def resolve_llm(stage, args=None):
    if stage not in LLM_STAGES:
        raise ValueError(f'Unknown LLM stage {stage!r}')
    S, flag = stage.upper(), f'--{stage}-llm'
    stage_base = pick((arg(args, f'{stage}_llm_base_url'), f'{flag}-base-url'), (env(f'LLM_{S}_BASE_URL'), f'LLM_{S}_BASE_URL'))
    stage_model = pick((arg(args, f'{stage}_llm_model'), f'{flag}-model'), (env(f'LLM_{S}_MODEL_NAME'), f'LLM_{S}_MODEL_NAME'))
    stage_key = pick((env(f'LLM_{S}_API_KEY'), f'LLM_{S}_API_KEY'))
    if stage_base[0]:
        if not stage_model[0]:
            raise ValueError(f'{stage_base[1]} sets a separate endpoint for {LLM_STAGES[stage]}; also set LLM_{S}_MODEL_NAME '
                             f'or {flag}-model (the generic model and API key are not reused for a different endpoint)')
        base, model, key = stage_base, stage_model, stage_key
    else:
        base = pick((arg(args, 'llm_base_url'), '--llm-base-url'), (env('LLM_BASE_URL'), 'LLM_BASE_URL'))
        model = stage_model if stage_model[0] else pick(
            (arg(args, 'llm_model'), '--llm-model'), (env('LLM_MODEL_NAME'), 'LLM_MODEL_NAME'), (env('LLM_MODEL'), 'LLM_MODEL (deprecated)'))
        key = stage_key if stage_key[0] else pick((env('LLM_API_KEY'), 'LLM_API_KEY'))
    if not base[0] or not model[0]:
        raise ValueError(f'No LLM configured for {LLM_STAGES[stage]}: set LLM_MODEL_NAME and LLM_BASE_URL '
                         f'(or LLM_{S}_MODEL_NAME and LLM_{S}_BASE_URL), or pass --llm-model and --llm-base-url')
    return LLMSettings(stage, model[0], base[0], key[0], {'model': model[1], 'base_url': base[1], 'api_key': key[1]})


def resolve_decision(stage, args=None):
    if stage not in DECISION_STAGES:
        raise ValueError(f'Unknown decision stage {stage!r}')
    S, flag = stage.upper(), f'--{stage}-decision'
    stage_value = lambda name, var: pick((arg(args, f'{stage}_decision_{name}'), f'{flag}-{name.replace("_", "-")}'),
                                         (env(f'DECISION_{S}_{var}'), f'DECISION_{S}_{var}'))
    generic_value = lambda name, var, *legacy: pick((arg(args, f'decision_{name}'), f'--decision-{name.replace("_", "-")}'),
                                                    (env(f'DECISION_{var}'), f'DECISION_{var}'), *legacy)
    stage_base = stage_value('base_url', 'BASE_URL')
    stage_key = pick((env(f'DECISION_{S}_API_KEY'), f'DECISION_{S}_API_KEY'))
    stage_kind = stage_value('backend', 'BACKEND')
    generic_kind = generic_value('backend', 'BACKEND')[0] or ('jev' if generic_value('base_url', 'BASE_URL')[0] else 'd1')
    if stage_base[0] or (stage_kind[0] and stage_kind[0] != generic_kind):
        # A separate endpoint or a different backend is a different provider: borrow nothing provider-specific.
        kind, model, revision, base, key = (stage_value('backend', 'BACKEND'), stage_value('model', 'MODEL_NAME'),
                                            stage_value('revision', 'MODEL_REVISION'), stage_base, stage_key)
    else:
        first = lambda own, generic: own if own[0] else generic
        kind = first(stage_value('backend', 'BACKEND'), generic_value('backend', 'BACKEND'))
        model = first(stage_value('model', 'MODEL_NAME'),
                      generic_value('model', 'MODEL_NAME', (env('DECISION_MODEL'), 'DECISION_MODEL (deprecated)')))
        revision = first(stage_value('revision', 'MODEL_REVISION'), generic_value('revision', 'MODEL_REVISION'))
        base = generic_value('base_url', 'BASE_URL')
        key = first(stage_key, pick((env('DECISION_API_KEY'), 'DECISION_API_KEY')))
    if kind[0] is None:
        kind = ('jev', f'inferred from {base[1]}') if base[0] else ('d1', 'default')
    if kind[0] not in DECISION_KINDS:
        raise ValueError(f'{kind[1]}={kind[0]!r}: decision backend must be one of {DECISION_KINDS}')
    sources = {'backend': kind[1], 'model': model[1], 'revision': revision[1], 'base_url': base[1], 'api_key': key[1]}
    if kind[0] == 'd1':
        name = model[0] or D1_MODEL
        pinned = revision[0] or (D1_REVISION if name == D1_MODEL else None)
        if not pinned:
            raise ValueError(f'{name} runs remote model code; pin a reviewed commit with DECISION_{S}_MODEL_REVISION, '
                             f'DECISION_MODEL_REVISION or {flag}-revision')
        sources.update(model=model[1] or 'default', revision=revision[1] or 'default pin', base_url=None, api_key=None)
        return DecisionSettings(stage, 'd1', name, pinned, None, None, sources)
    if not model[0] or not base[0]:
        raise ValueError(f'The jev decision backend for {DECISION_STAGES[stage]} needs a model and base URL: set '
                         f'DECISION_MODEL_NAME and DECISION_BASE_URL (or DECISION_{S}_*), or pass --decision-model and --decision-base-url')
    return DecisionSettings(stage, 'jev', model[0], None, base[0], key[0], sources)


class ModelRegistry:
    """Opens each distinct model once per run; stages resolving to identical settings share the instance."""
    def __init__(self, stack, *, offline=False):
        self.stack, self.offline, self.opened = stack, offline, {}

    def llm(self, settings):
        if self.offline:
            raise ValueError(f'{LLM_STAGES[settings.stage]} needs network access to {settings.base_url}')
        key = ('llm', settings.model, settings.base_url, settings.api_key)
        if key not in self.opened:
            from inference import LLMClient
            self.opened[key] = self.stack.enter_context(LLMClient(settings.config()))
            log.info('%s: LLM %s at %s', settings.stage, settings.model, settings.base_url)
        return self.opened[key]

    def decision(self, settings):
        key = (settings.kind, settings.model, settings.revision, settings.base_url, settings.api_key)
        if key not in self.opened:
            if settings.kind == 'jev':
                if self.offline:
                    raise ValueError(f'jev is a remote decision API ({settings.base_url}); use the local d1 backend offline')
                from inference import JevClient, ModelConfig
                self.opened[key] = self.stack.enter_context(JevClient(ModelConfig(
                    model=settings.model, base_url=settings.base_url, api_key=settings.api_key)))
                log.info('%s: decision API %s at %s', settings.stage, settings.model, settings.base_url)
            else:
                from . import local
                started = time.monotonic()
                log.info('%s: loading %s@%s on CUDA%s', settings.stage, settings.model, settings.revision[:12],
                         ' from the cache' if self.offline else '')
                self.opened[key] = local.load_d1(settings.model, settings.revision, offline=self.offline)
                log.info('%s: loaded in %.0fs', settings.stage, time.monotonic() - started)
        return self.opened[key]


def add_model_arguments(parser, llm_stages=(), decision_stages=()):
    """Generic and per-stage model arguments; API keys stay in environment variables."""
    if llm_stages:
        group = parser.add_argument_group('LLM (generic; overrides LLM_MODEL_NAME / LLM_BASE_URL, key from LLM_API_KEY)')
        group.add_argument('--llm-model')
        group.add_argument('--llm-base-url')
        for stage in llm_stages:
            group = parser.add_argument_group(f'LLM for {LLM_STAGES[stage]} (overrides generic; env LLM_{stage.upper()}_*)')
            group.add_argument(f'--{stage}-llm-model')
            group.add_argument(f'--{stage}-llm-base-url')
    if decision_stages:
        group = parser.add_argument_group('Decision model (generic; default local d1; env DECISION_*)')
        group.add_argument('--decision-backend', choices=DECISION_KINDS)
        group.add_argument('--decision-model')
        group.add_argument('--decision-revision', help='Model repository commit for d1-style local models')
        group.add_argument('--decision-base-url')
        for stage in decision_stages:
            group = parser.add_argument_group(f'Decision model for {DECISION_STAGES[stage]} (overrides generic; env DECISION_{stage.upper()}_*)')
            group.add_argument(f'--{stage}-decision-backend', choices=DECISION_KINDS)
            group.add_argument(f'--{stage}-decision-model')
            group.add_argument(f'--{stage}-decision-revision')
            group.add_argument(f'--{stage}-decision-base-url')
