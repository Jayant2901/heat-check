"""SSE endpoint for both live games and replay sessions.

A live game_id is auto-tracked on its first /stream connection (there is no
separate "start tracking" call -- opening the stream is what starts the
poller). The id must be one of today's games on the scoreboard (404
otherwise) unless the server still holds that game's state from earlier, and
at most MAX_CONCURRENT_LIVE_GAMES live pollers run at once (429 beyond
that). A live poller stops itself after LIVE_IDLE_STOP_SECONDS with no
viewers. A replay session's key only exists once
POST /api/replays/{game_id}/start has created it (app/api/replay.py), so
/stream never auto-starts one of those -- it either finds the session or 404s.

Backlog and live events never overlap or leave a gap. Every wp_update /
anomaly / game_end gets a sequence number (its SSE `id`) and is appended to
the game's event log (core/game_state.py). A connection subscribes to the
live queue and copies the log in one step with no await between them, so
the backlog is exactly the events up to `last_seq` and the queue holds
exactly what comes after. A browser that reconnects sends Last-Event-ID
and is sent only what it missed.
"""

import asyncio
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse, ServerSentEvent
from starlette.background import BackgroundTask

from app.core.artifacts import get_baseline, get_model
from app.core.config import settings
from app.core.events import to_sse_payload
from app.core.game_state import game_state_store
from app.core.limits import StreamLimiter
from app.nba.client import get_nba_client
from app.nba.parse import parse_scoreboard
from app.polling.poller import SourceUnavailable, poller_manager
from app.sse.broadcaster import DROPPED, broadcaster

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/games", tags=["stream"])

stream_limiter = StreamLimiter(settings.max_streams_per_ip, settings.max_total_streams)

# Not buffered, not cached, not compressed: every event must reach the browser
# as it is written. (main.py also keeps gzip away from these routes.)
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def event_to_sse(event, backlog: bool = False) -> dict:
    """An event as an SSE message. `backlog` marks history replayed to a new
    or reconnecting viewer, so the page can draw it quietly (no toasts) and
    never mistake it for a play that just happened."""
    payload = to_sse_payload(event)
    kind = payload.pop("type")
    payload["backlog"] = backlog
    message = {"event": kind, "data": json.dumps(payload)}
    if payload.get("seq"):
        message["id"] = str(payload["seq"])
    return message


def backlog_for(state, last_event_id: str | None) -> tuple[list, int]:
    """(events to replay, seq of the last one covered), copied atomically.

    With a Last-Event-ID only later events are returned. An id from the
    future (the server restarted and rebuilt the game) is ignored and the
    whole log is sent; the page drops points it already has.
    """
    last_seq = state.seq
    log = list(state.log)
    try:
        after = int(last_event_id) if last_event_id else 0
    except ValueError:
        after = 0
    if 0 < after <= last_seq:
        log = [e for e in log if e.seq > after]
    return log, last_seq


async def _ensure_live_tracking(game_id: str):
    """Make sure a live game is being tracked (or is cached as finished) and
    return its state."""
    if poller_manager.is_active(game_id):
        return game_state_store.get(game_id)
    cached = game_state_store.get(game_id)
    if cached is not None and cached.status == "final":
        return cached  # finished earlier; its full chart and heat checks are kept

    client = get_nba_client()
    try:
        board = parse_scoreboard(await client.scoreboard())
    except Exception:
        raise HTTPException(status_code=503, detail="Live NBA data is unreachable right now")
    game = next((g for g in board["games"] if g["game_id"] == game_id), None)
    if game is None:
        raise HTTPException(status_code=404, detail="Not one of today's games")

    active_live = [k for k in poller_manager.active_keys() if not k.startswith("replay:")]
    if len(active_live) >= settings.max_concurrent_live_games:
        raise HTTPException(
            status_code=429,
            detail=f"Already following {len(active_live)} live games (max {settings.max_concurrent_live_games}); "
            "try again shortly",
        )
    source, interval = client.live_source(game_id, (game["home"]["tricode"], game["away"]["tricode"]))
    try:
        await poller_manager.start_primed(
            game_id,
            source,
            get_model(),
            get_baseline(),
            interval,
            source_game_id=game_id,
            idle_stop_seconds=settings.live_idle_stop_seconds,
        )
    except SourceUnavailable:
        raise HTTPException(status_code=503, detail="Couldn't read this game's live feed right now. Try again shortly.")
    return game_state_store.get(game_id)


def _client_id(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/{game_id}/stream")
async def stream_game(game_id: str, request: Request):
    is_replay = game_id.startswith("replay:")
    if is_replay:
        state = game_state_store.get(game_id)
        if state is None:
            raise HTTPException(
                status_code=404,
                detail="No such replay session -- start one via POST /api/replays/{game_id}/start",
            )
    else:
        state = await _ensure_live_tracking(game_id)
        if state is None:  # the poller ended and was evicted in the same moment
            raise HTTPException(status_code=503, detail="Couldn't read this game right now. Try again shortly.")

    client = _client_id(request)
    if not stream_limiter.acquire(client):
        raise HTTPException(status_code=429, detail="Too many open streams. Close another tab and try again.")
    last_event_id = request.headers.get("last-event-id")
    released = False

    def release() -> None:
        # Called when the generator ends and again as the response's
        # background task (in case the client left before the generator
        # ever started); only the first call counts.
        nonlocal released
        if not released:
            released = True
            stream_limiter.release(client)

    async def event_generator():
        try:
            # Subscribe and copy the log with no await between, so nothing is
            # in both and nothing falls between (see the module docstring).
            queue = broadcaster.subscribe(game_id)
            try:
                backlog, last_seq = backlog_for(state, last_event_id)
                yield ServerSentEvent(data=None, retry=settings.sse_retry_ms)
                for event in backlog:
                    yield event_to_sse(event, backlog=True)
                if state.status == "final" and any(e.type == "game_end" for e in state.log):
                    return  # a finished game: game_end has been delivered (or the client already has it)

                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=settings.sse_heartbeat_seconds)
                    except asyncio.TimeoutError:
                        yield {"event": "heartbeat", "data": json.dumps({"ts": time.time()})}
                        continue
                    if event is DROPPED:
                        break  # too slow to keep up; the browser reconnects and catches up by id
                    if event.seq and event.seq <= last_seq:
                        continue
                    yield event_to_sse(event)
                    if event.type == "game_end":
                        break
            finally:
                broadcaster.unsubscribe(game_id, queue)
        finally:
            release()

    return EventSourceResponse(event_generator(), headers=SSE_HEADERS, background=BackgroundTask(release))
