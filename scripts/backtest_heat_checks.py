#!/usr/bin/env python
"""How many heat checks does a game get at each z threshold?

Replays finished games through the live engine (the same play-by-play path a
live game takes: one exact snapshot per action, core/engine.py) at several
thresholds and reports flags per game. Use it to pick ANOMALY_Z_THRESHOLD:
a typical game should flag a handful of runs, not none and not dozens.

    python scripts/backtest_heat_checks.py --fetch-days 30     # download, then run
    python scripts/backtest_heat_checks.py                     # reuse the cache

Games come from ESPN's public API (see espn_cache.py) and are cached in
data/backtest_cache/. Results are written to
data/artifacts/heat_check_backtest.json.

What this does NOT show
-----------------------
* It counts flags; it does not say whether a flagged run is "really" unusual.
  The z-score compares a run's speed with the baseline table, and that is the
  only yardstick.
* The run baseline was built from the 2023-24, 2024-25 and 2025-26 seasons and
  the win-probability model was tested on 2025-26, so those two overlap. The
  games replayed here are from the current season, which neither saw, but
  they are a few weeks of early-season games, not a full season.
* Heat checks are flagged at the first moment a run crosses the threshold, as
  they are live, so a run that keeps growing is still one flag.
"""

import argparse
import dataclasses
import json
import statistics
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import espn_cache  # also puts backend/ on sys.path

from app.core import engine
from app.core.artifacts import get_baseline, get_model
from app.core.game_state import GameState
from app.core.interfaces import GameSnapshot
from app.nba.replays import NotReplayable, snapshots_from_playbyplay

OUT_PATH = espn_cache.ROOT / "data" / "artifacts" / "heat_check_backtest.json"
DEFAULT_THRESHOLDS = (1.0, 1.5, 2.0, 2.5, 3.0)
NOTE = (
    "The run baseline was built from the 2023-24, 2024-25 and 2025-26 seasons; the win-probability model was "
    "tested on 2025-26, so the two overlap. These games are from the current season, which neither saw."
)


def percentile(values: list[int], q: float) -> float:
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, int(q * len(ordered)))])


def summarise(counts: list[int]) -> dict:
    return {
        "games": len(counts),
        "total_flags": sum(counts),
        "mean_per_game": round(statistics.mean(counts), 2),
        "median_per_game": statistics.median(counts),
        "p10": percentile(counts, 0.10),
        "p90": percentile(counts, 0.90),
        "max": max(counts),
        "share_with_zero": round(sum(1 for c in counts if c == 0) / len(counts), 3),
        "share_with_ten_or_more": round(sum(1 for c in counts if c >= 10) / len(counts), 3),
    }


def game_snapshots(game: dict) -> list[GameSnapshot] | None:
    pbp, home, away = espn_cache.to_pbp(game)
    try:
        return [GameSnapshot(**s) for s in snapshots_from_playbyplay(pbp, game["event_id"], home, away)]
    except NotReplayable:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fetch-days", type=int, default=0, help="download finished games from the last N days first")
    parser.add_argument("--min-games", type=int, default=100)
    parser.add_argument("--thresholds", type=float, nargs="+", default=list(DEFAULT_THRESHOLDS))
    args = parser.parse_args()

    games = espn_cache.fetch_games(args.fetch_days) if args.fetch_days else espn_cache.load_cached()
    prepared = [(g, s) for g in games if (s := game_snapshots(g))]
    print(f"{len(prepared)} games usable ({len(games) - len(prepared)} skipped: too little play-by-play)")
    if len(prepared) < args.min_games:
        sys.exit(f"Need at least {args.min_games} games for this backtest; have {len(prepared)}. Use --fetch-days.")

    model, baseline = get_model(), get_baseline()
    base_settings = engine.settings
    results = {}
    try:
        for threshold in args.thresholds:
            engine.settings = dataclasses.replace(base_settings, anomaly_z_threshold=threshold)
            counts = []
            for game, snapshots in prepared:
                state = GameState(game_id=game["event_id"])
                engine.process_snapshots(state, snapshots, model, baseline, now=0.0)
                counts.append(len(state.anomalies))
            results[str(threshold)] = summarise(counts)
    finally:
        engine.settings = base_settings

    days = sorted(g["day"] for g, _ in prepared)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "ESPN play-by-play, regular season",
        "n_games": len(prepared),
        "first_day": days[0],
        "last_day": days[-1],
        "min_run_points": base_settings.anomaly_min_magnitude,
        "thresholds": results,
        "note": NOTE,
    }
    OUT_PATH.write_text(json.dumps(report, indent=2) + "\n")

    print(f"\nFlags per game, {report['n_games']} games, {days[0]} to {days[-1]} (runs of {report['min_run_points']}+ points)")
    print(f"{'z >=':>6} {'mean':>6} {'median':>7} {'p10':>5} {'p90':>5} {'max':>5} {'none':>7} {'10+':>6}")
    for threshold, row in results.items():
        print(
            f"{threshold:>6} {row['mean_per_game']:>6} {row['median_per_game']:>7} {row['p10']:>5.0f} {row['p90']:>5.0f} "
            f"{row['max']:>5} {row['share_with_zero']:>7.0%} {row['share_with_ten_or_more']:>6.0%}"
        )
    print(f"\nNOTE: {NOTE}\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
