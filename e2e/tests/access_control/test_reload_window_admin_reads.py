"""An administrator's reads stay authorized through a fleet config reload.

An env write reloads the worker it lands on and broadcasts the reload to the rest of the
fleet. While a peer replica rebuilds its serving epoch from that broadcast, an
administrator's read of the env store on that peer is either served (``200``) or refused
as retriable while the reload holds the gate (``503``); it is never a ``403``, which would
tell the administrator they lack a permission they hold.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs("kind:identity")

# The env writes driven across the window; each one reloads the fleet once.
_RELOAD_ROUNDS = 3


@pytest.mark.needs("mutable", "topology:replicas", "setting:seeded-access-control")
async def test_admin_env_reads_on_a_reloading_peer_are_never_forbidden(
    auth_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    stack = auth_stack
    writer = stack.api(port=stack.port_a)
    reader = stack.api(port=stack.port_b)
    statuses: list[int] = []
    bodies: list[str] = []
    writes_done = asyncio.Event()

    async def read_continuously() -> None:
        while not writes_done.is_set():
            response = await reader.request_raw("GET", "/api/config/env")
            statuses.append(response.status_code)
            if response.status_code not in (200, 503):
                bodies.append(f"{response.status_code} {response.text}")

    async def write_rounds() -> None:
        try:
            for round_index in range(_RELOAD_ROUNDS):
                key = uniq(f"E2E_RELOAD_WINDOW_{round_index}").upper().replace("-", "_")
                await writer.post("/api/config/env", json={"env": {key: "1"}}, retry_on_reloading=True)
        finally:
            writes_done.set()

    await asyncio.gather(read_continuously(), write_rounds())

    assert statuses, "no read ran across the reload window"
    assert not bodies, f"an administrator's env read was refused across the reload window: {bodies}"
