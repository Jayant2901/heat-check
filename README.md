# Heat Check

**Live NBA win-probability tracking, with a statistical heat check on every scoring run.**

Announcers call it a heat check when a player takes a contested shot just to prove he's still hot. This project runs the same check on the whole game: any broadcast can tell you a team is on an 8-0 run, but is that run actually unusual for this point in the game, or does it just feel that way?

## Why this exists

A real-time system built around one statistical idea: pool a baseline distribution from historical data, then z-score a new observation against it. Applied here to two things at once, live, as a game unfolds:
- **Win probability** — a model trained on historical score-margin/time-remaining/quarter data, recomputed every ~12 seconds as the game is polled.
- **Anomaly detection on scoring runs** — is this run's speed actually a statistical outlier for this point in the game, or does it just feel that way?

The real engineering problem here is server-side per-game state, Server-Sent Events, and a poll→recompute→flag→push pipeline that runs the same code whether it's driven by a live game or a replay (same code, not identical results: see [What the numbers do and don't mean](#what-the-numbers-do-and-dont-mean)) — which matters because of a hard constraint: the NBA off-season means there's no live game to test against most of the year, so the whole system is designed to be built, validated, and demoed against historical replay first — see [Replay mode](#replay-mode) below.

## Quickstart

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows; use .venv/bin/activate on macOS/Linux
pip install -r requirements.txt

uvicorn app.main:app --app-dir backend --reload
# -> http://127.0.0.1:8000            (frontend)
# -> http://127.0.0.1:8000/docs       (interactive API docs)
```

No network, or no games today? Run with generated sample data:

```bash
HEAT_CHECK_DATA=demo uvicorn app.main:app --app-dir backend --reload
```

Demo mode serves fictional players on real team codes, random scores and a synthetic play-by-play, so the whole UI works offline. Its two "live" games stream archived replay fixtures at 10x. Every response carries `mode: "demo"` and the page shows a "Sample data" banner.

Run the test suite:

```bash
pytest
```

## How it works

### Data source
Two ways:
- **Live**: NBA.com's public CDN (`cdn.nba.com/static/json/...`), fetched directly with `httpx` (`backend/app/nba/client.py`): today's scoreboard, the season schedule, and per-game boxscore and play-by-play. A followed game's boxscore is polled every ~12s for score, period and clock. These are CDN-served JSON snapshots, not a push feed, so polling is the right approach. `stats.nba.com` is not used (it blocks most cloud hosts).
- **Historical**: `nba_api`'s stats endpoints (`leaguegamefinder`, `playbyplayv2`/`v3`, `boxscoretraditionalv2`), pulled once per season to train the win-probability model and build the anomaly baseline.

### Architecture
Two pipelines share one core engine:
1. **Historical pipeline** (`backend/historical/`, run once/occasionally) — pulls a season of play-by-play, trains a win-probability model (`backend/app/model/`) restricted to *only* the fields available live (score margin, time remaining, quarter — never full play-by-play, to guarantee live/train feature parity), and builds a run-magnitude-vs-game-clock baseline table (`backend/app/anomaly/`) for z-scoring live runs.
2. **Live pipeline** (`backend/app/polling/`, `backend/app/datasources/nba_live.py`) — polls in-progress games, feeds each tick through the shared engine (`backend/app/core/engine.py`), and pushes win-probability and anomaly updates to connected clients over SSE (`backend/app/api/stream.py`).

### Dashboard
The home page (`frontend/`, no build step) shows, top to bottom:
- **Tonight**: today's games from the scoreboard, refreshed every 15 s while the tab is visible. Live games link to their game page.
- **Moments of the week**: the plays that moved win chance the most ("Biggest swings") and the most unusually quick unanswered runs ("Hottest runs") from the last 7 days of finished games.
- **Results**: each of the week's games with its top performer, lead changes and largest lead.
- **Best performances**: the week's top 10 single-game lines by Game Score, points/rebounds/assists leaders, and the best average Game Score over 2+ games.
- **Replay a classic game** and **How the numbers work** (filled from `/api/model-info`).

The week is built in the background (`backend/app/nba/weekly.py`) from each finished game's boxscore and play-by-play, 6 games at a time. A finished game's analysis is cached for the life of the process, and the aggregate is refreshed every 10 minutes, so after the first build a page load is instant.

A game page (`#/live/<id>` or `#/replay/<fixture>?speed=10`) shows the score, a win-chance bar, the win-probability chart in team colours, and the heat checks.

### Replay mode
A completed historical game can be stepped through the same engine code as a live game, at accelerated speed (`backend/app/datasources/replay_source.py`). This isn't a dev-only test harness — it's a permanent "watch a classic game" feature, and it's how this whole project gets built, validated, and demoed during the NBA off-season (the season is in progress from late October to June).

### API
| Endpoint | Purpose |
|---|---|
| `GET /api/scoreboard` | Today's games (live, then upcoming, then final), with `mode` and whether each is being `tracked`; 503 with a readable message if NBA.com is unreachable |
| `GET /api/dashboard/week` | `{"status": "building", "progress"}` while the week is being read, then the weekly dashboard payload |
| `GET /api/nba-check` | Diagnostics: one direct request each to the scoreboard, schedule and a boxscore on cdn.nba.com, with status code, timing and the error if any. Open it on a deploy where the dashboard says NBA.com is unreachable |
| `GET /api/model-info` | Held-out model numbers and baseline size, read from the `.meta.json` files |
| `GET /api/games/live` | Today's game IDs (kept for compatibility; empty list off-season) |
| `GET /api/games/{game_id}/state` | Non-streaming snapshot of a tracked game |
| `GET /api/games/{game_id}/stream` | SSE stream of `wp_update` / `anomaly` / `game_end` events. A live game starts being tracked on its first connection. The id must be on today's scoreboard (404 otherwise); at most `MAX_CONCURRENT_LIVE_GAMES` (default 6) are followed at once (429 beyond that); a poller with no viewers for `LIVE_IDLE_STOP_SECONDS` (default 120) stops itself |
| `GET /api/replays` | Available historical replay fixtures |
| `POST /api/replays/{game_id}/start?speed=N` | Start a replay session, streamed via the same `/stream` endpoint |
| `GET /health` | Health check |

Full interactive docs at `/docs` once running.

## Status

- **Done**: health check + live-scoreboard passthrough (M0); resumable historical fetch of game indices and play-by-play across 3 seasons, 3,941 games (M1); the training dataset and win-probability model — HistGradientBoosting, chosen over logistic regression by log-loss, 74.7% accurate across all game states of the held-out 2025-26 season, log loss 0.477 vs 0.496 for logistic regression (M2/M3; the 96.3% "buzzer-convergence" figure measured the last play of each game, where the score already decides the winner, so it is a sanity check, not an accuracy number); the run-magnitude baseline table, built from 33,761 real 6+ point runs with log-transformed durations (a right-skew correction found by testing against real games, see `app/anomaly/baseline.py`) (M4); the core engine, run detector, and anomaly z-scoring, unit-tested and validated against real games (M5); the replay simulator, proven end-to-end with the real trained artifacts (M6); live polling + SSE wired to the engine, with a live game auto-tracked on its first `/stream` connection and a replay session driven through the identical poller code path — verified against a real running server, not just unit tests (M7); the D3 worm-chart frontend — game picker, live-updating WP curve, and anomaly markers with hover tooltips — verified in a real browser against a real replay session, including a genuine flagged heat check ("home 6-0 run in 13s, 2.7-sigma") rendering correctly end-to-end (M8).
- **Added since**: the dashboard (tonight's scoreboard, weekly moments, results and best performances), several live games at once with idle pollers stopping themselves, stream ids validated against today's scoreboard, demo data mode, a calmer frontend with D3 and the font vendored (no CDN at runtime), and parser tests for the CDN payloads.
- **Not started**: deploy + SSE verification on Render (M9).

## What the numbers do and don't mean

- **Win chance accuracy.** On the held-out 2025-26 season the model's favourite won 74.7% of the time across all 665,170 game states, with log loss 0.477 against 0.496 for logistic regression (`data/artifacts/win_prob_model_v1.meta.json`, served by `/api/model-info`). Earlier copy said "96% accuracy"; that was the buzzer-convergence check on the final play of each game and has been removed, as has a "12 s to your screen" tile, which was a config value, not a measurement.
- **No team strength.** The model sees only score margin, time remaining and quarter. Every game starts near 50%, whoever is playing.
- **Replay and live run the same code but don't see the same thing.** Replay fixtures tick every ~3 s of game time; live polling sees one boxscore every 12 s. Run detection works on score changes between ticks, so coarser sampling merges baskets and changes measured run lengths. On fixture `0022200001` the engine flags 0 runs at replay cadence and 1 when every 4th snapshot is used; `test_sampling_rate_changes_what_gets_flagged` pins that.
- **The heat-check threshold is a tuning, not an evaluation.** `ANOMALY_Z_THRESHOLD=1.5` was chosen by looking at three replay games so the comeback game flags. Runs between 1.5 and 2.5 are now described as "quicker than usual for this point in the game" rather than "rare". It should not be changed without evaluating many games at live cadence.
- **The run baseline overlaps the model's test season.** It was built from 2023-24, 2024-25 and 2025-26; 2025-26 is the season the win-probability model was tested on.
- **Big runs have little history.** The baseline holds very few runs of 18+ points (some buckets have one run and zero spread). The weekly "Hottest runs" score a long run on its longest opening stretch that has at least 50 historical runs behind it; the live engine flags a run when it first crosses the threshold, usually well below that size.
- **Swings** measure a single play: win chance just before vs just after it, at the same clock.
- **Parser verification.** The CDN field names were checked against the sample payloads that ship with `nba_api` 1.11.4 (`nba_api/live/nba/endpoints/*.py`) on 2026-11-04, and `minutesCalculated` values like `PT25M` are handled. They have **not yet** been checked against a live response: the environment this was built in could not reach `cdn.nba.com`. `backend/tests/test_nba_parse.py::test_real_saved_boxscore_parses` runs once a real boxscore is saved to `backend/tests/fixtures/boxscore_real.json`.

## Hardening

A few real correctness issues turned up in review, fixed rather than left as known caveats:
- **SSE backlog/subscribe race** (`app/api/stream.py`) — a tick published by the poller between reading the backlog and subscribing to the live queue used to be silently dropped for that connection. Fixed by subscribing first and having the queue-drain loop skip anything the backlog snapshot already covered (`_already_in_backlog`) — a duplicate is a much safer failure mode than a gap.
- **Duplicate points on SSE reconnect** (`frontend/src/worm-chart.js`) — `EventSource`'s default auto-reconnect re-runs the backlog from t=0, which used to redraw the worm chart's line from scratch on top of what was already there. `addPoint` now rejects strictly-earlier points, and a point with the same `t` as the last one replaces it instead of adding a duplicate.
- **Unbounded `GameStateStore` growth** (`app/polling/poller.py`) — nothing ever evicted a finished game's state; every game_end or completed replay leaked its full `score_history`/`anomalies` for the life of the process. The poller now removes its own entry when it stops.
- **Unbounded concurrent replay sessions** (`app/api/replay.py`) — `POST /api/replays/{game_id}/start` had no cap, so repeated calls could spin up arbitrarily many poller tasks. Capped at `MAX_CONCURRENT_REPLAY_SESSIONS` (default 10; 429 past that).
- **CI** — `.github/workflows/test.yml` runs the test suite on push/PR; previously it only proved anything locally.
- **SSE reconnect UX** — the frontend distinguishes a transient auto-retry (`EventSource.readyState === CONNECTING`, shown as "Reconnecting…") from a truly dead stream, so a drop reads as handled rather than broken. Still open: whether Render's free tier holds a long-lived SSE connection at all — that needs an actual deploy to verify (see M9 below).

## Caveats

- No full play-by-play dependency for live inference — margin/time/quarter is enough, consistent with how public NBA win-probability models are actually built.
- Up to `MAX_CONCURRENT_LIVE_GAMES` live games are followed at once (default 6), each costing one boxscore request every 12 s.
- NBA.com's CDN has no documented rate limit or SLA — the client is written with retry/backoff and isolated behind a `DataSource` interface so a fallback provider could be swapped in later without touching the engine, model, or SSE code.
- The in-memory single-process `GameStateStore`/`Broadcaster`, and the pandas-filter-per-lookup in `app/anomaly/baseline.py`, are deliberate v1 scope choices with a stated migration path (Redis-backed store, etc.) — not gaps to close before M9.
