"""The embed deployment shape end to end: a user-owned ``uvicorn`` process serving
a host FastAPI app that mounts ``tai42_skeleton.asgi.create_app()``, driven over
HTTP exactly like every other stack.

This covers what the ``tai serve`` fleet suite cannot: true in-process Prometheus
metrics mode (no ``PROMETHEUS_MULTIPROC_DIR`` in the process), and worker-bus fan-out
reaching an embedded worker that never went through ``tai serve``.
"""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e import wait_for_async
from tai42_e2e.manifests import PROBE_TOOLS_TITLE
from tai42_e2e.stack import TaiStack, _probe_tolerating_reloading

# The tool-call counter the ``prometheus_metrics`` extension increments, and the
# label set the embed app's own worker stamps (``runtime="main"``).
_FAMILY = "tai_tool_call_count_total"
_LABELS = {"name": "e2e_echo", "runtime": "main", "title": PROBE_TOOLS_TITLE}


async def test_boot_and_host_route_coexist(embed_stack: TaiStack) -> None:
    """The mounted tai app serves ``/health`` and the host-owned
    ``/embed-host/ping`` route serves beside it."""
    api = embed_stack.api()
    health = await api.request_raw("GET", "/health")
    assert health.status_code == 200, f"mounted /health not healthy: {health.status_code} {health.text}"
    assert health.text.strip() == "OK"

    ping = await api.request_raw("GET", "/embed-host/ping")
    assert ping.status_code == 200, f"host route not reachable: {ping.status_code} {ping.text}"
    assert ping.json() == {"host": "ok"}


async def test_mcp_tool_round_trip(embed_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    """A real MCP tool call reaches the mounted app and returns its result."""
    payload = uniq("echo")
    async with embed_stack.mcp() as mcp:
        # A boot-fresh embed worker may still hold its self-resync reload gate.
        result = await mcp.call_tool("e2e_echo_prometheus_metrics", {"payload": payload}, retry_on_reloading=True)
    assert result.data == payload, f"echo round-trip returned {result.data!r}, expected {payload!r}"


async def test_in_process_metrics_render_the_tool_counter(embed_stack: TaiStack) -> None:
    """The embed app's own ``/metrics`` renders the in-process registry: a tool
    call increments the counter and the scrape shows it — with no
    ``PROMETHEUS_MULTIPROC_DIR`` anywhere in the process."""
    # Self-sufficient: capture the counter's own baseline, then drive one call, so
    # a node-id run of this test alone still materializes and observes the sample.
    baseline = embed_stack.app_scrape().sample(_FAMILY, _LABELS) or 0.0

    async with embed_stack.mcp() as mcp:
        await mcp.call_tool("e2e_echo_prometheus_metrics", {"payload": "metrics"}, retry_on_reloading=True)

    scrape = embed_stack.app_scrape()
    assert scrape.raw, "the embed /metrics returned an empty exposition body"
    assert scrape.require(_FAMILY, _LABELS) - baseline >= 1, (
        f"the in-process registry did not render the incremented tool counter:\n{scrape.raw[:2000]}"
    )


async def test_reload_fans_out_from_embedded_worker(embed_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    """Fleet config-reload fan-out works from an embedded worker: the config-reload
    door broadcasts on the worker bus, and the confirmed-origin set covers the bus
    presence census (the embed worker + its backend-worker sibling)."""
    marker = uniq("E2E_MARKER").upper()
    api = embed_stack.api()

    # Wait for the FULL fleet in the census first (embed worker + backend-worker
    # sibling): presence keys are TTL-heartbeat-kept and re-homed across reloads,
    # so an instantaneous read can under-count; and without both members pinned,
    # the subset assertion below could pass while proving nothing about the
    # backend sibling.
    async def full_fleet_census() -> set[str]:
        members = {origin.origin for origin in embed_stack.census()}
        return members if len(members) >= 2 else set()

    census = await wait_for_async(
        full_fleet_census,
        deadline=8.0,
        message="census never reached full fleet strength (embed worker + backend worker)",
    )

    # (a) The config-reload door published on the worker bus — the mode-wrapped fleet
    # fan-out shape (siblings apply asynchronously), never a bare local-only note.
    # Poll past the embed worker's boot-time reload gate.
    data = await api.post("/api/config/env", json={marker: "1"}, retry_on_reloading=True)
    fanout = data.get("fanout")
    assert isinstance(fanout, dict), f"POST /api/config/env carried no fanout field: {data!r}"
    assert fanout["mode"] == "fleet", f"embedded worker did not broadcast the reload over the fleet: {fanout!r}"

    # (b) The fleet ``reload_config`` tool returns a FleetResult (no ``workers`` key)
    # whose per-origin outcomes cover the census — the embedded worker joined the bus
    # exactly like a CLI worker, and the backend sibling applied the reload.
    async def _reload_over_mcp():
        async with embed_stack.mcp() as mcp:
            # Prime the SDK's per-session tool-output-schema cache (ClientSession
            # ._tool_output_schemas) so the post-call result validation
            # (_validate_tool_result) does not issue a follow-up tools/list: reload_config
            # retires this session (D13a — the swapped-in epoch serves a new session-id
            # space), so that follow-up would hit the orphaned session and raise
            # "Session terminated" even though the tool result was already delivered.
            await mcp.list_tools()
            return await mcp.call_tool("reload_config", {}, retry_on_reloading=True)

    # The prior env-reload (POST /api/config/env above) already swapped the embed worker's
    # epoch on a deferred session-manager close; a session opened here can be retired
    # before the call even lands (D13a). Re-open on a fresh session until it lands cleanly,
    # exactly as a real client re-initialises.
    result = await wait_for_async(
        lambda: _probe_tolerating_reloading(_reload_over_mcp),
        deadline=10.0,
        message="the embed reload_config tool never left the reload/session-swap window",
    )
    result_data = result.data if isinstance(result.data, dict) else result.structured_content
    assert isinstance(result_data, dict), f"reload_config returned no result map: {result!r}"
    assert result_data["reachable"] is True, f"the bus was unreachable: {result_data}"
    outcomes = {r["origin"]: r["outcome"] for r in result_data["results"]}
    # The pre-op full-fleet census snapshot (embed worker + backend worker) must be
    # covered and applied — this is what proves the backend sibling applied the reload.
    assert census <= outcomes.keys(), f"not every census worker confirmed: census={census} report={outcomes}"
    assert all(outcomes[origin] == "applied" for origin in census), f"an origin did not apply: {result_data}"
