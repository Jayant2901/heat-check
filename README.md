# Heat Check

**Live NBA win-probability tracking, with a statistical heat check on every scoring run.**

Announcers call it a heat check when a player takes a contested shot just to prove he's still hot. This project runs the same check on the whole game: any broadcast can tell you a team is on an 8-0 run, but is that run actually unusual for this point in the game, or does it just feel that way?

## Why this exists

A real-time system built around one statistical idea: pool a baseline distribution from historical data, then z-score a new observation against it. Applied here to two things at once, live, as a game unfolds:
- **Win probability** — a model trained on historical score-margin/time-remaining/quarter data, recomputed every ~12 seconds as the game is polled.
- **Anomaly detection on scoring runs** — is this run's speed actually a statistical outlier for this point in the game, or does it just feel that way?

The real engineering problem here is server-side per-game state, Server-Sent Events, and a poll→recompute→flag→push pipeline that has to run **identically** whether it's driven by a live game or a replay — which matters because of a hard constraint: the NBA off-season means there's no live game to test against most of the year, so the whole system is designed to be built, validated, and demoed against historical replay first — see [Replay mode](#replay-mode) below.

## Quickstart

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows; use .venv/bin/activate on macOS/Linux
pip install -r requirements.txt

uvicorn app.main:app --app-dir backend --reload
# -> http://127.0.0.1:8000            (frontend)
# -> http://127.0.0.1:8000/docs       (interactive API docs)
```

Run the test suite:

```bash
pytest
```

## How it works

### Data source
[`nba_api`](https://github.com/swar/nba_api), two ways:
- **Live**: `nba_api.live.nba.endpoints.boxscore`, backed by NBA.com's CDN (`cdn.nba.com/static/json/liveData/boxscore/...`), polled every ~12s for score, period, and game clock. This is a CDN-served JSON snapshot, not a push feed — polling is the correct approach here, not a compromise.
- **Historical**: `nba_api`'s stats endpoints (`leaguegamefinder`, `playbyplayv2`/`v3`, `boxscoretraditionalv2`), pulled once per season to train the win-probability model and build the anomaly baseline.

### Architecture
Two pipelines share one core engine:
1. **Historical pipeline** (`backend/historical/`, run once/occasionally) — pulls a season of play-by-play, trains a win-probability model (`backend/app/model/`) restricted to *only* the fields available live (score margin, time remaining, quarter — never full play-by-play, to guarantee live/train feature parity), and builds a run-magnitude-vs-game-clock baseline table (`backend/app/anomaly/`) for z-scoring live runs.
2. **Live pipeline** (`backend/app/polling/`, `backend/app/datasources/nba_live.py`) — polls in-progress games, feeds each tick through the shared engine (`backend/app/core/engine.py`), and pushes win-probability and anomaly updates to connected clients over SSE (`backend/app/api/stream.py`).

### Replay mode
A completed historical game can be stepped through the *exact same* engine code as a live game, at accelerated speed (`backend/app/datasources/replay_source.py`). This isn't a dev-only test harness — it's a permanent "watch a classic game" feature, and it's how this whole project gets built, validated, and demoed during the NBA off-season (next season starts 2026-10-20).

### API
| Endpoint | Purpose |
|---|---|
| `GET /api/games/live` | Today's live game IDs (empty list off-season — that's correct, not broken) |
| `GET /api/games/{game_id}/state` | Non-streaming snapshot of a tracked game |
| `GET /api/games/{game_id}/stream` | SSE stream of `wp_update` / `anomaly` / `game_end` events; a live game_id starts being tracked on its first connection here -- there's no separate "start tracking" call |
| `GET /api/replays` | Available historical replay fixtures |
| `POST /api/replays/{game_id}/start?speed=N` | Start a replay session, streamed via the same `/stream` endpoint |
| `GET /health` | Health check |

Full interactive docs at `/docs` once running.

## Status

- **Done**: health check + live-scoreboard passthrough (M0); resumable historical fetch of game indices and play-by-play across 3 seasons, 3,941 games (M1); the training dataset and win-probability model — HistGradientBoosting, chosen over logistic regression by log-loss, passing the buzzer-convergence gate at 96.3% on strictly held-out data (M2/M3); the run-magnitude baseline table, built from 33,761 real 6+ point runs with log-transformed durations (a right-skew correction found by testing against real games, see `app/anomaly/baseline.py`) (M4); the core engine, run detector, and anomaly z-scoring, unit-tested and validated against real games (M5); the replay simulator, proven end-to-end with the real trained artifacts (M6); live polling + SSE wired to the engine, with a live game auto-tracked on its first `/stream` connection and a replay session driven through the identical poller code path — verified against a real running server, not just unit tests (M7); the D3 worm-chart frontend — game picker, live-updating WP curve, and anomaly markers with hover tooltips — verified in a real browser against a real replay session, including a genuine flagged heat check ("home 6-0 run in 13s, 2.7-sigma") rendering correctly end-to-end (M8).
- **Not started**: deploy + SSE verification on Render (M9).

## Hardening

A few real correctness issues turned up in review, fixed rather than left as known caveats:
- **SSE backlog/subscribe race** (`app/api/stream.py`) — a tick published by the poller between reading the backlog and subscribing to the live queue used to be silently dropped for that connection. Fixed by subscribing first and having the queue-drain loop skip anything the backlog snapshot already covered (`_already_in_backlog`) — a duplicate is a much safer failure mode than a gap.
- **Duplicate points on SSE reconnect** (`frontend/src/worm-chart.js`) — `EventSource`'s default auto-reconnect re-runs the backlog from t=0, which used to redraw the worm chart's line from scratch on top of what was already there. `addPoint` now rejects strictly-earlier points (same-t distinct events, e.g. back-to-back free throws, still plot fine).
- **Unbounded `GameStateStore` growth** (`app/polling/poller.py`) — nothing ever evicted a finished game's state; every game_end or completed replay leaked its full `score_history`/`anomalies` for the life of the process. The poller now removes its own entry when it stops.
- **Unbounded concurrent replay sessions** (`app/api/replay.py`) — `POST /api/replays/{game_id}/start` had no cap, so repeated calls could spin up arbitrarily many poller tasks. Capped at `MAX_CONCURRENT_REPLAY_SESSIONS` (default 10; 429 past that).
- **CI** — `.github/workflows/test.yml` runs the test suite on push/PR; previously it only proved anything locally.
- **SSE reconnect UX** — the frontend distinguishes a transient auto-retry (`EventSource.readyState === CONNECTING`, shown as "Reconnecting…") from a truly dead stream, so a drop reads as handled rather than broken. Still open: whether Render's free tier holds a long-lived SSE connection at all — that needs an actual deploy to verify (see M9 below).

## Caveats

- No full play-by-play dependency for live inference — margin/time/quarter is enough, consistent with how public NBA win-probability models are actually built.
- One game at a time in v1; the state store and poller are already keyed by `game_id`, so multi-game tracking is a scope change, not a rewrite.
- `nba_api`'s live endpoint has no documented rate limit or SLA — the client is written with retry/backoff and isolated behind a `DataSource` interface so a fallback provider could be swapped in later without touching the engine, model, or SSE code.
- The in-memory single-process `GameStateStore`/`Broadcaster`, and the pandas-filter-per-lookup in `app/anomaly/baseline.py`, are deliberate v1 scope choices with a stated migration path (Redis-backed store, etc.) — not gaps to close before M9.
