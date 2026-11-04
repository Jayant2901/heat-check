import json
from datetime import date
from pathlib import Path

import pytest

from app.datasources.nba_live import snapshot_from_boxscore
from app.nba.parse import (
    clock_text,
    game_score,
    parse_boxscore_players,
    parse_clock_seconds,
    parse_final_games,
    parse_scoreboard,
    period_label,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_clock_seconds():
    assert parse_clock_seconds("PT05M12.00S") == 312.0
    assert parse_clock_seconds("PT00M00.40S") == pytest.approx(0.4)
    assert parse_clock_seconds("PT25M") == 1500.0  # boxscore minutesCalculated has no seconds part
    assert parse_clock_seconds("PT45.5S") == 45.5
    assert parse_clock_seconds("") == 0.0
    assert parse_clock_seconds(None) == 0.0
    assert parse_clock_seconds("12:00") == 0.0


def test_period_labels_and_clock_text():
    assert [period_label(p) for p in (1, 4, 5, 6, 7)] == ["Q1", "Q4", "OT", "2OT", "3OT"]
    assert period_label(0) == ""
    assert period_label(None) == ""
    assert clock_text(3, 312.0) == "Q3 5:12"
    assert clock_text(5, 4.6) == "OT 0:04"
    assert clock_text(0, 720) == ""


def test_game_score_matches_hand_computed_line():
    # 30 pts, 11/20 FG, 6/8 FT, 2 OREB, 10 REB, 2 STL, 5 AST, 1 BLK, 3 PF, 4 TOV
    # = 30 + 4.4 - 14 - 0.8 + 1.4 + 2.4 + 2 + 3.5 + 0.7 - 1.2 - 4 = 24.4
    stats = {
        "points": 30,
        "fieldGoalsMade": 11,
        "fieldGoalsAttempted": 20,
        "freeThrowsMade": 6,
        "freeThrowsAttempted": 8,
        "reboundsOffensive": 2,
        "reboundsTotal": 10,
        "steals": 2,
        "assists": 5,
        "blocks": 1,
        "foulsPersonal": 3,
        "turnovers": 4,
    }
    assert game_score(stats) == pytest.approx(24.4)
    assert game_score({}) == 0.0


def _board_game(game_id, status, start, home="BOS", away="NYK", **extra):
    game = {
        "gameId": game_id,
        "gameStatus": status,
        "gameStatusText": "Q3" if status == 2 else "",
        "period": 3 if status == 2 else 0,
        "gameClock": "PT05M12.00S" if status == 2 else "",
        "gameTimeUTC": start,
        "homeTeam": {"teamTricode": home, "teamCity": "Boston", "teamName": "Celtics", "score": 80, "wins": 3, "losses": 1},
        "awayTeam": {"teamTricode": away, "teamCity": "New York", "teamName": "Knicks", "score": 77, "wins": 2, "losses": 2},
    }
    game.update(extra)
    return game


def test_scoreboard_orders_live_then_scheduled_then_final_and_parses_leaders():
    raw = {
        "scoreboard": {
            "gameDate": "2026-11-04",
            "games": [
                _board_game("F1", 3, "2026-11-04T23:00:00Z"),
                _board_game("S2", 1, "2026-11-05T02:00:00Z"),
                _board_game("S1", 1, "2026-11-05T00:00:00Z"),
                _board_game(
                    "L1",
                    2,
                    "2026-11-05T00:30:00Z",
                    gameLeaders={
                        "homeLeaders": {"personId": 1, "name": "Jay Smith", "points": 21, "rebounds": 5, "assists": 4},
                        "awayLeaders": {"personId": 0, "name": "", "points": 0},
                    },
                ),
                {"gameStatus": 2},  # no gameId -> skipped
            ],
        }
    }
    board = parse_scoreboard(raw)
    assert board["game_date"] == "2026-11-04"
    assert [g["game_id"] for g in board["games"]] == ["L1", "S1", "S2", "F1"]

    live = board["games"][0]
    assert live["status"] == "live"
    assert live["clock_text"] == "Q3 5:12"
    assert live["home"] == {"tricode": "BOS", "city": "Boston", "name": "Celtics", "score": 80, "wins": 3, "losses": 1}
    assert live["home_leader"] == {"person_id": 1, "name": "Jay Smith", "points": 21, "rebounds": 5, "assists": 4}
    assert live["away_leader"] is None
    assert board["games"][1]["clock_text"] == ""


def test_scoreboard_tolerates_empty_payloads():
    assert parse_scoreboard({}) == {"game_date": "", "games": []}
    assert parse_scoreboard(None) == {"game_date": "", "games": []}


def test_parse_final_games_filters_by_window_and_status():
    def game(game_id, status, day):
        return {
            "gameId": game_id,
            "gameStatus": status,
            "gameDateEst": f"{day}T00:00:00Z",
            "homeTeam": {"teamTricode": "BOS", "score": 110},
            "awayTeam": {"teamTricode": "NYK", "score": 104},
        }

    raw = {
        "leagueSchedule": {
            "gameDates": [
                {"games": [game("old", 3, "2026-10-20")]},
                {"games": [game("in1", 3, "2026-10-29"), game("live", 2, "2026-10-29")]},
                {"games": [game("in2", 3, "2026-11-04"), game("future", 1, "2026-11-05")]},
                {"games": [{"gameId": "baddate", "gameStatus": 3, "gameDateEst": "soon"}]},
            ]
        }
    }
    games = parse_final_games(raw, date(2026, 10, 28), date(2026, 11, 4))
    assert [g["game_id"] for g in games] == ["in1", "in2"]
    assert games[0] == {
        "game_id": "in1",
        "date": "2026-10-29",
        "home": "BOS",
        "away": "NYK",
        "home_score": 110,
        "away_score": 104,
    }


def test_boxscore_players_skip_dnp_and_compute_game_score():
    raw = {
        "game": {
            "homeTeam": {
                "teamTricode": "BOS",
                "players": [
                    {
                        "personId": 1,
                        "name": "Starter One",
                        "statistics": {
                            "minutes": "PT34M12.00S",
                            "points": 30,
                            "fieldGoalsMade": 11,
                            "fieldGoalsAttempted": 20,
                            "freeThrowsMade": 6,
                            "freeThrowsAttempted": 8,
                            "reboundsOffensive": 2,
                            "reboundsTotal": 10,
                            "steals": 2,
                            "assists": 5,
                            "blocks": 1,
                            "foulsPersonal": 3,
                            "turnovers": 4,
                        },
                    },
                    {"personId": 2, "name": "Bench DNP", "statistics": {"minutes": "", "points": 0}},
                    {"personId": 3, "name": "Zero Minutes", "statistics": {"minutes": "PT00M00.00S"}},
                ],
            },
            "awayTeam": {
                "teamTricode": "NYK",
                "players": [
                    {"personId": 4, "name": "Calc Minutes", "statistics": {"minutesCalculated": "PT12M", "points": 4}},
                ],
            },
        }
    }
    players = parse_boxscore_players(raw)
    assert [p["name"] for p in players] == ["Starter One", "Calc Minutes"]
    starter = players[0]
    assert starter["team"] == "BOS" and starter["opponent"] == "NYK"
    assert starter["minutes"] == 34.2
    assert starter["game_score"] == pytest.approx(24.4)
    assert players[1]["minutes"] == 12.0


def test_live_snapshot_from_boxscore_handles_pregame():
    snap = snapshot_from_boxscore(
        {
            "gameId": "0022600001",
            "gameStatus": 1,
            "period": 0,
            "gameClock": "",
            "homeTeam": {"teamTricode": "BOS", "score": 0},
            "awayTeam": {"teamTricode": "NYK"},
        }
    )
    assert snap.game_status == "scheduled"
    assert snap.period == 0
    assert snap.game_clock_seconds_remaining == 0.0
    assert snap.away_score == 0


NBA_API_SAMPLE_PLAYER = {
    # Copied from the sample payload in nba_api/live/nba/endpoints/boxscore.py
    # (Jaylen Brown, ORL at BOS, 2021-01-15), which mirrors the CDN response.
    "status": "ACTIVE",
    "personId": 1627759,
    "name": "Jaylen Brown",
    "statistics": {
        "assists": 8, "blocks": 0, "fieldGoalsAttempted": 12, "fieldGoalsMade": 6, "foulsPersonal": 1,
        "freeThrowsAttempted": 7, "freeThrowsMade": 7, "minutes": "PT25M01.00S", "minutesCalculated": "PT25M",
        "points": 21, "reboundsDefensive": 2, "reboundsOffensive": 0, "reboundsTotal": 2, "steals": 1,
        "threePointersAttempted": 5, "threePointersMade": 2, "turnovers": 2,
    },
}


def test_parses_the_nba_api_sample_boxscore_shape():
    raw = {
        "game": {
            "homeTeam": {"teamTricode": "BOS", "players": [NBA_API_SAMPLE_PLAYER]},
            "awayTeam": {"teamTricode": "ORL", "players": []},
        }
    }
    [player] = parse_boxscore_players(raw)
    assert player["minutes"] == 25.0
    assert (player["pts"], player["reb"], player["ast"], player["fgm"], player["fga"]) == (21, 2, 8, 6, 12)
    # 21 + 2.4 - 8.4 - 0 + 0 + 0.6 + 1 + 5.6 + 0 - 0.4 - 2 = 19.8
    assert player["game_score"] == pytest.approx(19.8)


@pytest.mark.skipif(not (FIXTURES / "boxscore_real.json").exists(), reason="no real boxscore captured yet")
def test_real_saved_boxscore_parses():
    raw = json.loads((FIXTURES / "boxscore_real.json").read_text())
    players = parse_boxscore_players(raw)
    assert players
    assert all(p["minutes"] > 0 and p["team"] for p in players)
    home = raw["game"]["homeTeam"]
    home_points = sum(p["pts"] for p in players if p["side"] == "home")
    assert home_points == home["score"]
