"""Loads the trained win-probability artifact and wraps prediction.

The engine takes the model as an explicit argument (see core/engine.py)
rather than importing a global here, so tests can inject a trivial fake
without a real trained artifact on disk. main.py's startup lifespan is the
one place that calls load_model() against the real path.
"""

from pathlib import Path
from typing import Protocol

import joblib
import pandas as pd

from app.model.features import FEATURE_COLUMNS, build_feature_row


class WinProbModel(Protocol):
    def predict_proba(self, X: pd.DataFrame): ...


def load_model(path: Path) -> WinProbModel:
    if not path.exists():
        raise FileNotFoundError(
            f"No win-probability model at {path}. Run backend/historical/train.py "
            "(after fetch_games.py + fetch_pbp.py + build_dataset.py) to produce it."
        )
    return joblib.load(path)


def predict_win_prob(model: WinProbModel, margin: int, time_remaining: float, period: int) -> float:
    """P(home team wins), given the live-parity feature set."""
    row = build_feature_row(margin, time_remaining, period)
    X = pd.DataFrame([row], columns=FEATURE_COLUMNS)
    return float(model.predict_proba(X)[0][1])
