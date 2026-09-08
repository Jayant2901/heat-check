# Live NBA Win-Probability & Anomaly Tracker

**Any broadcast can tell you a team is on an 8-0 run. Is that run actually unusual, or does it just feel that way?**

This is the real-time counterpart to [Father Time](https://github.com/Jayant2901/father-time-lebron), a batch NBA aging-curve anomaly detector. Both projects share the same statistical instinct — build a baseline distribution from historical data, then z-score a new observation against it — applied to two different kinds of systems:

| | Father Time | This project |
|---|---|---|
| Data | Full career histories, pulled once | A single game, updating every ~12 seconds |
| Baseline | Age-cohort percentile distributions | Run-magnitude vs. game-clock-position distributions |
| Output | A static report, regenerated occasionally | A live-updating win-probability curve with anomalies flagged in real time |
| Engineering problem | ETL, joins across messy sources, statistical rigor | State management, streaming updates, replay/live code-path parity |

## Why this exists

A portfolio project meant to demonstrate the same statistical skill applied under a very different engineering constraint: Father Time can take its time; this can't. Specifically:
- **Real-time system design** — server-side per-game state, Server-Sent Events, a poll→recompute→flag→push pipeline that has to run identically whether it's driven by a live game or a replay.
- **The same anomaly-detection instinct as Father Time**, applied to a moving target instead of a fixed dataset.
- **Honest engineering about a hard constraint**: the NBA off-season means there's no live game to test against most of the year, so the whole system is designed to be built, validated, and demoed against historical replay first — see [Replay mode](#replay-mode) below.

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
[`nba_api`](https://github.com/swar/nba_api) (the same library Father Time uses for historical stats), two ways:
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
| `GET /api/games/{game_id}/stream` | SSE stream of `wp_update` / `anomaly` / `game_end` events |
| `GET /api/replays` | Available historical replay fixtures |
| `POST /api/replays/{game_id}/start?speed=N` | Start a replay session, streamed via the same `/stream` endpoint |
| `GET /health` | Health check |

Full interactive docs at `/docs` once running.

## Status

Early scaffolding (M0) — health check, live-scoreboard passthrough, and the core data/state models are in place. See the project plan for the full build roadmap: historical dataset → win-probability model (with buzzer-convergence validation) → run-magnitude baseline → replay simulator → live polling + SSE → D3 visualization.

## Caveats

- No full play-by-play dependency for live inference — margin/time/quarter is enough, consistent with how public NBA win-probability models are actually built.
- One game at a time in v1; the state store and poller are already keyed by `game_id`, so multi-game tracking is a scope change, not a rewrite.
- `nba_api`'s live endpoint has no documented rate limit or SLA — the client is written with retry/backoff and isolated behind a `DataSource` interface so a fallback provider could be swapped in later without touching the engine, model, or SSE code.
