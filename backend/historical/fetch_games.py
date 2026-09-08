"""Pull a season's game index via nba_api's stats endpoints.

One row per team per game as returned by the API; collapsed to one row per
GAME_ID before saving, since fetch_pbp.py only needs the unique game IDs.
Resumable: re-running skips any season whose games.parquet already exists.
"""

import argparse
import sys
import time
from pathlib import Path

import pandas as pd
from nba_api.stats.endpoints import leaguegamefinder

RAW_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "raw"
SEASON_TYPES = ["Regular Season", "Playoffs"]


def fetch_season_games(season: str, sleep_seconds: float = 1.0) -> pd.DataFrame:
    frames = []
    for season_type in SEASON_TYPES:
        finder = leaguegamefinder.LeagueGameFinder(
            season_nullable=season,
            season_type_nullable=season_type,
            league_id_nullable="00",
        )
        df = finder.get_data_frames()[0]
        df["SEASON_TYPE"] = season_type
        frames.append(df)
        time.sleep(sleep_seconds)
    games = pd.concat(frames, ignore_index=True)
    return games.drop_duplicates(subset=["GAME_ID", "TEAM_ID"]).reset_index(drop=True)


def fetch_and_save_season(season: str, force: bool = False) -> Path:
    out_dir = RAW_DIR / season
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "games.parquet"
    if out_path.exists() and not force:
        print(f"[skip] {season} games already fetched -> {out_path}")
        return out_path
    print(f"[fetch] {season} games...")
    games = fetch_season_games(season)
    games.to_parquet(out_path, index=False)
    print(f"[done] {season}: {games['GAME_ID'].nunique()} unique games -> {out_path}")
    return out_path


def unique_game_ids(season: str) -> list[str]:
    path = RAW_DIR / season / "games.parquet"
    df = pd.read_parquet(path, columns=["GAME_ID"])
    return sorted(df["GAME_ID"].unique().tolist())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("seasons", nargs="+", help="e.g. 2025-26 2024-25")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    for season in args.seasons:
        try:
            fetch_and_save_season(season, force=args.force)
        except Exception as exc:
            print(f"[FAILED] {season}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
