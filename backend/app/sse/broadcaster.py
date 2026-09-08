"""Per-game_id pub/sub for SSE fan-out.

v1: in-process asyncio queues -- one publisher (a poller task) and any
number of subscribers (SSE connections) per game_id, all in the same
process. Scaling beyond one process means swapping this for a Redis
pub/sub-backed implementation; publish() and subscribe() are the only two
calls that would need a new backend, nothing above this module (poller.py,
api/stream.py) should need to change to make that swap.
"""

import asyncio


class Broadcaster:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, game_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(game_id, set()).add(queue)
        return queue

    def unsubscribe(self, game_id: str, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(game_id)
        if subs is None:
            return
        subs.discard(queue)
        if not subs:
            self._subscribers.pop(game_id, None)

    def publish(self, game_id: str, event) -> None:
        for queue in self._subscribers.get(game_id, ()):
            queue.put_nowait(event)


broadcaster = Broadcaster()
