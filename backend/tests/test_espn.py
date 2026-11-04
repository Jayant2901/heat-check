from datetime import date

import httpx
import pytest

import app.nba.client as nba_client
from app.nba.client import AutoNbaClient, HttpNbaClient, NbaUnavailable
from app.nba.espn import (
    EspnNbaClient,
    clock_from_text,
    is_espn_id,
    translate_boxscore,
    translate_playbyplay,
    translate_scoreboard,
    tricode,
)
from app.nba.moments import scoring_events
from app.nba.parse import parse_boxscore_players, parse_final_games, parse_scoreboard

TODAY = date(2026, 11, 4)


def competitor(home_away, abbreviation, team_id, score, record="3-1", leaders=None):
    return {
        "homeAway": home_away,
        "score": str(score),
        "team": {"id": team_id, "abbreviation": abbreviation, "location": "City", "name": f"{abbreviation} Name"},
        "records": [{"name": "overall", "summary": record}],
        "leaders": leaders or [],
    }


def leader(category, athlete_id, name, value):
    return {
        "name": category,
        "leaders": [{"value": value, "displayValue": str(value), "athlete": {"id": athlete_id, "displayName": name}}],
    }


def event(event_id, state, home, away, period=0, clock=0.0, date_text="2026-11-05T00:30Z"):
    return {
        "id": event_id,
        "date": date_text,
        "competitions": [
            {
                "status": {
                    "clock": clock,
                    "displayClock": "",
                    "period": period,
                    "type": {"state": state, "shortDetail": "Q3 5:12" if state == "in" else "Final"},
                },
                "competitors": [home, away],
            }
        ],
    }


def sample_scoreboard():
    return {
        "events": [
            event(
                "401700001",
                "in",
                competitor(
                    "home",
                    "GS",
                    "9",
                    80,
                    leaders=[leader("points", "1", "Home Star", 25), leader("rebounds", "1", "Home Star", 9)],
                ),
                competitor("away", "NY", "18", 77, leaders=[leader("points", "2", "Away Star", 21)]),
                period=3,
                clock=312.0,
            ),
            event("401700002", "post", competitor("home", "SA", "24", 101), competitor("away", "UTAH", "26", 99)),
        ]
    }


def test_tricodes_and_ids():
    assert [tricode(x) for x in ("GS", "NY", "SA", "NO", "UTAH", "WSH", "BOS")] == [
        "GSW", "NYK", "SAS", "NOP", "UTA", "WAS", "BOS",
    ]
    assert is_espn_id("401700001")
    assert not is_espn_id("0022600001")
    assert clock_from_text("5:12") == 312.0
    assert clock_from_text("45.2") == pytest.approx(45.2)
    assert clock_from_text("") is None


def test_scoreboard_translates_into_nba_shape():
    board = parse_scoreboard(translate_scoreboard(sample_scoreboard(), "2026-11-04"))
    assert board["game_date"] == "2026-11-04"
    live, final = board["games"]
    assert live["game_id"] == "401700001"
    assert live["status"] == "live"
    assert live["clock_text"] == "Q3 5:12"
    assert live["start_time_utc"] == "2026-11-05T00:30:00Z"
    assert live["home"]["tricode"] == "GSW" and live["home"]["score"] == 80
    assert (live["home"]["wins"], live["home"]["losses"]) == (3, 1)
    assert live["home_leader"] == {"person_id": "1", "name": "Home Star", "points": 25, "rebounds": 9, "assists": None}
    assert live["away_leader"]["rebounds"] is None
    assert final["status"] == "final"
    assert final["away"]["tricode"] == "UTA"


def sample_summary():
    labels = ["MIN", "FG", "3PT", "FT", "OREB", "DREB", "REB", "AST", "STL", "BLK", "TO", "PF", "+/-", "PTS"]
    return {
        "header": {
            "competitions": [
                {
                    "status": {"period": 4, "displayClock": "0.0", "type": {"state": "post", "shortDetail": "Final"}},
                    "competitors": [competitor("home", "BOS", "2", 112), competitor("away", "NY", "18", 104)],
                }
            ]
        },
        "boxscore": {
            "players": [
                {
                    "team": {"id": "2", "abbreviation": "BOS"},
                    "statistics": [
                        {
                            "labels": labels,
                            "athletes": [
                                {
                                    "athlete": {"id": "11", "displayName": "Star Player"},
                                    "stats": ["34", "11-20", "2-6", "6-8", "2", "8", "10", "5", "2", "1", "4", "3", "+9", "30"],
                                },
                                {"athlete": {"id": "12", "displayName": "Bench Guy"}, "stats": [], "didNotPlay": True},
                            ],
                        }
                    ],
                },
                {
                    "team": {"id": "18", "abbreviation": "NY"},
                    "statistics": [
                        {
                            "labels": labels,
                            "athletes": [
                                {
                                    "athlete": {"id": "21", "displayName": "Other Guy"},
                                    "stats": ["30", "8-15", "1-4", "3-4", "1", "4", "5", "6", "1", "0", "2", "2", "-9", "20"],
                                }
                            ],
                        }
                    ],
                },
            ]
        },
        "plays": [
            {"period": {"number": 1}, "clock": {"displayValue": "12:00"}, "text": "Jump ball", "homeScore": 0, "awayScore": 0, "team": {"id": "2"}},
            {"period": {"number": 1}, "clock": {"displayValue": "11:40"}, "text": "Star Player makes layup", "homeScore": 2, "awayScore": 0, "team": {"id": "2"}},
            {"period": {"number": 1}, "clock": {"displayValue": "11:20"}, "text": "Other Guy makes three", "homeScore": 2, "awayScore": 3, "team": {"id": "18"}},
        ],
    }


def test_boxscore_translates_and_parses():
    raw = translate_boxscore(sample_summary(), "401700003")
    assert raw["game"]["gameStatus"] == 3
    players = parse_boxscore_players(raw)
    assert [p["name"] for p in players] == ["Star Player", "Other Guy"]  # the DNP is skipped
    star = players[0]
    assert (star["team"], star["opponent"], star["minutes"]) == ("BOS", "NYK", 34.0)
    assert (star["pts"], star["reb"], star["ast"], star["fgm"], star["fga"]) == (30, 10, 5, 11, 20)
    assert star["game_score"] == pytest.approx(24.4)  # same hand-computed line as test_nba_parse


def test_playbyplay_translates_into_scoring_events():
    raw = translate_playbyplay(sample_summary(), "401700003")
    assert raw["game"]["actions"][1]["teamTricode"] == "BOS"
    events = scoring_events(raw)
    assert [(e.side, e.points, e.home_score, e.away_score) for e in events] == [
        ("home", 2, 2, 0),
        ("away", 3, 2, 3),
    ]
    assert events[0].clock == 700.0


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(nba_client.asyncio, "sleep", instant)


def _espn_handler(request):
    url = str(request.url)
    if "scoreboard" in url:
        return httpx.Response(200, json=sample_scoreboard())
    if "summary" in url:
        return httpx.Response(200, json=sample_summary())
    return httpx.Response(404)


@pytest.mark.asyncio
async def test_espn_schedule_covers_the_window():
    client = EspnNbaClient(today=lambda: TODAY, transport=httpx.MockTransport(_espn_handler))
    raw = await client.schedule()
    finals = parse_final_games(raw, date(2026, 10, 28), TODAY)
    assert finals and all(g["game_id"] == "401700002" for g in finals)  # only the final game, once per day
    await client.aclose()


@pytest.mark.asyncio
async def test_auto_client_falls_back_to_espn_when_nba_refuses():
    nba_calls = []

    def nba_handler(request):
        nba_calls.append(request)
        return httpx.Response(403)

    nba = HttpNbaClient(transport=httpx.MockTransport(nba_handler))
    espn = EspnNbaClient(today=lambda: TODAY, transport=httpx.MockTransport(_espn_handler))
    auto = AutoNbaClient(nba, espn)

    board = parse_scoreboard(await auto.scoreboard())
    assert auto.source == "espn"
    assert [g["game_id"] for g in board["games"]] == ["401700001", "401700002"]
    assert len(nba_calls) == 1  # a 403 is not retried

    await auto.scoreboard()
    assert len(nba_calls) == 1  # NBA.com is left alone for a while after failing

    # Per-game calls go to the source the id belongs to.
    players = parse_boxscore_players(await auto.boxscore("401700003"))
    assert players[0]["name"] == "Star Player"
    with pytest.raises(NbaUnavailable):
        await auto.boxscore("0022600001")
    await nba.aclose()
    await espn.aclose()


@pytest.mark.asyncio
async def test_auto_client_prefers_nba_when_it_works():
    nba = HttpNbaClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"scoreboard": {"games": []}}))
    )
    espn = EspnNbaClient(today=lambda: TODAY, transport=httpx.MockTransport(_espn_handler))
    auto = AutoNbaClient(nba, espn)
    assert await auto.scoreboard() == {"scoreboard": {"games": []}}
    assert auto.source == "nba"
    await nba.aclose()
    await espn.aclose()
