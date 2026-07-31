"""C5 / G4 — cross-process lost-update on the shared config file. Disjoint env
writes against both replicas must all survive: the ``FileConfigManager``
read-modify-write is serialized across processes by an exclusive ``flock`` on a
sidecar lock file, so concurrent writers cannot interleave and drop keys."""

from __future__ import annotations

import asyncio

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.stack import TaiStack


async def _write_env(api: ApiClient, body: dict[str, str]) -> None:
    """Persist one env write, honouring the endpoint's documented retriable 503.

    A write on one replica fans a ``reload_config`` out to its sibling, whose
    reload gate then rejects the sibling's own concurrent write with a retriable
    ``503 reloading`` — before that write reaches the flock'd read-modify-write.
    Re-sending until it lands is the contract's intended client behaviour: the
    file write itself is flock-serialized across processes, so once the gate
    frees the key is persisted without loss (exactly what this test asserts)."""

    async def _attempt() -> bool:
        resp = await api.request_raw("POST", "/api/config/env", json=body)
        if resp.status_code == 503:  # retriable "reloading" — re-send
            return False
        if resp.status_code != 200:
            raise AssertionError(f"POST /api/config/env -> {resp.status_code}: {resp.text}")
        return True

    await wait_for_async(_attempt, deadline=15.0, message=f"env write {body} never left the reloading gate")


# This drives 40 env writes (20 concurrent A/B pairs), each a heavy local reload
# plus a reload-gate 503-retry contention against the sibling. Every write lands
# within its own 15s deadline, but the cumulative wall time is load-sensitive
# (~15s idle, ~75s under 2x CPU oversubscription) and can exceed the 120s default
# on a contended runner while still progressing to completion. The larger budget
# absorbs that load; a genuine hang would still exceed it and fail.
@pytest.mark.timeout(300)
async def test_concurrent_env_writes_lose_nothing(replicas_stack: TaiStack) -> None:
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)

    for i in range(20):
        await asyncio.gather(
            _write_env(api_a, {f"E2E_A_{i}": "1"}),
            _write_env(api_b, {f"E2E_B_{i}": "1"}),
        )

    env = (await api_a.get("/api/config/env"))["env"]
    missing = [k for i in range(20) for k in (f"E2E_A_{i}", f"E2E_B_{i}") if k not in env]
    assert not missing, f"concurrent config writes dropped keys: {missing}"
