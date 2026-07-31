"""Probe tools the harness observes the system under test through.

Each registers at import via ``@tai42_app.tools.tool`` and is selected by a
``tools:`` manifest entry. Probes never mock — they expose an in-process
observable (a return value or an ``e2e_record`` Redis side effect) so a test can
read what actually happened inside a real server/worker process."""

from __future__ import annotations

import json
import os
import socket

from pydantic_settings import SettingsConfigDict
from tai42_contract.app import tai42_app
from tai42_kit.clients import RedisConnectionSettings


class _E2eProbeRedisSettings(RedisConnectionSettings):
    """Points the probe-record client at the harness probe channel (logical DB
    0) via ``E2E_PROBE_REDIS_URL``."""

    model_config = SettingsConfigDict(env_prefix="E2E_PROBE_")


@tai42_app.tools.tool(tags={"e2e"})
def e2e_echo(payload: str) -> str:
    """Return ``payload`` unchanged — the default subject for extension
    attachment (prometheus / batch / proxy branches)."""
    return payload


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_worker_info() -> dict:
    """Report identity + metrics/socket state + a state digest of the process that
    ran this call.

    ``value_class`` is the frozen ``prometheus_client`` value backend —
    ``MmapedValue`` when the multiproc env froze correctly, ``MutexValue`` when
    it did not (the C2 in-vivo probe). ``socket_class`` doubles as the C8
    pristine-socket check (a proxy leak would leave a non-stdlib socket class).

    ``state_digest`` is a PROBE-COMPUTED hash of this process's OWN live view —
    ``tai42_app.admin.live_manifest`` plus its sorted live tool names, computed
    in-process. It is not a SUT-emitted revision marker (none exists): convergence
    is every distinct pid reporting the SAME digest, differing from a pre-mutation
    baseline, so a test never recomputes an expected digest from the seeded file
    (the live view is defaults-merged and ``!ENV``-resolved)."""
    import hashlib

    import prometheus_client.values

    value_class = getattr(prometheus_client.values.ValueClass, "__name__", repr(prometheus_client.values.ValueClass))
    tool_names = sorted(await tai42_app.tools.get_tools())
    material = json.dumps({"manifest": tai42_app.admin.live_manifest, "tools": tool_names}, sort_keys=True, default=str)
    state_digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return {
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "cwd": os.getcwd(),
        "value_class": value_class,
        "multiproc_dir_env": os.environ.get("PROMETHEUS_MULTIPROC_DIR"),
        "socket_class": f"{socket.socket.__module__}.{socket.socket.__qualname__}",
        "state_digest": state_digest,
    }


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_record(key: str, value: str) -> str:
    """RPUSH ``{"value", "pid"}`` onto the Redis list ``e2e:rec:{key}`` so the
    harness (which reads that list) sees a side effect from whichever process
    executed the call — webhook/hook/backend-execution observation."""
    from collections.abc import Awaitable
    from typing import cast

    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient

    record = json.dumps({"value": value, "pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        # The pooled client yields an async Redis; rpush returns an awaitable.
        await cast(Awaitable[int], client.rpush(f"e2e:rec:{key}", record))
    return "recorded"


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_sleep(seconds: float) -> str:
    """Sleep ``seconds`` (bounded at 30 so a mis-scripted test cannot hang) —
    the target of background tool-run tests."""
    import asyncio

    if seconds > 30:
        raise ValueError(f"e2e_sleep bound exceeded: {seconds} > 30 seconds")
    await asyncio.sleep(seconds)  # noqa: TID251 — the observed work under test, bounded above
    return "slept"


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_slow_task(key: str, seconds: float) -> str:
    """RPUSH a ``started`` marker onto ``e2e:rec:{key}`` then sleep ``seconds``
    (bounded at 30). The worker-crash spec attaches ``sync_task`` to this, runs it
    through the tool-runs API, waits for the marker to prove the run is in-flight
    IN the backend worker, then SIGKILLs the worker — so the terminal it reads
    back is caused by the crash, not by a self-completing sleep."""
    import asyncio
    from collections.abc import Awaitable
    from typing import cast

    from tai42_kit.clients import client_ctx
    from tai42_kit.clients.impl.redis import RedisClient

    if seconds > 30:
        raise ValueError(f"e2e_slow_task bound exceeded: {seconds} > 30 seconds")
    record = json.dumps({"value": "started", "pid": os.getpid()})
    async with client_ctx(RedisClient, _E2eProbeRedisSettings()) as client:
        await cast(Awaitable[int], client.rpush(f"e2e:rec:{key}", record))
    await asyncio.sleep(seconds)  # noqa: TID251 — the observed in-flight work, bounded above
    return "done"


@tai42_app.tools.tool(tags={"e2e"})
def e2e_fail(message: str) -> str:
    """Always raise — the error-counter / is-error path."""
    raise ValueError(message)


@tai42_app.tools.tool(tags={"e2e"})
def e2e_external_link(callback_url: str) -> str:
    """Build and return an external action URL embedding ``callback_url`` — the
    subject the ``ask_external`` extension wraps. The extension supplies
    ``callback_url`` (the platform-minted ticket URL) and drives the human-in-the-
    loop external ask; this tool's job is only to weave it into the link the human
    would visit."""
    return f"https://ext.example/act?cb={callback_url}"


@tai42_app.tools.tool(tags={"e2e"})
def e2e_http_probe(url: str) -> dict:
    """GET ``url`` with a fresh env-ignoring client. Its ``_proxy`` branch tunnels
    through the harness CONNECT proxy at the socket layer; the plain tool must
    reach the target directly."""
    import httpx

    with httpx.Client(trust_env=False, timeout=10.0) as client:
        response = client.get(url)
    return {"status": response.status_code, "body_head": response.text[:200]}


@tai42_app.tools.tool(tags={"e2e"})
async def run_tool(tool_name: str, arguments: dict[str, object]) -> object:
    """Dispatch any registered tool by name — the e2e backend-execution door.

    The skeleton ``run_tool`` operation is a tier-1 META-EXECUTOR hardcode-blocked
    from the MCP surface, so it has no projected base for the ``sync_task`` backend
    extension to wrap. This fixture provides an MCP-callable dispatch-by-name whose
    ``sync_task`` branch (``run_tool_sync_task``, attached in ``_probe_tools_entry``)
    runs the dispatch INSIDE the backend worker — the "run tool X in the backend"
    vehicle the cross-worker / preset / schedule specs drive. It mirrors the skeleton
    ``run_tool`` operation's ``(tool_name, arguments)`` signature, delegating to the
    same internal registry dispatch."""
    return await tai42_app.tools.run_tool(tool_name, arguments)


@tai42_app.tools.tool(tags={"e2e"})
async def e2e_resolve_connector(connection_id: str, provider_id: str = "e2e_idp", sub_service: str = "default") -> dict:
    """Resolve a managed-connector token for ``connection_id`` in this process.

    Drives the real resolver path (freshness gate + refresh-under-lock + CAS
    write-back); an expired stored token forces one refresh serialized by the
    cross-process connection lock. Two concurrent calls across replicas therefore
    trigger exactly one upstream refresh — the seam the refresh-lock test reads
    off the stub IdP's refresh counter."""
    from tai42_skeleton.connectors.runtime.resolver import resolve_managed_auth

    auth = await resolve_managed_auth(connection_id, provider_id, sub_service)
    return {"resolved": auth is not None, "pid": os.getpid()}
