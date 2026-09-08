"""Converts one fetched historical game (raw play-by-play + games index) into
a replay fixture: a JSON sequence of boxscore-shaped snapshots, identical in
shape to what NbaLiveDataSource would produce polling that same game live.
This is what lets ReplaySource (app/datasources/replay_source.py) feed the
exact same engine code as live polling.

Play-by-play only carries scoreHome/scoreAway on rows where the score
actually changed, so they're forward-filled here -- the resulting snapshot
sequence is denser than a real ~12s live poll (one snapshot per pbp event,
not per poll tick), which just means replay ticks faster through quiet
stretches, not that it's wrong.
"""

import argparse
import json
from pathlib import Path

import pandas as pd

from historical.fetch_games import RAW_DIR
from historical.pbp_utils import parse_clock_seconds, parse_score_value

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "replay_fixtures"


def _find_teams(season: str, game_id: str) -> tuple[str, str]:
    games = pd.read_parquet(RAW_DIR / season / "games.parquet")
    rows = games[games["GAME_ID"] == game_id]
    home = rows[rows["MATCHUP"].str.contains("vs.", regex=False)]["TEAM_ABBREVIATION"].iloc[0]
    away = rows[rows["MATCHUP"].str.contains("@", regex=False)]["TEAM_ABBREVIATION"].iloc[0]
    return home, away


def build_fixture(season: str, game_id: str) -> dict:
    pbp = pd.read_parquet(RAW_DIR / season / "pbp" / f"{game_id}.parquet")
    home_team, away_team = _find_teams(season, game_id)

    snapshots = []
    home_score = 0
    away_score = 0
    last_index = len(pbp) - 1
    for i, row in enumerate(pbp.itertuples(index=False)):
        home_val = parse_score_value(getattr(row, "scoreHome", ""))
        away_val = parse_score_value(getattr(row, "scoreAway", ""))
        if home_val is not None:
            home_score = home_val
        if away_val is not None:
            away_score = away_val
        snapshots.append(
            {
                "game_id": game_id,
                "period": int(row.period),
                "game_clock_seconds_remaining": parse_clock_seconds(row.clock),
                "home_team": home_team,
                "away_team": away_team,
                "home_score": home_score,
                "away_score": away_score,
                "game_status": "final" if i == last_index else "live",
            }
        )

    return {
        "game_id": game_id,
        "season": season,
        "home_team": home_team,
        "away_team": away_team,
        "final_home_score": home_score,
        "final_away_score": away_score,
        "snapshots": snapshots,
    }


def _update_manifest(fixture: dict, label: str | None) -> None:
    manifest_path = FIXTURES_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"fixtures": []}
    manifest["fixtures"] = [f for f in manifest["fixtures"] if f["game_id"] != fixture["game_id"]]
    manifest["fixtures"].append(
        {
            "game_id": fixture["game_id"],
            "season": fixture["season"],
            "home_team": fixture["home_team"],
            "away_team": fixture["away_team"],
            "final_home_score": fixture["final_home_score"],
            "final_away_score": fixture["final_away_score"],
            "label": label or f"{fixture['away_team']} @ {fixture['home_team']}",
        }
    )
    manifest_path.write_text(json.dumps(manifest, indent=2))


def save_fixture(season: str, game_id: str, label: str | None = None) -> Path:
    fixture = build_fixture(season, game_id)
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    out_path = FIXTURES_DIR / f"{game_id}.json"
    out_path.write_text(json.dumps(fixture))
    _update_manifest(fixture, label)
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("season")
    parser.add_argument("game_id")
    parser.add_argument("--label", default=None, help='e.g. "record comeback"')
    args = parser.parse_args()
    path = save_fixture(args.season, args.game_id, args.label)
    print(f"Wrote fixture ({len(json.loads(path.read_text())['snapshots'])} snapshots) -> {path}")


if __name__ == "__main__":
    main()
