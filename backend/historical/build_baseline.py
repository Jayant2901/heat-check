"""Builds the run-magnitude/time-elapsed baseline table for anomaly z-scoring.

Replays each historical game's score sequence through the SAME RunDetector
used live (app/anomaly/run_detector.py), so "how fast do runs of size N
happen" is measured against the identical run-closing rules the live/replay
engine uses, rather than a separately-reimplemented notion of a "run" that
could quietly disagree with it.

Each closed run is bucketed by (period, time_bucket) at the run's START --
not its end -- since "how fast do runs like this begin" is anchored to when
they started. The live engine checks a run's z-score using the *current*
tick's period/clock while the run is still open, which for a genuinely fast
(anomalous) run is only seconds away from its start bucket in practice; this
is a documented approximation, not a precision mismatch worth chasing given
the project's live data is boxscore-level, not play-by-play, to begin with.

Durations are log-transformed before averaging -- see app/anomaly/baseline.py's
module docstring for why raw-duration z-scoring doesn't work for this
right-skewed a variable.
"""

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from app.anomaly.baseline import time_bucket
from app.anomaly.run_detector import DEFAULT_GAP_SECONDS, RunDetector
from app.core.config import settings
from app.core.game_state import RunState
from app.model.features import elapsed_seconds
from historical.fetch_games import RAW_DIR
from historical.pbp_utils import parse_clock_seconds, parse_score_value

ARTIFACTS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "artifacts"


def _extract_closed_runs(season: str, game_id: str) -> list[dict]:
    pbp = pd.read_parquet(RAW_DIR / season / "pbp" / f"{game_id}.parquet")
    detector = RunDetector(gap_seconds=DEFAULT_GAP_SECONDS)
    current: RunState | None = None
    run_start_period = None
    run_start_clock = None
    home_score = 0
    away_score = 0
    closed_runs = []

    for row in pbp.itertuples(index=False):
        home_val = parse_score_value(getattr(row, "scoreHome", ""))
        away_val = parse_score_value(getattr(row, "scoreAway", ""))
        period = int(row.period)
        clock = parse_clock_seconds(row.clock)

        home_delta = (home_val - home_score) if home_val is not None else 0
        away_delta = (away_val - away_score) if away_val is not None else 0
        if home_val is not None:
            home_score = home_val
        if away_val is not None:
            away_score = away_val

        if home_delta and away_delta:
            current, run_start_period, run_start_clock = None, None, None
            continue
        if not home_delta and not away_delta:
            continue

        team, points = ("home", home_delta) if home_delta else ("away", away_delta)
        t = elapsed_seconds(period, clock)
        update = detector.on_score(current, team, points, t, home_score - away_score)

        if update.closed_run is not None and run_start_period is not None:
            closed_runs.append(
                {
                    "period": run_start_period,
                    "time_bucket": time_bucket(run_start_clock),
                    "magnitude": update.closed_run.points_scored,
                    "duration": update.closed_run.last_scored_elapsed - update.closed_run.start_elapsed_seconds,
                }
            )

        if update.current_run is not current:
            run_start_period, run_start_clock = period, clock
        current = update.current_run

    return closed_runs


def build_baseline(seasons: list[str]) -> pd.DataFrame:
    rows = []
    for season in seasons:
        game_ids = sorted(p.stem for p in (RAW_DIR / season / "pbp").glob("*.parquet"))
        for game_id in game_ids:
            rows.extend(_extract_closed_runs(season, game_id))

    runs_df = pd.DataFrame(rows)
    runs_df = runs_df[runs_df["duration"] > 0]  # a same-tick double-score can yield a zero-duration artifact
    # Below anomaly_min_magnitude, a "run" is really just a pair of free
    # throws (clock frozen -> near-zero duration) -- see core/config.py.
    # Excluding them keeps the artifact free of buckets nobody queries.
    runs_df = runs_df[runs_df["magnitude"] >= settings.anomaly_min_magnitude]
    runs_df["log_duration"] = runs_df["duration"].apply(math.log)
    baseline = (
        runs_df.groupby(["period", "time_bucket", "magnitude"])["log_duration"]
        .agg(mean_log_duration="mean", std_log_duration="std", n="count")
        .reset_index()
    )
    baseline["std_log_duration"] = baseline["std_log_duration"].fillna(0.0)
    return baseline


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("seasons", nargs="+")
    args = parser.parse_args()

    baseline = build_baseline(args.seasons)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = ARTIFACTS_DIR / "run_baseline_v1.parquet"
    baseline.to_parquet(out_path, index=False)

    meta = {
        "seasons": args.seasons,
        "n_buckets": len(baseline),
        "n_runs": int(baseline["n"].sum()),
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    out_path.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2))
    print(f"{meta['n_buckets']} buckets from {meta['n_runs']} runs -> {out_path}")


if __name__ == "__main__":
    main()
