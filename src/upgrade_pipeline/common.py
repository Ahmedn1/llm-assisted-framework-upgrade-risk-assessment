"""Helpers shared by pipeline stages: JSON output, text matching, version tokens and progress logging."""
import json
import logging
import re
import time

HEADING_MARK = re.compile(r'^\s*#{1,6}\s+')
VERSION_TOKEN = re.compile(r'(?<![\w.])v?(\d+(?:\.\d+)+)(?![\w]|\.\d)')


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + '\n')


def normalize(text):
    """Collapse whitespace; citation matching is otherwise exact."""
    return ' '.join(text.split())


def loose(text):
    """Quote matching that also ignores Markdown emphasis and code markers, nothing else."""
    return ' '.join(re.sub(r'[`*_>#]', ' ', text).split())


def names_version(text, version):
    """True if text names the release, allowing the conventional short form (18.0.0 as 18.0 or 18)."""
    parts = version.split('.')
    forms = ['.'.join(parts)]
    while len(parts) > 1 and parts[-1] == '0':
        parts = parts[:-1]
        forms.append('.'.join(parts))
    return any(re.search(r'(?<![\w.])v?' + re.escape(form) + r'(?:\.0)*(?![\w]|\.\d)', text) for form in forms)


def slug(text, limit=60):
    return re.sub(r'[^a-z0-9]+', '_', text.lower()).strip('_')[:limit].strip('_') or 'item'


def get_logger(step):
    """Step loggers share the 'upgrade' namespace; only the CLI attaches a handler."""
    return logging.getLogger('upgrade.' + step)


class Progress:
    """Log a counted step at its first item, about every tenth of the total, and its last item."""
    def __init__(self, log, label, total, every=None):
        self.log, self.label, self.total, self.done = log, label, total, 0
        self.every = every or max(1, total // 10)
        self.started = time.monotonic()

    def advance(self, detail=''):
        self.done += 1
        if self.done in (1, self.total) or self.done % self.every == 0:
            self.log.info('%s %d/%d%s (%.0fs)', self.label, self.done, self.total,
                          f' - {detail}' if detail else '', time.monotonic() - self.started)


class StepFormatter(logging.Formatter):
    def format(self, record):
        record.step = record.name.removeprefix('upgrade.')
        return super().format(record)


def configure_logging(level='info'):
    """Progress to stderr as 'HH:MM:SS step | message'; stdout stays machine-readable."""
    handler = logging.StreamHandler()
    handler.setFormatter(StepFormatter('%(asctime)s %(step)s | %(message)s', datefmt='%H:%M:%S'))
    root = logging.getLogger('upgrade')
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    root.propagate = False
