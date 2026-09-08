"""Builds the win-probability training dataset from fetched play-by-play.

One row per play-by-play event (after forward-filling score), restricted to
exactly the live-parity feature set (see app/model/features.py) plus the
game's final outcome as the label. Deliberately NOT one row per
poll-tick-equivalent -- pbp events are much denser than a live ~12s poll,
which just means more (highly correlated) training rows, not wrong ones.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from app.model.features import elapsed_seconds, time_remaining_seconds
from historical.fetch_games import RAW_DIR
from historical.pbp_utils import parse_clock_seconds, parse_score_value

PROCESSED_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "processed"


def build_game_rows(season: str, game_id: str) -> pd.DataFrame | None:
    pbp = pd.read_parquet(RAW_DIR / season / "pbp" / f"{game_id}.parquet")
    if pbp.empty:
        return None

    home_score = 0
    away_score = 0
    rows = []
    for row in pbp.itertuples(index=False):
        home_val = parse_score_value(getattr(row, "scoreHome", ""))
        away_val = parse_score_value(getattr(row, "scoreAway", ""))
        if home_val is not None:
            home_score = home_val
        if away_val is not None:
            away_score = away_val
        period = int(row.period)
        clock = parse_clock_seconds(row.clock)
        rows.append(
            {
                "game_id": game_id,
                "season": season,
                "elapsed_seconds": elapsed_seconds(period, clock),
                "margin": home_score - away_score,
                "time_remaining_seconds": time_remaining_seconds(period, clock),
                "period": period,
            }
        )

    if home_score == away_score:
        # A real NBA game can't end tied -- this means the pbp is malformed
        # or truncated for this game (missing final rows), so skip it
        # rather than train on a fabricated label.
        return None

    df = pd.DataFrame(rows)
    df["home_win"] = int(home_score > away_score)
    return df


def build_dataset(seasons: list[str]) -> tuple[pd.DataFrame, list[tuple[str, str]]]:
    frames = []
    skipped = []
    for season in seasons:
        game_ids = sorted(p.stem for p in (RAW_DIR / season / "pbp").glob("*.parquet"))
        for game_id in game_ids:
            df = build_game_rows(season, game_id)
            if df is None:
                skipped.append((season, game_id))
            else:
                frames.append(df)
    dataset = pd.concat(frames, ignore_index=True)
    return dataset, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("seasons", nargs="+")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    dataset, skipped = build_dataset(args.seasons)
    if skipped:
        preview = skipped[:10]
        print(f"Skipped {len(skipped)} malformed games: {preview}{'...' if len(skipped) > 10 else ''}")

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out_name = args.out or f"training_rows_{args.seasons[0]}_{args.seasons[-1]}.parquet"
    out_path = PROCESSED_DIR / out_name
    dataset.to_parquet(out_path, index=False)

    meta = {
        "seasons": args.seasons,
        "n_rows": len(dataset),
        "n_games": int(dataset["game_id"].nunique()),
        "n_skipped_games": len(skipped),
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    out_path.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2))
    print(f"{meta['n_rows']} rows across {meta['n_games']} games -> {out_path}")


if __name__ == "__main__":
    main()
