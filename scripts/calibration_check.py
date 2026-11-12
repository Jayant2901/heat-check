#!/usr/bin/env python
"""Does "70%" mean 70%? A calibration curve and Brier score for the win model.

The held-out numbers in data/artifacts/win_prob_model_v1.meta.json give
accuracy, log loss and Brier score over the 2025-26 season, but not whether a
predicted 70% is followed by a win about 70% of the time. This replays finished
current-season games (never seen by the model: it was trained on 2023-24 and
2024-25 and tested on 2025-26) through the same engine and compares each
state's predicted home win chance with how the game ended.

    python scripts/calibration_check.py --fetch-days 30
    python scripts/calibration_check.py

Reads the games cached by backtest_heat_checks.py / espn_cache.py and writes
data/artifacts/win_prob_calibration_v1.json, which /api/model-info serves.

Read it with care: every play of a game is one "state", and the states within
a game are not independent (one result decides all of them), so the effective
sample is the number of GAMES, not the number of states.
"""

import argparse
import json
import sys
from datetime import datetime, timezone

import espn_cache  # also puts backend/ on sys.path
import numpy as np

from app.core.artifacts import get_model
from app.core.engine import _home_win_probs
from app.core.interfaces import GameSnapshot
from app.nba.replays import NotReplayable, snapshots_from_playbyplay

OUT_PATH = espn_cache.ROOT / "data" / "artifacts" / "win_prob_calibration_v1.json"
BINS = 10


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fetch-days", type=int, default=0)
    args = parser.parse_args()
    games = espn_cache.fetch_games(args.fetch_days) if args.fetch_days else espn_cache.load_cached()

    model = get_model()
    probs, outcomes, game_ids, days = [], [], [], []
    for game in games:
        pbp, home, away = espn_cache.to_pbp(game)
        try:
            snaps = [GameSnapshot(**s) for s in snapshots_from_playbyplay(pbp, game["event_id"], home, away)]
        except NotReplayable:
            continue
        final = snaps[-1]
        if final.home_score == final.away_score:
            continue
        p = _home_win_probs(model, snaps)
        probs.append(p)
        outcomes.append(np.full(len(p), float(final.home_score > final.away_score)))
        game_ids.append(np.full(len(p), len(days)))
        days.append(game["day"])
    if not days:
        sys.exit("No usable games; run with --fetch-days.")

    p, y, g = np.concatenate(probs), np.concatenate(outcomes), np.concatenate(game_ids)
    edges = np.linspace(0, 1, BINS + 1)
    which = np.minimum(np.digitize(p, edges[1:-1]), BINS - 1)
    bins = []
    for b in range(BINS):
        mask = which == b
        n = int(mask.sum())
        bins.append(
            {
                "lo": round(float(edges[b]), 2),
                "hi": round(float(edges[b + 1]), 2),
                "n_states": n,
                "n_games": int(len(np.unique(g[mask]))) if n else 0,
                "mean_predicted": round(float(p[mask].mean()), 4) if n else None,
                "observed_home_win_rate": round(float(y[mask].mean()), 4) if n else None,
            }
        )
    clipped = np.clip(p, 1e-6, 1 - 1e-6)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "ESPN play-by-play, regular season, games the model never saw",
        "n_games": len(days),
        "n_states": int(len(p)),
        "first_day": min(days),
        "last_day": max(days),
        "brier": round(float(np.mean((p - y) ** 2)), 4),
        "brier_coin_flip": 0.25,
        "log_loss": round(float(-np.mean(y * np.log(clipped) + (1 - y) * np.log(1 - clipped))), 4),
        "accuracy": round(float(np.mean((p >= 0.5) == (y == 1))), 4),
        "bins": bins,
        "note": (
            "Every play of a game is one state, and the states of a game share one outcome, so the effective "
            f"sample is the {len(days)} games, not {len(p):,} states. Bins with few games are noisy."
        ),
    }
    OUT_PATH.write_text(json.dumps(report, indent=2) + "\n")

    print(f"{report['n_games']} games, {report['n_states']:,} states, {report['first_day']} to {report['last_day']}")
    print(f"Brier {report['brier']} (always-50% scores 0.25; lower is better), log loss {report['log_loss']}, accuracy {report['accuracy']:.1%}")
    print(f"{'predicted':>12} {'states':>8} {'games':>6} {'mean pred':>10} {'home won':>9}")
    for b in bins:
        if b["n_states"]:
            print(f"{b['lo']:>5.0%}-{b['hi']:<5.0%} {b['n_states']:>8} {b['n_games']:>6} {b['mean_predicted']:>10.1%} {b['observed_home_win_rate']:>9.1%}")
    print(f"\n{report['note']}\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
