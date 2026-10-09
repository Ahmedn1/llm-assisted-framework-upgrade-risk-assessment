"""Local decision-model loading and provenance. Settings resolution lives in models.settings."""
import os
from pathlib import Path

from .settings import D1_MODEL, D1_REVISION

ROOT = Path(__file__).resolve().parents[2]


def load_d1(model=D1_MODEL, revision=D1_REVISION, *, offline=False):
    """Load a d1-style decision model (exposing system_one) from a pinned snapshot on CUDA."""
    # Keep model downloads and remote-code modules inside the workspace, as examples/local_decisions.py does.
    os.environ.setdefault('HF_HOME', str(ROOT / '.cache/huggingface'))
    os.environ.setdefault('HF_MODULES_CACHE', str(ROOT / '.cache/huggingface/modules'))
    try:
        import torch
        from huggingface_hub import snapshot_download
        from transformers import AutoModel
    except ImportError as exc:
        raise ValueError(f'the d1 decision backend needs torch, transformers and huggingface_hub ({exc.name} missing); '
                         'run with .venv-d1/bin/python, or choose the jev backend (DECISION_BACKEND=jev)') from None
    from inference import OpenD1Client
    if not torch.cuda.is_available():
        raise ValueError('d1 requires CUDA; refusing to fall back to CPU silently')
    if not torch.cuda.is_bf16_supported():
        raise ValueError('d1 baseline requires a GPU with bf16 support')
    try:
        snapshot = snapshot_download(model, revision=revision, local_files_only=offline)
    except Exception as exc:  # hub errors vary by version; surface them as configuration errors
        hint = ' (offline runs need the snapshot cached; run once without --offline)' if offline else ''
        raise ValueError(f'Cannot obtain {model}@{revision}: {type(exc).__name__}{hint}') from exc
    loaded = AutoModel.from_pretrained(snapshot, trust_remote_code=True, local_files_only=True,
                                       dtype=torch.bfloat16).to('cuda').eval()
    return OpenD1Client(loaded, model_name=f'{model}@{revision}')


def describe(backend):
    """Provenance for manifests: adapter class and, when known, the pinned model."""
    if backend is None:
        return None
    return {'adapter': type(backend).__name__, 'model': getattr(backend, 'model_name', None)
            or getattr(getattr(backend, 'config', None), 'model', None)}
