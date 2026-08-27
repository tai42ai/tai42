"""Declared preset seeds over the REAL versioned store on a running stack: a boot makes
a seed a LIVE callable preset in the SAME epoch, a re-boot is idempotent, a content
change upgrades the tagged version, an operator-edited (untagged-active) seed survives a
re-boot untouched, and a store-OFF profile skips visibly while booting healthy.

The seed-lifecycle legs each boot their OWN store-backed / store-off stack through
``fresh_stack`` so a leg owns its seed's global version history. Reloads are driven
through the env-write door (``POST /api/config/env``); an empty body re-applies the
current seed, a variant flip ships the drifted body."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tai42_e2e.manifests import build_seams_seed_off_stack, build_seams_seed_stack
from tai42_e2e.stack import TaiStack

_SEED = "e2e_seed_probe"
_SHIPPED_DEFAULT_TAG = "shipped-default"


async def _active_body(stack: TaiStack) -> dict:
    return await stack.api().get(f"/api/presets/{_SEED}", retry_on_reloading=True)


async def _versions(stack: TaiStack) -> list[dict]:
    return await stack.api().get(f"/api/presets/{_SEED}/versions", retry_on_reloading=True)


async def _reload_env(stack: TaiStack, env: dict[str, str]) -> None:
    """Drive a reload through the env-write door — an empty ``env`` re-applies the seed,
    a variant flip ships a drifted body — then wait past the reload gate on a probe."""
    await stack.api().post("/api/config/env", json=env, retry_on_reloading=True)
    async with stack.mcp() as mcp:
        await mcp.call_tool("e2e_worker_info", retry_on_reloading=True)


async def test_seed_is_live_callable_in_first_boot_epoch(
    fresh_stack: Callable[..., TaiStack],
) -> None:
    stack = fresh_stack(build_seams_seed_stack)
    # FIRST-BOOT PIN: call the seed BEFORE any reload — a boot alone must make it a live
    # callable preset resolvable in the same epoch, returning its baked payload.
    async with stack.mcp() as mcp:
        assert _SEED in await mcp.tool_names()
        result = await mcp.call_tool(_SEED, retry_on_reloading=True)
    value = result.data if result.data is not None else result.structured_content
    assert value == "seeded-base", value

    body = await _active_body(stack)
    assert body["base_tool"] == "e2e_echo"
    assert body["active_version"] == 1
    # Version 1 wears the shipped-default tag from the atomic create.
    versions = await _versions(stack)
    assert len(versions) == 1
    assert _SHIPPED_DEFAULT_TAG in versions[0]["tags"]

    # A re-boot re-runs the applier over the unchanged seed — a pure no-op, no new version.
    await _reload_env(stack, {})
    assert len(await _versions(stack)) == 1


async def test_content_change_upgrades_tagged_version(fresh_stack: Callable[..., TaiStack]) -> None:
    stack = fresh_stack(build_seams_seed_stack)
    assert (await _active_body(stack))["fixed_kwargs"]["payload"] == "seeded-base"

    # Flip the seed variant and reload: the re-imported module re-declares a DRIFTED body,
    # so the applier upgrades the tagged version in place.
    await _reload_env(stack, {"E2E_SEED_VARIANT": "upgraded"})

    versions = await _versions(stack)
    assert len(versions) == 2
    active = next(v for v in versions if v["is_current"])
    assert active["version"] == 2
    assert _SHIPPED_DEFAULT_TAG in active["tags"]
    assert (await _active_body(stack))["fixed_kwargs"]["payload"] == "seeded-upgraded"


async def test_operator_edit_survives_reboot(fresh_stack: Callable[..., TaiStack]) -> None:
    stack = fresh_stack(build_seams_seed_stack)
    # An operator saves a new version — the save door tags nothing, so the active version
    # is UNTAGGED (operator-edited).
    await stack.api().post(f"/api/presets/{_SEED}/versions", json={"fixed_kwargs": {"payload": "operator"}})
    versions = await _versions(stack)
    active = next(v for v in versions if v["is_current"])
    assert _SHIPPED_DEFAULT_TAG not in active["tags"]

    # A re-boot leaves the operator's untagged version untouched — no new version, the
    # active body still the operator's.
    await _reload_env(stack, {})
    versions_after = await _versions(stack)
    assert len(versions_after) == 2
    assert (await _active_body(stack))["fixed_kwargs"]["payload"] == "operator"


async def test_store_off_visible_skip_boots_healthy(fresh_stack: Callable[..., TaiStack]) -> None:
    # The versioned store OFF: the applier must skip the seed VISIBLY and create nothing,
    # and the stack still boots healthy (the fixture returning is that health).
    stack = fresh_stack(build_seams_seed_off_stack)
    async with stack.mcp() as mcp:
        assert _SEED not in await mcp.tool_names()
    # A store-off presets list is 200-empty — the seed was never created.
    listing: list[Any] = await stack.api().get("/api/presets")
    assert all(row.get("name") != _SEED for row in listing), listing
    # The skip is VISIBLE in the serve log, naming the seed and the OFF store.
    log = stack.process("serve").log_tail(lines=4000)
    assert _SEED in log, log
    assert "not configured" in log, "the seed skip line must name the unconfigured store"
