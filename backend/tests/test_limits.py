from app.core.limits import RateLimiter, StreamLimiter


def test_stream_limiter_caps_per_client_and_total():
    limiter = StreamLimiter(per_client=2, total=3)
    assert limiter.acquire("a") and limiter.acquire("a")
    assert not limiter.acquire("a")  # a is at its cap
    assert limiter.acquire("b")
    assert not limiter.acquire("c")  # process-wide cap of 3
    limiter.release("a")
    assert limiter.acquire("c")


def test_stream_limiter_release_is_safe_to_repeat():
    limiter = StreamLimiter(per_client=2, total=5)
    limiter.acquire("a")
    limiter.release("a")
    limiter.release("a")
    assert limiter.total == 0


def test_rate_limiter_is_a_sliding_window():
    now = {"t": 0.0}
    limiter = RateLimiter(limit=3, window=60, clock=lambda: now["t"])
    assert [limiter.allow("a") for _ in range(4)] == [True, True, True, False]
    assert limiter.allow("b")  # other clients are unaffected
    now["t"] = 61
    assert limiter.allow("a")


def test_rate_limiter_memory_is_bounded():
    now = {"t": 0.0}
    limiter = RateLimiter(limit=1, window=1, clock=lambda: now["t"])
    limiter.MAX_CLIENTS = 50
    for n in range(200):
        now["t"] += 2  # every earlier client's window has expired
        limiter.allow(f"client-{n}")
    assert len(limiter._hits) <= 51


class _Req:
    def __init__(self, forwarded=None, host="10.0.0.1"):
        self.headers = {"x-forwarded-for": forwarded} if forwarded else {}
        self.client = type("C", (), {"host": host})()


def test_client_id_ignores_a_spoofable_header_unless_the_proxy_is_trusted():
    from app.core.limits import client_id

    assert client_id(_Req("1.2.3.4"), trust_proxy=False) == "10.0.0.1"
    assert client_id(_Req(), trust_proxy=True) == "10.0.0.1"


def test_client_id_trusts_the_last_forwarded_entry_the_proxy_appended():
    from app.core.limits import client_id

    # the client put "6.6.6.6" first; the proxy appended the address it really saw
    assert client_id(_Req("6.6.6.6, 203.0.113.9"), trust_proxy=True) == "203.0.113.9"
    assert client_id(_Req("203.0.113.9"), trust_proxy=True) == "203.0.113.9"
