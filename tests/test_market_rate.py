"""Client-side rate limit in market.py: 20 calls back to back -> no 1-second window holds more than 5 requests.

    .venv/bin/python tests/test_market_rate.py
"""
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import market  # noqa: E402

STAMPS = []


def _handler(request: httpx.Request) -> httpx.Response:
    STAMPS.append(time.monotonic())
    return httpx.Response(200, json={"ok": True})


def test_twenty_calls_stay_under_five_per_second():
    STAMPS.clear()
    api = market.Market("http://mock.invalid", "sk-test")
    api.http = httpx.Client(base_url="http://mock.invalid", transport=httpx.MockTransport(_handler))
    for _ in range(20):
        api.call("GET", "/v1/state")
    worst = max(sum(1 for t in STAMPS if s <= t < s + 1.0) for s in STAMPS)
    gaps = [b - a for a, b in zip(STAMPS, STAMPS[1:])]
    print(f"20 calls in {STAMPS[-1] - STAMPS[0]:.2f}s · min gap {min(gaps):.3f}s · max per 1 s window {worst}")
    assert len(STAMPS) == 20 and worst <= 5 and min(gaps) >= market.MIN_INTERVAL - 0.005


if __name__ == "__main__":
    test_twenty_calls_stay_under_five_per_second()
    print("  ok test_twenty_calls_stay_under_five_per_second")
