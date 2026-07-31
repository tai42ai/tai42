"""C6 — the fixed-window rate limiter is GLOBAL across workers, not per-worker.
Ten requests alternating across two replicas fill one 10-second burst window;
the eleventh is refused on either replica. A per-worker counter would allow ~2L."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import align_to_window


async def test_rate_limit_is_global_not_per_worker(auth_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    topic = uniq("topic").replace("_", "-")
    api_a = auth_stack.api(port=auth_stack.port_a)
    api_b = auth_stack.api(port=auth_stack.port_b)
    path = f"/universal_webhook/{topic}"

    # Start just after a burst-window boundary so the 10+1 cannot straddle.
    align_to_window(10.0)

    for i in range(10):
        client = api_a if i % 2 == 0 else api_b
        response = await client.request_raw("POST", path, json={})
        assert response.status_code != 429, f"request {i} was rate-limited early: {response.text}"

    over = await api_a.request_raw("POST", path, json={})
    assert over.status_code == 429, f"the 11th request was not refused (got {over.status_code})"
