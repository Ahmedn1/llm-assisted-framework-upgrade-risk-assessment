"""Adapter for an explicitly loaded Laya Agent; no model loading on import."""
from copy import deepcopy
from .base import DecisionBackend
from ..transport import ModelOutputError


class LayaClient(DecisionBackend):
    def __init__(self, agent, *, model_name: str):
        if not callable(getattr(agent, 'predict', None)) or not model_name.strip():
            raise ValueError('Supply a loaded Laya Agent and model_name')
        self.agent, self.model_name = agent, model_name

    def decide(self, state, questions):
        typed, serialized = self.prepare_questions(questions)
        # Laya's legend is rendered text: preserve a single unambiguous contract.
        for question in serialized.values():
            criteria = question.get('criteria')
            if question['type'] == 'score' and not all(isinstance(c, str) for c in criteria):
                raise ValueError('Laya score criteria must be strings')
        # The native runtime silently clips question text as well as state. Check
        # its exact tokenization before inference when wrapping a real Agent.
        if hasattr(self.agent, 'tok'):
            from laya.common import _encode_question_text, render_options, build_sequence
            for name, qdef in serialized.items():
                internal = self.agent._to_internal(qdef)
                tok = self.agent.tok
                options = [len(_encode_question_text(tok, ' ' + text.replace(tok.mask_token, ' '), add_special_tokens=False))
                           for text in render_options(internal)]
                instruction = len(_encode_question_text(tok, internal['t'] + ' question: ' + str(internal['ins']).replace(tok.mask_token, ' '), add_special_tokens=False))
                budget = self.agent.cfg.get('head_max_len', 192)
                if any(n > 48 for n in options) or max(instruction, 16) + sum(n + 1 for n in options) > budget:
                    raise ModelOutputError('Laya would truncate the question rubric; shorten it explicitly')
                _, _, _, stats = build_sequence(tok, state, internal, self.agent.cfg.get('max_len', 512), budget,
                                                 return_stats=True, return_truncation_stats=True)
                if stats['truncated']:
                    raise ModelOutputError('Laya input exceeds the checkpoint context; use an explicit compact projection')
        raw = self.agent.predict(deepcopy(state), serialized)
        if not isinstance(raw, dict) or not isinstance(raw.get('usage'), dict):
            raise ModelOutputError('Invalid Laya response envelope')
        usage = raw['usage']
        if usage.get('truncated') or usage.get('state_tokens_dropped', 0) or usage.get('options'):
            raise ModelOutputError('Laya truncated evidence or collapsed rubric options; refusing the decision')
        answers = {}
        extensions = {}
        for name, answer in raw.get('answers', {}).items():
            if not isinstance(answer, dict):
                raise ModelOutputError('Invalid Laya answer')
            keep = {'type', 'noul'} if answer.get('type') == 'noul' else {'type', 'choice', 'score', 'legend', 'probabilities', 'confidence'}
            answers[name] = {k: v for k, v in answer.items() if k in keep}
            extensions[name] = {k: v for k, v in answer.items() if k not in keep}
        result = self.validate_decision({'model': self.model_name, 'answers': answers,
                                    'usage': {k: usage[k] for k in ('input_tokens', 'output_tokens') if k in usage}},
                                   typed, backend='laya')
        result.provider_metadata = {'usage': usage, 'answer_extensions': extensions,
                                    'confidence_semantics': 'Laya native normalized-entropy confidence; not comparable to d1 confidence.'}
        return result
