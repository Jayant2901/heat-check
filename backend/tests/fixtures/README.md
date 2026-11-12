# Test fixtures

`espn_*.json` are real responses from ESPN's public site API
(`site.api.espn.com/apis/site/v2/sports/basketball/nba/...`), captured on
2026-11-12. They are the real JSON with unused sections removed to keep the
repo small (scoreboards: highlights/headlines/broadcast blocks and all but
2-3 events; summary: everything except `header.competitions`,
`boxscore.players` and `plays`, with each play cut to the fields the parser
reads). Nothing was edited or invented.

* `espn_scoreboard_final.json` -- three finished games (event ids 401909966-68).
* `espn_scoreboard_pre.json` -- two games not yet started.
* `espn_summary_401909968.json` -- the finished BOS @ TOR game's summary:
  boxscore players and all 495 plays.

There is **no** real `cdn.nba.com` payload here: that host answers 403 to the
environment this was written in, so `boxscore_real.json` (what
`test_nba_parse.py::test_real_saved_boxscore_parses` waits for) and a real NBA
play-by-play are still missing. The NBA play-by-play tests use payloads built
in the shape of the sample that ships in
`nba_api/live/nba/endpoints/playbyplay.py` and say so.
