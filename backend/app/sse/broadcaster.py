"""Per-game_id pub/sub for SSE fan-out.

v1: in-process asyncio queues -- one publisher (a poller task) and any
number of subscribers (SSE connections) per game_id, all in the same
process. Scaling beyond one process means swapping this for a Redis
pub/sub-backed implementation; publish() and subscribe() are the only two
calls that would need a new backend, nothing above this module (poller.py,
api/stream.py) should need to change to make that swap.

publish() hands the event to every subscriber's queue immediately (no
batching, no timer), so delivery adds no delay of its own. Queues are
bounded: a client that stops reading is dropped (it gets DROPPED) instead of
growing memory for the life of the process. A dropped browser reconnects and
catches up from the game's event log using Last-Event-ID.
"""

import asyncio

QUEUE_LIMIT = 1000


class _Dropped:
    def __repr__(self) -> str:
        return "DROPPED"


# Put on a subscriber's queue when it fell too far behind and was removed.
DROPPED = _Dropped()


class Broadcaster:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, game_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._subscribers.setdefault(game_id, set()).add(queue)
        return queue

    def unsubscribe(self, game_id: str, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(game_id)
        if subs is None:
            return
        subs.discard(queue)
        if not subs:
            self._subscribers.pop(game_id, None)

    def subscriber_count(self, game_id: str) -> int:
        return len(self._subscribers.get(game_id, ()))

    def publish(self, game_id: str, event) -> None:
        for queue in list(self._subscribers.get(game_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self.unsubscribe(game_id, queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(DROPPED)


broadcaster = Broadcaster()
