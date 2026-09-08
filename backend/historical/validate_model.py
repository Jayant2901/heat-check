"""Buzzer-convergence validation gate.

Before a win-probability model is trusted for anything live, every held-out
game's predicted win probability at its FINAL recorded event must land on
the correct side of 0.5 for the outcome that actually happened, and be
confident enough (>= CONVERGENCE_THRESHOLD) that it reads as "converged" to
the true result, not just barely right. A model whose aggregate log-loss
looks good but fails this on more than a small fraction of games is not
safe to ship -- run this right after train.py, before anything else uses
the artifact.
"""

import argparse
import json
from pathlib import Path

import joblib
import pandas as pd

from app.model.features import FEATURE_COLUMNS

ARTIFACTS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "artifacts"

CONVERGENCE_THRESHOLD = 0.9
PASS_RATE_REQUIRED = 0.95


def last_row_per_game(dataset: pd.DataFrame) -> pd.DataFrame:
    idx = dataset.groupby("game_id")["elapsed_seconds"].idxmax()
    return dataset.loc[idx]


def validate(model, dataset: pd.DataFrame) -> dict:
    final_rows = last_row_per_game(dataset)
    proba_home = model.predict_proba(final_rows[FEATURE_COLUMNS])[:, 1]
    home_won = final_rows["home_win"] == 1

    correct_side = (proba_home >= 0.5) == home_won
    converged = ((home_won) & (proba_home >= CONVERGENCE_THRESHOLD)) | (
        (~home_won) & (proba_home <= 1 - CONVERGENCE_THRESHOLD)
    )
    return {
        "n_games": int(len(final_rows)),
        "correct_side_rate": float(correct_side.mean()),
        "converged_rate": float(converged.mean()),
        "convergence_threshold": CONVERGENCE_THRESHOLD,
        "pass_rate_required": PASS_RATE_REQUIRED,
        "passed": bool(converged.mean() >= PASS_RATE_REQUIRED),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset_path")
    parser.add_argument("--model", default=str(ARTIFACTS_DIR / "win_prob_model_v1.joblib"))
    args = parser.parse_args()

    model = joblib.load(args.model)
    dataset = pd.read_parquet(args.dataset_path)
    result = validate(model, dataset)
    print(json.dumps(result, indent=2))

    if not result["passed"]:
        raise SystemExit(
            f"FAILED buzzer-convergence gate: {result['converged_rate']:.1%} of games converged, "
            f"needed >= {PASS_RATE_REQUIRED:.0%}"
        )
    print("PASSED buzzer-convergence gate.")


if __name__ == "__main__":
    main()
