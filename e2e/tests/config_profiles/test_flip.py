"""F2 — the settings-profile HOT flip, end to end over ONE stack boot.

A profile apply is the C5 env-write-LAST reload: it builds a FRESH serving epoch under
the proposed env, swaps it in atomically (``epoch.build_and_swap_epoch``), and broadcasts
the reload to the whole fleet. This test certifies the flip CONVERGED for a hot-class
registered settings group the leg's manifest loads — the kit ``LoggingSettings`` group
(env ``TAI_LOG_LEVEL``, reload_class ``hot``, present on every leg) — on the REPLICAS
stack so the census dimension is real (≥2 HTTP workers + the backend worker on the bus):

* the group's field resolves to the NEW effective value (the reset cache re-read the env);
* ZERO stale settings holders (no retired-epoch settings singleton is still held —
  the sweep, surfaced by the probe);
* ZERO stale-epoch client pools (the retire drained + detached every retired pool);
* the settings epoch advanced past the pre-apply epoch;
* MULTI-WORKER census: EVERY live census worker (both HTTP replicas + the backend worker)
  confirmed the reload ``applied`` — the fan-out names them, none non-applied.

Budgeted at 300s per the measured reload cost (the ``test_file_config_race`` precedent);
a hot apply is one ``build_and_swap`` per leg, riding the existing CI axis matrix — NO
per-plugin stack boots. The ``replicas_stack`` carries the skeleton component store (via
``_base_env``'s default database), so the profile documents persist there.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e.stack import TaiStack

# The representative hot field: kit ``LoggingSettings.log_level`` reads this var, is
# ``hot``, and validates to a real logging level — so flipping between two valid levels
# is a benign, guaranteed-present hot change on every leg.
_LEVEL_VAR = "TAI_LOG_LEVEL"


async def _snapshot(stack: TaiStack) -> dict[str, Any]:
    """The ``e2e_settings_snapshot`` probe's return from replica A, read over a FRESH MCP
    session so a just-swapped epoch's tools are the ones observed."""
    async with stack.mcp(port=stack.port_a) as mcp:
        result = await mcp.call_tool("e2e_settings_snapshot", {}, retry_on_reloading=True)
    data = result.data if isinstance(result.data, dict) else result.structured_content
    assert isinstance(data, dict), f"e2e_settings_snapshot returned no result map: {result!r}"
    return data


def _level_field(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The ``TAI_LOG_LEVEL`` field row from the probe's registered-groups walk — proof
    the leg actually loads a hot group carrying it."""
    for group in snapshot["groups"]:
        for field in group["fields"]:
            if field["env_var"] == _LEVEL_VAR:
                assert field["reload_class"] == "hot", f"{_LEVEL_VAR} is not hot-class: {field}"
                return field
    raise AssertionError(
        f"no registered settings group exposes {_LEVEL_VAR}: {[g['name'] for g in snapshot['groups']]}"
    )


@pytest.mark.timeout(300)
async def test_hot_profile_flip_converges_and_fans_out(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api = replicas_stack.api(port=replicas_stack.port_a)

    # Pre-apply posture: the current effective level and the epoch to advance past.
    before = await _snapshot(replicas_stack)
    current_level = (_level_field(before)["value"] or "INFO").upper()
    new_level = "DEBUG" if current_level != "DEBUG" else "INFO"
    base_epoch = before["settings_epoch"]

    # A profile whose env is the CURRENT stored env plus the flipped level. A profile apply
    # REPLACES the whole stored env band, so carrying the stored keys keeps every other
    # setting unchanged; the single diff key is the hot level.
    stored = (await api.get("/api/config/env"))["env"]
    profile_env = {**stored, _LEVEL_VAR: new_level}
    name = uniq("flip")
    await api.put(f"/api/config/profiles/{name}", json={"env": profile_env}, retry_on_reloading=True)

    # Apply: build + swap a fresh epoch under the proposed env, broadcast the reload. The
    # dedicated ``profileApplyResponse`` names the hot diff key and refuses nothing.
    applied = await api.post(f"/api/config/profiles/{name}/apply", retry_on_reloading=True)
    assert _LEVEL_VAR in applied["hot"], f"the flipped level was not classified hot: {applied}"
    assert applied["refused"] == [], f"a clean hot apply refused a key: {applied}"

    # MULTI-WORKER census: the fan-out is a real fleet report (≥2 HTTP replicas + backend on
    # the bus), and EVERY live census worker confirmed the reload ``applied`` — none silently
    # dropped. Mirrors the reload-fanout contract (``reload/test_fanout``).
    fanout = applied["fanout"]
    assert fanout.get("mode") == "fleet", f"the profile apply did not fan out to the fleet: {fanout}"
    outcomes = {row["name"]: row["outcome"] for row in fanout["results"]}
    census = {worker.name for worker in replicas_stack.census()}
    assert census <= outcomes.keys(), (
        f"a census worker was absent from the apply fan-out: census={census} fanout={fanout}"
    )
    assert all(outcomes[name] == "applied" for name in census), f"a worker did not apply the reload: {fanout}"

    # Post-apply posture on replica A, off a fresh epoch.
    after = await _snapshot(replicas_stack)
    assert _level_field(after)["value"] == new_level, f"the singleton did not re-read the flipped level: {after}"
    assert after["settings_epoch"] > base_epoch, f"the settings epoch did not advance: {before} -> {after}"
    # No retired-epoch settings LEAK from the flip. Two holders are expected baseline, NOT
    # leaks: the app-lifetime ``WorkerBus`` intentionally pins its epoch-0 ``BusSettings``
    # across every reload (an epoch-immune subscription), and a swept entry with no
    # live holder is benign roster residue. A real leak is any OTHER retired-epoch singleton
    # still held — in particular the flipped ``LoggingSettings`` must have been released.
    leaked = [
        holder
        for holder in after["stale_holders"]
        if holder["holders"] and holder["holders"] != ["tai42_skeleton.app.bus.WorkerBus"]
    ]
    assert not leaked, f"the flip leaked a retired-epoch settings singleton: {leaked}"
    assert after["stale_pool_epochs"] == [], (
        f"a retired client-pool epoch was not drained: {after['stale_pool_epochs']}"
    )
