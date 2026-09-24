"""Cross-process lost-update on the shared config file. Disjoint env
writes against both replicas must all survive: the ``FileConfigManager``
read-modify-write is serialized across processes by an exclusive ``flock`` on a
sidecar lock file, so concurrent writers cannot interleave and drop keys."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

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


# The number of concurrent A/B write rounds. What the assertion needs is (a) both
# replicas writing disjoint keys concurrently, so the cross-process flock arbitrates a
# real read-modify-write race, and (b) more than one round, so a later round's
# read-modify-write must preserve the keys earlier rounds persisted ACROSS the fleet
# reload every write triggers. The endpoint's reload gate serialises concurrent writers
# (a sibling write meets a retriable ``503 reloading`` while the other's reload runs), so
# each write is a heavy reload and extra rounds buy reload wall-time, not contention
# coverage. Three is the smallest count that exercises both facets with a one-round margin
# for the gate's serialisation not to erase the concurrency in a single round.
_LOSE_NOTHING_ROUNDS = 3


@pytest.mark.timeout(300)
async def test_concurrent_env_writes_lose_nothing(replicas_stack: TaiStack) -> None:
    """Concurrent disjoint env writes against both replicas all survive: the flock'd
    read-modify-write of ``FileConfigManager.write_env`` serialises cross-process writers,
    so no round's keys are dropped and a later round's write preserves the earlier rounds'
    keys across the fleet reload each write fans out. The 300s ceiling is a genuine-hang
    backstop, not a throughput budget — the rounds are held to the few the invariant needs
    (:data:`_LOSE_NOTHING_ROUNDS`)."""
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)

    for i in range(_LOSE_NOTHING_ROUNDS):
        await asyncio.gather(
            _write_env(api_a, {f"E2E_A_{i}": "1"}),
            _write_env(api_b, {f"E2E_B_{i}": "1"}),
        )

    env = (await api_a.get("/api/config/env"))["env"]
    missing = [k for i in range(_LOSE_NOTHING_ROUNDS) for k in (f"E2E_A_{i}", f"E2E_B_{i}") if k not in env]
    assert not missing, f"concurrent config writes dropped keys: {missing}"


# NOTE on REPLACE vs the merge race above: ``FileConfigManager.replace_env`` (the profile
# apply's env write) is a WHOLE-MAP blind write under the SAME sidecar flock as ``write_env``
# — there is no read-modify step, so two concurrent replaces are last-writer-wins by design
# (the flock guarantees each write lands atomically, never a torn interleave). The
# "no key silently dropped" lost-update property is ``write_env``'s MERGE contract, proven
# above; a whole-map replace legitimately drops the keys the new map omits. What the profile
# apply adds over ``write_env`` is exactly that delete-on-omission, asserted here on the same
# flock'd file manager.
@pytest.mark.timeout(300)
async def test_replace_env_is_whole_map_and_deletes_omitted_keys(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = replicas_stack.api(port=replicas_stack.port_a)

    # Seed one plain HOT marker key, then read the FULL stored env back (GET returns real
    # values). Building the profile as "current stored env MINUS the marker" keeps every
    # deployment-pinned / recycle-class stored key (e.g. TAI_BUS_REDIS_URL) at its current
    # value — a zero diff on them, so a BARE stack does not refuse the apply — while the
    # whole-map replace deletes ONLY the omitted hot marker.
    marker = uniq("E2E_REPL_MARKER").upper()
    await _write_env(api, {marker: "seeded"})
    baseline = (await api.get("/api/config/env"))["env"]
    assert marker in baseline, baseline

    profile_env = {key: value for key, value in baseline.items() if key != marker}
    name = uniq("replace")
    await api.put(f"/api/config/profiles/{name}", json={"env": profile_env}, retry_on_reloading=True)
    await api.post(f"/api/config/profiles/{name}/apply", retry_on_reloading=True)

    # Whole-map replace under the flock: the store reads back as EXACTLY the profile's env —
    # the omitted hot marker deleted, every preserved key intact.
    stored = (await api.get("/api/config/env"))["env"]
    assert marker not in stored, f"whole-map replace did not delete the omitted key: {stored}"
    assert stored == profile_env, f"replace_env did not write the whole profile map atomically: {stored}"
