"""Trains the win-probability model: LogisticRegression vs
HistGradientBoostingClassifier, chronological split (train on all but the
most recent season, test on the most recent), scored on log-loss/Brier/
accuracy. The better of the two (by log-loss) is saved as the versioned
artifact; both candidates' metrics are recorded in the .meta.json either
way, so the choice is auditable rather than just asserted.

Must pass validate_model.py's buzzer-convergence gate before the artifact
is trusted for anything live -- this script does not enforce that itself,
run validate_model.py right after.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss

from app.model.features import FEATURE_COLUMNS

ARTIFACTS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "artifacts"


def chronological_split(dataset: pd.DataFrame, test_season: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    train = dataset[dataset["season"] != test_season]
    test = dataset[dataset["season"] == test_season]
    return train, test


def _score(model, X, y) -> dict:
    proba = model.predict_proba(X)[:, 1]
    return {
        "log_loss": float(log_loss(y, proba)),
        "brier": float(brier_score_loss(y, proba)),
        "accuracy": float(((proba >= 0.5) == (y == 1)).mean()),
    }


def train_and_select(dataset: pd.DataFrame, test_season: str) -> tuple[object, dict]:
    train_df, test_df = chronological_split(dataset, test_season)
    X_train, y_train = train_df[FEATURE_COLUMNS], train_df["home_win"]
    X_test, y_test = test_df[FEATURE_COLUMNS], test_df["home_win"]

    candidates = {
        "logistic_regression": LogisticRegression(max_iter=1000),
        "hist_gradient_boosting": HistGradientBoostingClassifier(),
    }

    results = {}
    fitted = {}
    for name, model in candidates.items():
        model.fit(X_train, y_train)
        results[name] = _score(model, X_test, y_test)
        fitted[name] = model

    best_name = min(results, key=lambda n: results[n]["log_loss"])
    report = {
        "chosen_model": best_name,
        "test_season": test_season,
        "train_seasons": sorted(train_df["season"].unique().tolist()),
        "n_train_rows": len(train_df),
        "n_test_rows": len(test_df),
        "candidates": results,
    }
    return fitted[best_name], report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset_path")
    parser.add_argument("--test-season", required=True, help="held-out season, e.g. 2025-26")
    args = parser.parse_args()

    dataset = pd.read_parquet(args.dataset_path)
    model, report = train_and_select(dataset, args.test_season)

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = ARTIFACTS_DIR / "win_prob_model_v1.joblib"
    joblib.dump(model, out_path)

    report["feature_columns"] = FEATURE_COLUMNS
    report["trained_at"] = datetime.now(timezone.utc).isoformat()
    out_path.with_suffix(".meta.json").write_text(json.dumps(report, indent=2))

    print(json.dumps(report, indent=2))
    print(f"Saved -> {out_path}")


if __name__ == "__main__":
    main()
