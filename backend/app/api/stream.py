"""SSE endpoint for both live games and replay sessions.

A live game_id is auto-tracked on its first /stream connection (v1 has no
separate "start tracking" call -- opening the stream is what starts the
poller), enforcing the one-live-game-at-a-time restriction. A replay
session's key only exists once POST /api/replays/{game_id}/start has
created it (app/api/replay.py), so /stream never auto-starts one of those --
it either finds the session or 404s.
"""

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from app.core.artifacts import get_baseline, get_model
from app.core.config import settings
from app.core.events import to_sse_payload
from app.core.game_state import game_state_store
from app.datasources.nba_live import NbaLiveDataSource
from app.polling.poller import poller_manager
from app.sse.broadcaster import broadcaster

router = APIRouter(prefix="/api/games", tags=["stream"])


def _backlog_events(state):
    # Deliberately a smaller field set than a live wp_update (no period/
    # clock/wp_away) -- GameState.score_history only stores what's needed to
    # redraw the worm chart's historical trail, not the extra display
    # metadata a live front-edge tick carries. wp_away is trivially 1-wp_home
    # if a client wants it. Confirmed by hand against a real replay stream
    # that this asymmetry is intentional, not a missing-field bug. Team
    # abbreviations ARE included, though -- they're constant for the whole
    # game (read once off `state`, not stored per-point), and a late-joining
    # viewer needs them immediately to render the matchup header.
    for point in state.score_history:
        yield {
            "event": "wp_update",
            "data": json.dumps(
                {
                    "t": point.elapsed_seconds,
                    "home_team": state.home_team,
                    "away_team": state.away_team,
                    "home_score": point.home_score,
                    "away_score": point.away_score,
                    "wp_home": point.wp_home,
                }
            ),
        }
    for anomaly in state.anomalies:
        yield {
            "event": "anomaly",
            "data": json.dumps(
                {
                    "t": anomaly.elapsed_seconds,
                    "team": anomaly.team,
                    "magnitude": anomaly.magnitude,
                    "duration_sec": anomaly.duration_seconds,
                    "z_score": anomaly.z_score,
                    "bucket": anomaly.bucket,
                    "message": anomaly.message,
                }
            ),
        }


def _already_in_backlog(event, last_backlog_t: float) -> bool:
    """True if `event` falls at or before the last timestamp the backlog
    snapshot already covered -- see the subscribe-before-backlog comment in
    stream_game(). Events with no `t` (game_end, degraded) are never
    considered already-delivered."""
    event_t = getattr(event, "t", None)
    return event_t is not None and event_t <= last_backlog_t


def _ensure_live_tracking(game_id: str) -> None:
    active_live = [k for k in poller_manager.active_keys() if not k.startswith("replay:")]
    if active_live and game_id not in active_live:
        raise HTTPException(
            status_code=409,
            detail=f"Already tracking {active_live[0]} live; v1 supports one live game at a time",
        )
    if not poller_manager.is_active(game_id):
        poller_manager.start(
            game_id,
            NbaLiveDataSource(),
            get_model(),
            get_baseline(),
            settings.poll_interval_seconds,
            source_game_id=game_id,
        )


@router.get("/{game_id}/stream")
async def stream_game(game_id: str, request: Request):
    is_replay = game_id.startswith("replay:")
    state = game_state_store.get(game_id)

    if state is None:
        if is_replay:
            raise HTTPException(
                status_code=404,
                detail="No such replay session -- start one via POST /api/replays/{game_id}/start",
            )
        _ensure_live_tracking(game_id)
        state = game_state_store.get_or_create(game_id)

    async def event_generator():
        # Subscribe BEFORE reading the backlog, not after -- the poller task
        # runs on its own asyncio task and can publish between "read the
        # backlog" and "start listening for new events" on any await point,
        # which used to mean a tick published in that window was silently
        # dropped for this connection (never in the backlog snapshot, never
        # seen by a queue we hadn't subscribed to yet). Subscribing first
        # means that same tick now arrives twice instead -- once already
        # captured by the backlog snapshot below, once off the queue -- so
        # the queue-drain loop skips anything at or before the last
        # timestamp the backlog already covered. Re-delivering a duplicate
        # is a much safer failure mode than silently losing an event.
        queue = broadcaster.subscribe(game_id)
        try:
            last_backlog_t = state.score_history[-1].elapsed_seconds if state.score_history else -1

            for event in _backlog_events(state):
                yield event

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=settings.sse_heartbeat_seconds)
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": ""}
                    continue
                if _already_in_backlog(event, last_backlog_t):
                    continue
                payload = to_sse_payload(event)
                yield {"event": payload.pop("type"), "data": json.dumps(payload)}
        finally:
            broadcaster.unsubscribe(game_id, queue)

    return EventSourceResponse(event_generator())
