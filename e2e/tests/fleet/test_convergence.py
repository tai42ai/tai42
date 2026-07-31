"""Fleet convergence on the multi-worker no-backend stack.

Each scenario mutates the deployment through ONE worker of a MULTIWORKER(2) fleet
and proves EVERY worker converged, read off the real per-worker probe digest
(``_fleet.converged_digest`` — polled, never sampled once): convergence is every
distinct pid reporting the SAME digest, DIFFERING from the pre-mutation baseline.

* mcp-config add server  → the live mcp section converges fleet-wide.
* tool-extensions apply  → the branch binds fleet-wide (ApplyResult report shape).
* update_manifest        → converge, then a fleet reload leaves digests UNCHANGED
                           (the update persisted).
* out-of-band file edit  → all digests stay stale, then a fleet reload converges
                           EVERY worker onto the file's new state.
* env write              → the resolved (``!ENV``) live view moves fleet-wide.
* ``/api/fleet/workers`` → the census lists every worker origin.

Live-vs-persisted drift is folded into the mcp-mutating scenarios: the mutated entry
is present in ``GET /api/manifest`` exactly as the persisted doc says. The extensions
map and env writes mutate nothing in that projection and are excluded — their proof
is the digest.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import yaml
from _fleet import (
    assert_fleet_fanout,
    build_fleet_env_stack_builder,
    build_fleet_stack,
    converged_baseline,
    converged_digest,
    manifest_file,
    wait_present,
)

from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless

# Every scenario takes its OWN fresh stack: a section replace / out-of-band edit never
# poisons a sibling, and only one fleet is alive at a time.
#
# The mcp-config scenarios add an UNREACHABLE loopback server (the loopback range is
# opted into the URL guard by the base env). The viability probe cannot connect, so no
# tools bind and no subprocess is spawned — but the entry still lands, resolved, in the
# live mcp section every worker hashes into its digest, so the add converges the fleet
# DETERMINISTICALLY. A managed launchable server would bind real tools, but its
# per-worker stdio-subprocess viability probe is nondeterministic across workers (a
# raced probe leaves one worker without the tools and the fleet never converging); the
# digest over the live mcp section is the robust convergence proof the harness is built
# on, and the connector scenarios cover real managed-tool binding through the
# engine-managed connection lifecycle.
_UNREACHABLE_MCP = {"config": {"url": "http://127.0.0.1:1/mcp"}}


def _mcp_entry(title: str) -> dict:
    return {"title": title, **_UNREACHABLE_MCP}


async def _manifest_mcp_titles(stack: TaiStack, port: int | None = None) -> list[str]:
    """The titles in the LIVE mcp projection ``GET /api/manifest`` serves (the
    live-vs-persisted operand — the persisted mcp section, not the whole document)."""
    manifest = await stack.api(port=port).get("/api/manifest", retry_on_reloading=True)
    return [entry["title"] for entry in manifest["mcp"]]


# ---- census -------------------------------------------------------------


async def test_fleet_workers_lists_every_worker_origin(fresh_stack: Callable[..., TaiStack]) -> None:
    """``GET /api/fleet/workers`` lists every worker origin — the census door's
    view is exactly the bus presence census the harness scans, one ``serve`` origin
    per uvicorn worker."""
    stack = fresh_stack(build_fleet_stack)
    workers = (await stack.api().get("/api/fleet/workers", retry_on_reloading=True))["workers"]
    origins = {w["origin"] for w in workers}
    assert origins == {origin.origin for origin in stack.census()}
    assert all(w["kind"] == "serve" for w in workers), workers
    # Every worker carries a live pid; the multi-worker fleet lists more than one.
    assert all(isinstance(w["pid"], int) for w in workers), workers
    assert len(origins) >= 2, f"a multi-worker fleet must list >1 origin: {workers}"


# ---- tool-extensions ----------------------------------------------------


async def test_tool_extensions_apply_converges(fresh_stack: Callable[..., TaiStack]) -> None:
    """A tool-extensions apply on one worker binds the branch on EVERY worker; the
    response carries the mode-wrapped ApplyResult fleet report.

    The target ``e2e_echo`` is TOOLS-module-owned, so the extensions map lands outside
    the ``GET /api/manifest`` mcp projection — it mutates nothing in that projection,
    and the digest is the whole proof."""
    stack = fresh_stack(build_fleet_stack)
    api = stack.api()
    baseline = await converged_baseline(stack)

    result = await api.post("/api/tools/e2e_echo/extensions", json={"combos": [["batch"]]}, retry_on_reloading=True)

    # The ApplyResult report shape: a mode-wrapped fleet fan-out, every origin applied.
    fanout = assert_fleet_fanout(result)
    assert fanout["results"], f"the apply reported no per-origin outcomes: {fanout}"
    assert all(r["outcome"] == "applied" for r in fanout["results"]), fanout

    # The real proof: every worker's digest converged onto a NEW value.
    await converged_digest(stack, differ_from=baseline)

    # And the branch tool is served (the live tool listing gained ``e2e_echo_batch``).
    async def serves_branch() -> bool:
        async with stack.mcp() as mcp:
            return "e2e_echo_batch" in await mcp.tool_names()

    await wait_present(serves_branch, message="the fleet never served e2e_echo_batch after the apply")


# ---- mcp-config add server ----------------------------------------------


async def test_mcp_config_add_server_converges(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    """Adding an MCP server through ``POST /api/mcp-config`` converges the live mcp
    section on EVERY worker (the per-worker digest moves onto one new value), and the
    new entry is present in ``GET /api/manifest`` exactly as the persisted doc says."""
    stack = fresh_stack(build_fleet_stack)
    api = stack.api()
    baseline = await converged_baseline(stack)

    title = uniq("b1_mcp")
    result = await api.post("/api/mcp-config", json={"mcp": [_mcp_entry(title)]}, retry_on_reloading=True)
    assert_fleet_fanout(result)

    await converged_digest(stack, differ_from=baseline)

    # The persisted-doc mcp entry is present in the live projection every worker serves.
    assert title in await _manifest_mcp_titles(stack)


# ---- split-brain: update_manifest persists ------------------------------


async def test_update_manifest_persists_across_fleet_reload(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    """``update_manifest`` whose document ADDS an mcp entry converges the fleet;
    a subsequent ``POST /api/fleet/reload-config`` leaves every digest UNCHANGED — the
    update persisted, so a reload re-reads the same state."""
    stack = fresh_stack(build_fleet_stack)
    api = stack.api()
    baseline = await converged_baseline(stack)

    document = yaml.safe_load(manifest_file(stack).read_text()) or {}
    title = uniq("b5a_mcp")
    document["mcp"] = [*document.get("mcp", []), _mcp_entry(title)]
    result = await api.post(
        "/api/manifest/replace", json={"manifest_text": yaml.safe_dump(document)}, retry_on_reloading=True
    )
    assert_fleet_fanout(result)

    converged = await converged_digest(stack, differ_from=baseline)
    assert title in await _manifest_mcp_titles(stack)  # the mutated entry shows in the live projection

    # The update persisted: a fleet reload re-reads it, so every digest is UNCHANGED.
    await api.post("/api/fleet/reload-config", json={"targets": None}, retry_on_reloading=True)
    after_reload = await converged_digest(stack)
    assert after_reload == converged, "the persisted update did not survive a fleet reload"


# ---- fleet-route local apply of an out-of-band edit ---------------------


async def test_fleet_reload_applies_out_of_band_edit(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    """An out-of-band manifest edit (raw write past the pipeline) leaves every
    worker stale — no broadcast fires — so all digests are UNCHANGED; then ``POST
    /api/fleet/reload-config`` makes EVERY worker re-read the file, changing every
    digest (fleet-wide convergence subsumes whichever worker served the request)."""
    stack = fresh_stack(build_fleet_stack)
    api = stack.api()
    baseline = await converged_baseline(stack)

    path = manifest_file(stack)
    document = yaml.safe_load(path.read_text()) or {}
    title = uniq("b5b_mcp")
    document["mcp"] = [*document.get("mcp", []), _mcp_entry(title)]
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    # Nothing reloaded the workers, so every digest is still the pre-edit baseline.
    assert await converged_digest(stack) == baseline, "an out-of-band edit must not converge the fleet on its own"

    # The fleet route makes every worker re-read the file: all digests move.
    await api.post("/api/fleet/reload-config", json={"targets": None}, retry_on_reloading=True)
    await converged_digest(stack, differ_from=baseline)
    assert title in await _manifest_mcp_titles(stack)  # the mutated entry shows in the live projection


# ---- env write ----------------------------------------------------------


async def test_env_write_moves_resolved_view(fresh_stack: Callable[..., TaiStack]) -> None:
    """An env write is observed by every worker. The seeded manifest references the
    written key via ``!ENV``, so the digest — which hashes the RESOLVED live view —
    actually moves (an unreferenced env write would leave the projection, and the
    digest, untouched). This mutates nothing in the mcp projection."""
    key = "E2E_FLEET_B6"
    stack = fresh_stack(build_fleet_env_stack_builder(key))
    api = stack.api()
    baseline = await converged_baseline(stack)

    result = await api.post("/api/config/env", json={key: "b6-converged-value"}, retry_on_reloading=True)
    assert_fleet_fanout(result)

    await converged_digest(stack, differ_from=baseline)
