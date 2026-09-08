"""Lazily loads and caches the trained model + baseline once per process.

Every endpoint that needs them (stream.py, replay.py) calls get_model()/
get_baseline() rather than loading the files themselves, so the artifacts
are read from disk exactly once no matter how many games get tracked.
"""

from app.anomaly.baseline import load_baseline
from app.core.config import settings
from app.model.win_prob import load_model

_model = None
_baseline = None


def get_model():
    global _model
    if _model is None:
        _model = load_model(settings.win_prob_model_path)
    return _model


def get_baseline():
    global _baseline
    if _baseline is None:
        _baseline = load_baseline(settings.run_baseline_path)
    return _baseline
