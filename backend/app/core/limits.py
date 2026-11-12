"""Small in-process abuse limits for a public deploy.

Both are per-process and keyed by client address (uvicorn's --proxy-headers
makes that the real client behind a platform proxy). They are deliberately
simple: enough to stop one client from opening hundreds of streams or
spinning up replay sessions in a loop, not a substitute for a real gateway.
"""

import time
from collections import Counter, deque


class StreamLimiter:
    """Caps concurrent SSE connections per client and in total."""

    def __init__(self, per_client: int, total: int) -> None:
        self.per_client = per_client
        self.total_limit = total
        self._by_client: Counter[str] = Counter()
        self.total = 0

    def acquire(self, client: str) -> bool:
        if self.total >= self.total_limit or self._by_client[client] >= self.per_client:
            return False
        self._by_client[client] += 1
        self.total += 1
        return True

    def release(self, client: str) -> None:
        if self._by_client[client] > 0:
            self._by_client[client] -= 1
            self.total -= 1
            if self._by_client[client] == 0:
                del self._by_client[client]


class RateLimiter:
    """At most `limit` hits per `window` seconds per client (sliding window)."""

    MAX_CLIENTS = 10_000

    def __init__(self, limit: int, window: float = 60.0, clock=time.monotonic) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._hits: dict[str, deque] = {}

    def allow(self, client: str) -> bool:
        now = self._clock()
        hits = self._hits.setdefault(client, deque())
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        if len(self._hits) > self.MAX_CLIENTS:  # bound memory under address churn
            for key in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]:
                del self._hits[key]
        return True
