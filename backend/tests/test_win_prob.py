from pathlib import Path

import pytest

from app.model.win_prob import load_model, predict_win_prob


class FakeModel:
    """predict_proba ignores time/period and returns a step function on
    margin, purely so tests can assert the plumbing (feature order, output
    shape) without a real trained artifact."""

    def predict_proba(self, X):
        margin = X["margin"].iloc[0]
        p_home = 0.9 if margin > 0 else 0.1
        return [[1 - p_home, p_home]]


def test_predict_win_prob_uses_margin():
    model = FakeModel()
    assert predict_win_prob(model, margin=10, time_remaining=100, period=4) == pytest.approx(0.9)
    assert predict_win_prob(model, margin=-10, time_remaining=100, period=4) == pytest.approx(0.1)


def test_load_model_missing_file_raises_clear_error(tmp_path):
    missing = tmp_path / "no_such_model.joblib"
    with pytest.raises(FileNotFoundError, match="train.py"):
        load_model(missing)
