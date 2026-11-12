#!/usr/bin/env python
"""Hold N SSE clients on one game and check that one poller serves them all.

    MAX_STREAMS_PER_IP=1000 HEAT_CHECK_DATA=demo uvicorn app.main:app --app-dir backend
    python scripts/load_test_sse.py --clients 100 --seconds 20 --game demo-live-2

(Without raising MAX_STREAMS_PER_IP the server correctly refuses more than 30
streams from one address.) Reports how long the first event took, how many
events each client received, whether every client saw the same ids, and how
many polls the server made: with one poller per game that number should not
grow with the number of clients -- run it with --clients 1 to compare.
"""

import argparse
import asyncio
import statistics
import time

import httpx


async def client_task(http: httpx.AsyncClient, url: str, deadline: float, result: dict) -> None:
    started = time.monotonic()
    last_id = None
    events = 0
    try:
        async with http.stream("GET", url, timeout=None) as response:
            result["status"] = response.status_code
            if response.status_code != 200:
                return
            async for line in response.aiter_lines():
                if line.startswith("id:"):
                    last_id = int(line[3:].strip())
                    events += 1
                    if "first" not in result:
                        result["first"] = time.monotonic() - started
                if time.monotonic() > deadline:
                    break
    except httpx.HTTPError as exc:
        result["error"] = type(exc).__name__
    result["events"] = events
    result["last_id"] = last_id


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--game", default="demo-live-2")
    parser.add_argument("--clients", type=int, default=100)
    parser.add_argument("--seconds", type=float, default=20)
    args = parser.parse_args()
    url = f"{args.base}/api/games/{args.game}/stream"
    limits = httpx.Limits(max_connections=args.clients + 10, max_keepalive_connections=0)
    async with httpx.AsyncClient(limits=limits) as http:
        deadline = time.monotonic() + args.seconds
        results = [{} for _ in range(args.clients)]
        t0 = time.monotonic()
        await asyncio.gather(*(client_task(http, url, deadline, r) for r in results))
        elapsed = time.monotonic() - t0
        lag = (await http.get(f"{args.base}/api/games/{args.game}/lag")).json()

    ok = [r for r in results if r.get("status") == 200 and "first" in r]
    firsts = sorted(r["first"] for r in ok)
    counts = [r["events"] for r in ok]
    last_ids = {r["last_id"] for r in ok}
    print(f"{len(ok)}/{args.clients} clients streamed for {elapsed:.1f}s; failures: {[r for r in results if r not in ok][:3]}")
    if ok:
        print(f"time to first event: median {statistics.median(firsts) * 1000:.0f} ms, max {firsts[-1] * 1000:.0f} ms")
        print(f"events per client: min {min(counts)}, median {statistics.median(counts)}, max {max(counts)}")
        spread = max(last_ids) - min(last_ids)
        print(f"last event id seen: {min(last_ids)}..{max(last_ids)} (spread {spread}; a few ids is just clients stopping at slightly different moments)")
    print(f"server polls for this game: {lag['polls']}, failures {lag['failures']}, interval {lag['interval_seconds']}s, publish {lag['last_publish_ms']} ms")


if __name__ == "__main__":
    asyncio.run(main())
