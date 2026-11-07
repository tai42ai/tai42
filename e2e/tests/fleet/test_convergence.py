"""Fleet convergence on the multi-worker no-backend stack.

Each scenario mutates the deployment through ONE worker of a MULTIWORKER(2) fleet and
proves EVERY worker applied it from the mutation's own fan-out report: every serve member
on the bus census at the time of the call has an ``applied`` entry and no entry is
anything else (``_fleet.assert_applied_by``).

* mcp-config add server  → every serve member applies the new mcp section.
* tool-extensions apply  → every serve member applies the branch, which is then served.
* update_manifest        → every serve member applies it, and a fleet reload, applied by
                           every serve member, still serves the entry (the update persisted).
* out-of-band file edit  → no op reaches any worker and the edit is not served, then a
                           fleet reload, applied by every serve member, serves it.
* env write              → every serve member applies the write.
* ``/api/fleet/workers`` → the census lists every worker by name.

A refused write is a loud error and delivers no op: after the call every serve worker's
presence row keeps the generation read before it and its ``last_op`` names no delivery of
the write's op since the call started (``_fleet.assert_no_op_delivered``).

Live-vs-persisted drift is folded into the mcp-mutating scenarios: the mutated entry is
served in ``GET /api/manifest`` exactly as the persisted doc says. The extensions map and
env writes mutate nothing in that projection; their proof is the report alone.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import yaml

from tai42_e2e.stack import TaiStack

from ._fleet import (
    assert_applied_by,
    assert_fleet_fanout,
    assert_no_op_delivered,
    build_fleet_env_stack_builder,
    build_fleet_stack,
    call_start,
    manifest_file,
    serve_generations,
    serve_members,
    wait_op_stamped,
    wait_present,
)

pytestmark = pytest.mark.backendless

# Every scenario takes its OWN fresh stack: a section replace / out-of-band edit never
# poisons a sibling, and only one fleet is alive at a time.
#
# The mcp-config scenarios add an UNREACHABLE loopback server (the loopback range is
# opted into the URL guard by the base env). The viability probe cannot connect, so no
# tools bind and no subprocess is spawned — but the entry still lands, resolved, in every
# worker's live mcp section, so each worker's apply is the same on every run. A managed
# launchable server would bind real tools through a per-worker stdio-subprocess viability
# probe whose outcome varies across workers; the connector scenarios cover real
# managed-tool binding through the engine-managed connection lifecycle.
_UNREACHABLE_MCP = {"config": {"url": "http://127.0.0.1:1/mcp"}}


def _mcp_entry(title: str) -> dict:
    return {"title": title, **_UNREACHABLE_MCP}


async def _manifest_mcp_titles(stack: TaiStack, port: int | None = None) -> list[str]:
    """The titles of the persisted mcp section ``GET /api/manifest`` serves: the manifest file,
    read on every call, so the answer is what is on disk whichever worker serves it."""
    manifest = await stack.api(port=port).get("/api/manifest")
    return [entry["title"] for entry in manifest["mcp"]]


# ---- census -------------------------------------------------------------


@pytest.mark.needs("process", "store:redis", "topology:multiworker")
async def test_fleet_workers_lists_every_worker(fresh_stack: Callable[..., TaiStack]) -> None:
    """``GET /api/fleet/workers`` lists every worker by name — the census door's
    view is exactly the bus presence census the harness scans, one ``serve`` slot
    per uvicorn worker."""
    stack = fresh_stack(build_fleet_stack)
    workers = (await stack.api().get("/api/fleet/workers"))["workers"]
    names = {w["name"] for w in workers}
    assert names == {worker.name for worker in stack.census()}
    assert all(w["kind"] == "serve" for w in workers), workers
    # Every worker carries a live pid; the multi-worker fleet lists more than one.
    assert all(isinstance(w["pid"], int) for w in workers), workers
    assert len(names) >= 2, f"a multi-worker fleet must list >1 worker: {workers}"


# ---- tool-extensions ----------------------------------------------------


@pytest.mark.needs("probe-tools", "mutable", "process", "topology:multiworker", "setting:extension:batch")
async def test_tool_extensions_apply_converges(fresh_stack: Callable[..., TaiStack]) -> None:
    """A tool-extensions apply on one worker binds the branch on EVERY worker: the response's
    mode-wrapped ApplyResult fan-out report shows every serve member applied, and the branch
    tool is served.

    The target ``e2e_echo`` is TOOLS-module-owned, so the extensions map lands outside the
    ``GET /api/manifest`` mcp projection; the served tool listing is the caller's read."""
    stack = fresh_stack(build_fleet_stack)
    api = stack.api()

    members = serve_members(stack)
    result = await api.post("/api/tools/e2e_echo/extensions", json={"combos": [["batch"]]})
    assert_applied_by(assert_fleet_fanout(result), members)

    # The branch tool is served (the live tool listing gained ``e2e_echo_batch``).
    async def serves_branch() -> bool:
        async with stack.mcp() as mcp:
            return "e2e_echo_batch" in await mcp.tool_names()

    await wait_present(serves_branch, message="e2e_echo_batch was not served after the apply")


# ---- mcp-config add server ----------------------------------------------


@pytest.mark.needs("probe-tools", "mutable", "process", "topology:multiworker")
async def test_mcp_config_add_server_converges(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    """Adding an MCP server through ``POST /api/mcp-config`` is applied by EVERY serve member
    (the fan-out report), and the new entry is served in ``GET /api/manifest`` exactly as the
    persisted doc says."""
    stack = fresh_stack(build_fleet_stack)
    api = stack.api()

    title = uniq("b1_mcp")
    members = serve_members(stack)
    result = await api.post("/api/mcp-config", json={"mcp": [_mcp_entry(title)]})
    assert_applied_by(assert_fleet_fanout(result), members)

    titles = await _manifest_mcp_titles(stack)
    assert title in titles, f"the added entry is not served: {titles}"


# ---- split-brain: update_manifest persists ------------------------------


@pytest.mark.needs("probe-tools", "mutable", "process", "files", "topology:multiworker")
async def test_update_manifest_persists_across_fleet_reload(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    """``update_manifest`` whose document ADDS an mcp entry is applied by every serve member;
    a subsequent ``POST /api/fleet/reload-config``, applied by every serve member, still
    serves the entry — the update persisted, so a reload re-reads it."""
    stack = fresh_stack(build_fleet_stack)
    api = stack.api()

    document = yaml.safe_load(manifest_file(stack).read_text()) or {}
    title = uniq("b5a_mcp")
    document["mcp"] = [*document.get("mcp", []), _mcp_entry(title)]
    members = serve_members(stack)
    result = await api.post("/api/manifest/replace", json={"manifest_text": yaml.safe_dump(document)})
    assert_applied_by(assert_fleet_fanout(result), members)
    assert title in await _manifest_mcp_titles(stack), "the replaced manifest's entry is not served"

    # The update persisted: every serve member re-reads it on a fleet reload.
    members = serve_members(stack)
    reload = await api.post("/api/fleet/reload-config", json={"targets": None})
    assert_applied_by(reload, members)
    assert title in await _manifest_mcp_titles(stack), "the persisted update is not served after a fleet reload"


# ---- fleet-route local apply of an out-of-band edit ---------------------


@pytest.mark.needs("probe-tools", "mutable", "process", "files", "topology:multiworker")
async def test_fleet_reload_applies_out_of_band_edit(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    """An out-of-band manifest edit (raw write past the pipeline) reaches no worker: no
    ``reload_config`` is delivered (each serve row keeps its generation and names no
    ``reload_config`` since the edit). Then ``POST /api/fleet/reload-config`` makes EVERY serve
    member re-read the file (the fan-out report)."""
    stack = fresh_stack(build_fleet_stack)
    api = stack.api()

    path = manifest_file(stack)
    document = yaml.safe_load(path.read_text()) or {}
    title = uniq("b5b_mcp")
    document["mcp"] = [*document.get("mcp", []), _mcp_entry(title)]
    generations = serve_generations(stack)
    started_at = call_start()
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    # Nothing reloaded the workers: no reload_config was delivered.
    assert_no_op_delivered(stack, generations, "reload_config", started_at, generations)

    # The fleet route makes every serve member re-read the file.
    members = serve_members(stack)
    reload = await api.post("/api/fleet/reload-config", json={"targets": None})
    assert_applied_by(reload, members)


# ---- env write ----------------------------------------------------------


@pytest.mark.needs(
    "probe-tools",
    "mutable",
    "process",
    "topology:multiworker",
    "setting:manifest:mcp-env-probe",
)
async def test_env_write_moves_resolved_view(fresh_stack: Callable[..., TaiStack]) -> None:
    """An env write is applied by every serve member (the fan-out report). The seeded
    manifest references the written key via ``!ENV``, so the write moves the RESOLVED
    config the write validates and every worker reloads. This mutates nothing in the mcp
    projection."""
    key = "E2E_FLEET_B6"
    stack = fresh_stack(build_fleet_env_stack_builder(key))
    api = stack.api()

    members = serve_members(stack)
    result = await api.post("/api/config/env", json={"env": {key: "b6-converged-value"}})
    assert_applied_by(assert_fleet_fanout(result), members)


# ---- granular manifest-family entry ops (mcp / tools / agents / api_tools) ----
#
# The per-entry add/remove doors mutate ONE section of the persisted manifest and ride
# the SAME pipeline the whole-list writers above ride, so convergence is proven the same
# way: every serve member applied the write (the fan-out report). The section-specific
# content is then read back from the one worker that serves the read. ``GET /api/manifest``
# serves only the preserved mcp section, so tools/agents/api_tools are read through the
# backup export door (the fleet stack mounts ``routers.backup``), which returns the whole
# persisted manifest document.

# A second unreachable loopback entry shape, distinct from ``_UNREACHABLE_MCP`` only in its
# (still-unresolvable) port — the changed field the in-place replace swaps in.
_UNREACHABLE_MCP_ALT = {"config": {"url": "http://127.0.0.1:2/mcp"}}


async def _manifest_mcp(stack: TaiStack) -> list[dict]:
    """The full mcp entries of the persisted mcp section ``GET /api/manifest`` serves (not just
    the titles): the operand for the in-place-replace position/field assertions."""
    manifest = await stack.api().get("/api/manifest")
    return list(manifest["mcp"])


async def _exported_manifest(stack: TaiStack) -> dict:
    """The whole persisted manifest document, read through the backup export door — the
    read for the tools/agents/api_tools sections ``GET /api/manifest`` omits."""
    export = await stack.api().request_raw("POST", "/api/backup/export", json={"sections": ["manifest"]})
    assert export.status_code == 200, export.text
    return export.json()["sections"]["manifest"]


def _section_titles(manifest: dict, section: str) -> list[str]:
    return [entry["title"] for entry in manifest.get(section, [])]


# ---- A: mcp entries ------------------------------------------------------


@pytest.mark.needs("probe-tools", "mutable", "process", "topology:multiworker")
async def test_mcp_entries_add_converges(fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]) -> None:
    """Seed one entry through the whole-list ``POST /api/mcp-config``, then append a
    SECOND through the granular ``POST /api/mcp-config/entries``: every serve member applies
    the add (the fan-out report) and both titles are served in ``GET /api/manifest``."""
    stack = fresh_stack(build_fleet_stack)
    api = stack.api()

    seed = uniq("a1_seed")
    await api.post("/api/mcp-config", json={"mcp": [_mcp_entry(seed)]})

    added = uniq("a1_add")
    members = serve_members(stack)
    result = await api.post("/api/mcp-config/entries", json={"entries": [_mcp_entry(added)]})
    assert_applied_by(assert_fleet_fanout(result), members)

    titles = await _manifest_mcp_titles(stack)
    assert seed in titles, titles
    assert added in titles, titles


@pytest.mark.needs("probe-tools", "mutable", "process", "topology:multiworker")
async def test_mcp_entries_add_duplicate_refused(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    """A second add of an existing title WITHOUT ``replace`` is a loud 400 naming the
    title, and no op reaches any worker: every serve row keeps its generation and names no
    delivery of the write's op since the refused call started, and the served entries are
    unchanged."""
    stack = fresh_stack(build_fleet_stack)
    api = stack.api()

    title = uniq("a2_mcp")
    seed_started = call_start()
    seed = await api.post("/api/mcp-config", json={"mcp": [_mcp_entry(title)]})
    seeded = assert_fleet_fanout(seed)
    # The seed's own stamps land before the refused call starts.
    wait_op_stamped(stack, seeded, seed_started)

    generations = serve_generations(stack)
    started_at = call_start()
    body = await api.post("/api/mcp-config/entries", json={"entries": [_mcp_entry(title)]}, expect=400)
    assert title in body["error"], body

    assert_no_op_delivered(stack, generations, seeded["op"], started_at, generations)
    assert await _manifest_mcp_titles(stack) == [title]


@pytest.mark.needs("probe-tools", "mutable", "process", "topology:multiworker")
async def test_mcp_entries_add_replace_in_place(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    """``replace: true`` for an existing title swaps the entry in AT ITS CURRENT POSITION,
    leaving order and the neighbor entry untouched."""
    stack = fresh_stack(build_fleet_stack)
    api = stack.api()

    first = uniq("a3_first")
    second = uniq("a3_second")
    await api.post("/api/mcp-config", json={"mcp": [_mcp_entry(first), _mcp_entry(second)]})

    replacement = {"title": first, **_UNREACHABLE_MCP_ALT}
    members = serve_members(stack)
    result = await api.post("/api/mcp-config/entries", json={"entries": [replacement], "replace": True})
    assert_applied_by(assert_fleet_fanout(result), members)

    entries = await _manifest_mcp(stack)
    assert [entry["title"] for entry in entries] == [first, second], entries
    assert entries[0]["config"]["url"] == _UNREACHABLE_MCP_ALT["config"]["url"], entries[0]
    assert entries[1]["config"]["url"] == _UNREACHABLE_MCP["config"]["url"], entries[1]


@pytest.mark.needs("probe-tools", "mutable", "process", "topology:multiworker")
async def test_mcp_entry_remove_converges_and_persists(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    """DELETE one of two entries by title: every serve member applies the removal (the fan-out
    report) and the title is not served while the other is, and a subsequent ``POST
    /api/fleet/reload-config``, applied by every serve member, confirms the removal PERSISTED —
    the title stays not served."""
    stack = fresh_stack(build_fleet_stack)
    api = stack.api()

    keep = uniq("a4_keep")
    drop = uniq("a4_drop")
    await api.post("/api/mcp-config", json={"mcp": [_mcp_entry(keep), _mcp_entry(drop)]})

    members = serve_members(stack)
    result = await api.delete(f"/api/mcp-config/entries/{drop}")
    assert_applied_by(assert_fleet_fanout(result), members)

    titles = await _manifest_mcp_titles(stack)
    assert drop not in titles, titles
    assert keep in titles, titles

    members = serve_members(stack)
    reload = await api.post("/api/fleet/reload-config", json={"targets": None})
    assert_applied_by(reload, members)
    persisted = await _manifest_mcp_titles(stack)
    assert drop not in persisted, persisted
    assert keep in persisted, persisted


@pytest.mark.needs
async def test_mcp_entry_remove_unknown_404(fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]) -> None:
    """Removing a title that is not in the mcp section is a loud 404 naming it."""
    stack = fresh_stack(build_fleet_stack)
    api = stack.api()

    missing = uniq("a5_absent")
    body = await api.delete(f"/api/mcp-config/entries/{missing}", expect=404)
    assert missing in body["error"], body


# ---- B: tools / agents entries -------------------------------------------

# A selection-free toolbox tool module the base fleet manifest does NOT already load, so a
# tools entry adding it is a real add that registers a tool on every worker.
_TOOLS_MODULE = "tai42_toolbox.tools.generate_uuid"
_TOOLS_INCLUDE = ["generate_uuid"]

# One reference agent whose module registers a single agent; adding it registers the agent
# (and its run tool) with no LLM env — the model is resolved lazily at run time.
_AGENTS_MODULE = "tai42_agents.tools_agent"
_AGENTS_INCLUDE = ["tools_agent"]


@pytest.mark.needs(
    "probe-tools",
    "mutable",
    "process",
    "topology:multiworker",
    "setting:tool:generate_uuid",
    "setting:router:backup",
)
async def test_tools_entries_add_remove_converges(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    """Add a ``tools`` entry through ``POST /api/tools-config/entries``: every serve member
    applies it (the fan-out report) and it lands in the tools section, then DELETE, applied
    by every serve member, removes it and a fleet reload confirms it stays gone (persisted)."""
    stack = fresh_stack(build_fleet_stack)
    api = stack.api()

    title = uniq("b6_tools")
    entry = {"title": title, "module": _TOOLS_MODULE, "include": _TOOLS_INCLUDE}
    members = serve_members(stack)
    result = await api.post("/api/tools-config/entries", json={"entries": [entry]})
    assert_applied_by(assert_fleet_fanout(result), members)
    assert title in _section_titles(await _exported_manifest(stack), "tools")

    members = serve_members(stack)
    removed = await api.delete(f"/api/tools-config/entries/{title}")
    assert_applied_by(assert_fleet_fanout(removed), members)

    members = serve_members(stack)
    reload = await api.post("/api/fleet/reload-config", json={"targets": None})
    assert_applied_by(reload, members)
    assert title not in _section_titles(await _exported_manifest(stack), "tools")


@pytest.mark.needs(
    "probe-tools",
    "mutable",
    "process",
    "topology:multiworker",
    "setting:agent:tools_agent",
    "setting:router:backup",
)
async def test_agents_entries_add_remove_converges(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    """Same shape for ``agents`` — the identical op code path over ``POST
    /api/agents-config/entries`` and its DELETE, on the ``agents`` section."""
    stack = fresh_stack(build_fleet_stack)
    api = stack.api()

    title = uniq("b7_agents")
    entry = {"title": title, "module": _AGENTS_MODULE, "include": _AGENTS_INCLUDE}
    members = serve_members(stack)
    result = await api.post("/api/agents-config/entries", json={"entries": [entry]})
    assert_applied_by(assert_fleet_fanout(result), members)
    assert title in _section_titles(await _exported_manifest(stack), "agents")

    members = serve_members(stack)
    removed = await api.delete(f"/api/agents-config/entries/{title}")
    assert_applied_by(assert_fleet_fanout(removed), members)

    members = serve_members(stack)
    reload = await api.post("/api/fleet/reload-config", json={"targets": None})
    assert_applied_by(reload, members)
    assert title not in _section_titles(await _exported_manifest(stack), "agents")


# ---- C: api_tools include/exclude lists ----------------------------------


@pytest.mark.needs(
    "probe-tools",
    "mutable",
    "process",
    "topology:multiworker",
    "setting:router:backup",
)
async def test_api_tools_lists_update_converges(
    fresh_stack: Callable[..., TaiStack], uniq: Callable[[str], str]
) -> None:
    """Add a name to the ``api_tools`` exclude list through ``POST /api/api-tools``: every serve
    member applies it (the fan-out report) and the api_tools block carries it, a duplicate add
    is a loud 400 that delivers no op and an absent remove a loud 404, then removing it,
    applied by every serve member, clears the block again."""
    stack = fresh_stack(build_fleet_stack)
    api = stack.api()

    name = uniq("c8_op")
    members = serve_members(stack)
    add_started = call_start()
    result = await api.post("/api/api-tools", json={"exclude_add": [name]})
    fanout = assert_fleet_fanout(result)
    assert_applied_by(fanout, members)
    # The add's own stamps land before the refused call starts.
    wait_op_stamped(stack, fanout, add_started)
    assert name in (await _exported_manifest(stack)).get("api_tools", {}).get("exclude", [])

    # Adding the same name again → loud 400 naming it; no op reaches any worker.
    generations = serve_generations(stack)
    started_at = call_start()
    dup = await api.post("/api/api-tools", json={"exclude_add": [name]}, expect=400)
    assert name in dup["error"], dup
    assert_no_op_delivered(stack, generations, fanout["op"], started_at, generations)

    # Removing a name absent from the list → loud 404 naming it.
    absent = uniq("c8_absent")
    missing = await api.post("/api/api-tools", json={"exclude_remove": [absent]}, expect=404)
    assert absent in missing["error"], missing

    # Removing the real name clears the block again on every serve member.
    members = serve_members(stack)
    cleared = await api.post("/api/api-tools", json={"exclude_remove": [name]})
    assert_applied_by(assert_fleet_fanout(cleared), members)
    assert name not in (await _exported_manifest(stack)).get("api_tools", {}).get("exclude", [])
