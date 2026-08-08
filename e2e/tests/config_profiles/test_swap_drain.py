"""F6 — swap / drain behaviours of a profile apply's epoch build+swap.

Three landed contracts of ``config/service.py::apply_replace_env`` + ``app/epoch.py``:

* FAILED BUILD (C5 env-write-LAST): a build that fails leaves the old surface serving and
  the stored env byte-for-byte unchanged (STEP 4 ``replace_env`` never ran, the client epoch
  never advanced). The poison is an ENV value the SAVE-time validator accepts but the BUILD
  rejects — ``ACCESS_CONTROL_CACHE_SIZE`` (a hot-class ``int``) that every build constructs
  eagerly (``server.py::_build_serving_core`` reads ``access_control_settings()`` regardless
  of whether the gate is enabled), so a non-integer passes ``_validate_replace`` (no
  singleton construction) and raises a ``ValidationError`` deep in the rebuild.
* MCP SESSION D13a: the swap ``aclose``s the old epoch's FastMCP session manager, so a
  stateful session is retired (fresh session-id space) and the client cleanly re-initialises
  — the SAME ``McpClient`` handle transparently re-opens and serves the NEW epoch.
* IN-FLIGHT BACKEND WORK: a hot in-process swap does not lose an in-flight backend job — its
  work runs to completion across the swap.

Validated locally against the isolated infra.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.mcp import McpClient
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import wait_for


async def _read_until(lines: AsyncIterator[str], marker: str, *, deadline: float) -> None:
    """Read SSE lines until one contains ``marker``, bounded by ``deadline``. A stream that
    closes or goes silent before the marker is a loud failure — never a vacuous pass."""
    end = time.monotonic() + deadline
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"SSE stream did not deliver {marker!r} within {deadline}s")
        try:
            line = await asyncio.wait_for(lines.__anext__(), timeout=remaining)
        except StopAsyncIteration as exc:
            raise AssertionError(f"SSE stream closed before delivering {marker!r}") from exc
        except TimeoutError as exc:
            raise AssertionError(f"SSE stream went silent before delivering {marker!r} ({deadline}s)") from exc
        if marker in line:
            return


# A hot-class int settings field read at EVERY build (via _build_serving_core), so a
# non-integer value fails the build after passing the save-time validator.
_BUILD_POISON_VAR = "ACCESS_CONTROL_CACHE_SIZE"


async def _settings_epoch(stack: TaiStack) -> int:
    async with stack.mcp(port=stack.port_a) as mcp:
        result = await mcp.call_tool("e2e_settings_snapshot", {}, retry_on_reloading=True)
    data = result.data if isinstance(result.data, dict) else result.structured_content
    assert isinstance(data, dict), f"e2e_settings_snapshot returned no result map: {result!r}"
    return data["settings_epoch"]


async def _effective_settings_values(stack: TaiStack) -> dict[str, Any]:
    """The EFFECTIVE (os.environ-resolved) value of every non-masked registered settings field,
    read off the live worker via the F1 probe (``e2e_settings_snapshot`` resolves each field
    from ``os.environ``). Used to assert a failed build RESTORED ``os.environ`` (not just the
    stored env) — matching the skeleton unit ``test_profile_apply.py`` os.environ==before check."""
    async with stack.mcp(port=stack.port_a) as mcp:
        result = await mcp.call_tool("e2e_settings_snapshot", {}, retry_on_reloading=True)
    data = result.data if isinstance(result.data, dict) else result.structured_content
    assert isinstance(data, dict), f"e2e_settings_snapshot returned no result map: {result!r}"
    return {
        field["env_var"]: field["value"]
        for group in data["groups"]
        for field in group["fields"]
        if field["env_var"] and not (field["secret"] or field["key_material"])
    }


async def _snapshot_epoch_over(mcp: McpClient) -> int:
    """The ``settings_epoch`` read over an ALREADY-OPEN stateful session (not a fresh one) —
    so a re-init after a swap is observed on the same client handle."""
    result = await mcp.call_tool("e2e_settings_snapshot", {}, retry_on_reloading=True)
    data = result.data if isinstance(result.data, dict) else result.structured_content
    assert isinstance(data, dict), f"e2e_settings_snapshot returned no result map: {result!r}"
    return data["settings_epoch"]


async def _apply_hot_flip(stack: TaiStack, uniq: Callable[[str], str]) -> None:
    """Save + apply a profile that flips the hot ``TAI_LOG_LEVEL`` between two valid levels,
    carrying the rest of the stored env unchanged — a clean hot swap (no recycle)."""
    api = stack.api(port=stack.port_a)
    stored = (await api.get("/api/config/env"))["env"]
    new_level = "DEBUG" if (stored.get("TAI_LOG_LEVEL") or "INFO").upper() != "DEBUG" else "INFO"
    name = uniq("swap")
    await api.put(
        f"/api/config/profiles/{name}", json={"env": {**stored, "TAI_LOG_LEVEL": new_level}}, retry_on_reloading=True
    )
    await api.post(f"/api/config/profiles/{name}/apply", retry_on_reloading=True, timeout=120.0)


@pytest.mark.timeout(300)
async def test_failed_epoch_build_keeps_old_surface_and_stored_env(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    api = replicas_stack.api(port=replicas_stack.port_a)

    before_env: dict[str, Any] = (await api.get("/api/config/env"))["env"]
    before_epoch = await _settings_epoch(replicas_stack)
    # The EFFECTIVE settings values (resolved off os.environ) before the poisoned build — a
    # failed build must restore os.environ, so these must be byte-for-byte identical after.
    before_effective = await _effective_settings_values(replicas_stack)

    # A profile carrying a non-integer where the build expects an int. The save-time
    # boundary validator accepts it (it never constructs AccessControlSettings); the build
    # rejects it.
    name = uniq("poison")
    profile_env = {**before_env, _BUILD_POISON_VAR: "not-an-integer"}
    await api.put(f"/api/config/profiles/{name}", json={"env": profile_env}, retry_on_reloading=True)

    # The apply fails (the build raised); it is an error, never a 200. The exact code is
    # secondary — a construction ValidationError is not the door's ValueError path, so it
    # surfaces as a 5xx — what matters is that it did NOT apply.
    resp = await api.request_raw("POST", f"/api/config/profiles/{name}/apply")
    assert resp.status_code >= 400, f"a poisoned-build apply unexpectedly succeeded: {resp.status_code} {resp.text}"

    # The client epoch never advanced — the swap never happened (the build aborts before
    # ``advance_client_epoch``), so the old serving generation is still live.
    after_epoch = await _settings_epoch(replicas_stack)
    assert after_epoch == before_epoch, f"a failed build advanced the epoch: {before_epoch} -> {after_epoch}"

    # Env-write-LAST: STEP 4 (``replace_env``) never ran, so the stored env is UNCHANGED —
    # the poison value never reached the store.
    after_env = (await api.get("/api/config/env"))["env"]
    assert after_env == before_env, f"a failed build mutated the stored env: {before_env} -> {after_env}"
    assert _BUILD_POISON_VAR not in after_env, f"the poison value leaked into the stored env: {after_env}"

    # F6 / os.environ RESTORED: the failed build applied the profile to os.environ before the
    # rebuild raised; ``build_and_swap_epoch`` must restore os.environ exactly, so the EFFECTIVE
    # settings values (read off os.environ by the probe) are unchanged and the poison never
    # stuck in the live process env.
    after_effective = await _effective_settings_values(replicas_stack)
    assert after_effective == before_effective, (
        f"a failed build left os.environ mutated (effective settings values changed): "
        f"{before_effective} -> {after_effective}"
    )
    assert after_effective.get(_BUILD_POISON_VAR) != "not-an-integer", (
        f"the poison value is still live in os.environ after the failed build: {after_effective.get(_BUILD_POISON_VAR)}"
    )


@pytest.mark.timeout(300)
async def test_hot_apply_breaks_and_reinitializes_stateful_mcp_session(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """D13a: a swap retires the old epoch's FastMCP session manager, so a stateful session is
    TERMINATED (fresh session-id space) and the client cleanly re-initialises. The break is
    proven by the streamable-http SESSION ID changing across the apply — the old session was
    retired and ``McpClient`` transparently re-opened a fresh one. (``settings_epoch`` alone
    would NOT prove this: it is process-global and advances on the swap regardless of whether
    the session broke — so the session-id change is the load-bearing signal.)"""
    async with replicas_stack.mcp(port=replicas_stack.port_a) as mcp:
        # A stateful session established against the current epoch.
        before_epoch = await _snapshot_epoch_over(mcp)
        session_before = mcp.session_id
        assert session_before, "no MCP session id was established before the apply"

        # Apply a hot profile through the HTTP door (a SEPARATE connection). The swap
        # ``aclose``s the old epoch's session manager under the stateful session above.
        await _apply_hot_flip(replicas_stack, uniq)

        # The next call on the SAME handle hits the retired session ("Session terminated") and
        # transparently re-initialises a FRESH session — observed as a CHANGED session id.
        # Poll: the deferred session close lands a beat after the door-driven apply returns.
        async def session_reinitialised() -> bool:
            await _snapshot_epoch_over(mcp)  # a wrapped call re-inits iff the old session was retired
            return mcp.session_id not in (None, session_before)

        await wait_for_async(
            session_reinitialised,
            deadline=45.0,
            message="the stateful MCP session id never changed — the D13a break did not happen",
        )
        # The break is explicit: a NEW session id (old terminated + re-initialised), now
        # serving the swapped-in epoch.
        assert mcp.session_id != session_before, "the MCP session was not re-initialised across the apply"
        assert await _snapshot_epoch_over(mcp) > before_epoch, "the re-initialised session does not serve the new epoch"


@pytest.mark.timeout(300)
async def test_sse_stream_survives_a_hot_apply(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    """An SSE stream opened on the old epoch SURVIVES a hot apply. The interactions stream is
    a plain Starlette response (NOT the MCP session manager the swap ``aclose``s), so the
    retire drains it gracefully without cutting it: it keeps delivering after the swap, proven
    by its periodic ``: keepalive`` frame still arriving."""
    # The epoch of the worker that will serve the stream, captured before the swap.
    before_epoch = await _settings_epoch(replicas_stack)

    url = f"http://{replicas_stack.host}:{replicas_stack.port_a}/api/interactions/stream"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client, client.stream("GET", url) as response:
        assert response.status_code == 200, f"interactions SSE did not open: {response.status_code}"
        lines = response.aiter_lines()

        # The stream is live pre-apply: it replays its backlog up to the sentinel.
        await _read_until(lines, "backlog_done", deadline=20.0)

        # A hot apply lands on the SAME worker over a SEPARATE connection.
        await _apply_hot_flip(replicas_stack, uniq)

        # The SAME stream still delivers after the swap: its 15s keepalive still fires (the
        # old epoch keeps serving the in-flight stream — never abruptly cut).
        await _read_until(lines, "keepalive", deadline=45.0)

    # A REAL swap crossed the live stream: the serving worker's epoch advanced between the
    # pre-apply backlog read and the post-apply keepalive, so the stream genuinely survived a
    # generation swap rather than merely outlasting an awaited no-op.
    after_epoch = await _settings_epoch(replicas_stack)
    assert after_epoch > before_epoch, (
        f"the apply did not advance the stream worker's epoch: {before_epoch} -> {after_epoch}"
    )


@pytest.mark.timeout(300)
async def test_hot_apply_does_not_lose_in_flight_backend_work(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """A hot in-process swap does not lose an in-flight backend job: a job started before the
    apply and still executing when the swap lands runs to completion (its ``done`` marker,
    from the ORIGINAL backend pid, still arrives)."""
    api = replicas_stack.api(port=replicas_stack.port_a)
    key = uniq("swapdrain")
    serve_pids = {origin.pid for origin in replicas_stack.census() if origin.kind == "serve"}

    await api.post(
        "/api/tool-runs",
        json={"tool_name": "e2e_drain_probe_sync_task", "arguments": {"key": key, "seconds": 10}},
        expect=202,
    )

    def _values() -> list[str]:
        return [json.loads(rec)["value"] for rec in replicas_stack.records(key)]

    wait_for(lambda: "started" in _values(), deadline=30.0, message="the backend job never reported it was in-flight")
    started_pid = json.loads(replicas_stack.records(key)[0])["pid"]
    assert started_pid not in serve_pids, f"the job ran on a serve worker ({started_pid}), not a backend"
    assert "done" not in _values(), "the backend job completed before the swap could land mid-flight"

    # A hot swap lands while the backend job is in-flight.
    await _apply_hot_flip(replicas_stack, uniq)

    # The in-flight backend work completed across the swap (not lost), from the same pid.
    wait_for(lambda: "done" in _values(), deadline=90.0, message="the in-flight backend job was LOST across the swap")
    done_pids = {json.loads(rec)["pid"] for rec in replicas_stack.records(key) if json.loads(rec)["value"] == "done"}
    assert started_pid in done_pids, f"'done' came from a different pid than started ({started_pid} vs {done_pids})"
