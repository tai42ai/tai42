"""The assembled app served for real: ``create_app`` under uvicorn on a loopback port.

A session is driven by the pinned SDK's own MCP clients, and a single request is written
to the socket as given, so every request crosses the real ASGI stack — the access-control
middleware, the router, the sub-MCP mount and the transports — exactly as a deployment
serves it. The access-control gate is ON with the real verifier over in-memory stores: an
administrator key is provisioned and no route row exists, so every transport path
resolves only through the route registry's own records.

A host application that mounts the platform under a prefix (the documented embed) is
modelled by a Starlette app that mounts ``create_app`` and runs its lifespan.
"""

from __future__ import annotations

import asyncio
import gc
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from sse_starlette.sse import AppStatus
from starlette.applications import Starlette
from starlette.routing import Mount
from tai42_contract.access_control import OWNER_USER_ID_CLAIM
from tai42_identity_redis import redis_api_key_provider as provider_module
from tai42_identity_redis.settings import redis_identity_settings
from tai42_kit.utils.data.string_util import hash_api_key

from tai42_skeleton import asgi
from tai42_skeleton.access_control import policy as policy_module
from tai42_skeleton.access_control import store as store_module
from tai42_skeleton.app.instance import app
from tai42_skeleton.app.sub_mcp_app import SubMcpAppRouter
from tai42_skeleton.manifest import Manifest

from ..access_control.conftest import FakeAccessControlPg, FakeRedis, make_client_ctx, make_pg_ctx

ADMIN_KEY = "admin-raw-key"

# The tool every served manifest registers, so a session has something to list.
TOOL_MANIFEST_ENTRY = {"title": "fxt", "module": "tests.app._fixtures.tools_a", "include": ["greet"]}
TOOL_NAME = "greet"

# Every test request runs under this bound: a guard that is missing lets a GET open a
# stream that never ends, which must fail the test instead of hanging it.
REQUEST_TIMEOUT_S = 10.0


def _identity(raw_key: str, key_id: str, owner_id: str) -> tuple[str, dict[str, str]]:
    identity_key = f"{redis_identity_settings().key_prefix}{hash_api_key(raw_key)}"
    return identity_key, {"user_id": key_id, "description": "d", "owner_user_id": owner_id}


def install_access_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provision an administrator key over in-memory identity and policy stores.

    The admin's key and owner carry the universal scope with no condition. No route row is stored.
    """
    fake = FakeRedis(hashes=dict([_identity(ADMIN_KEY, "ad-key", "ad-owner")]))
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "test")
    pg = FakeAccessControlPg()
    pg.add_policy("ad-key", scopes=["*"], policy_data={OWNER_USER_ID_CLAIM: "ad-owner"})
    pg.add_policy("ad-owner", scopes=["*"])
    ctx = make_client_ctx(fake)
    monkeypatch.setattr(policy_module, "client_ctx", ctx)
    monkeypatch.setattr(provider_module, "client_ctx", ctx)
    monkeypatch.setattr(store_module, "client_ctx", make_pg_ctx(pg))


def manifest(default_routers: str) -> dict[str, Any]:
    """The served manifest: one tool, the given router set."""
    return {"default_routers": default_routers, "tools": [TOOL_MANIFEST_ENTRY]}


@dataclass
class Served:
    """A running server: the base URL a client dials (the host prefix included)."""

    base_url: str
    origin: str


@asynccontextmanager
async def served(
    monkeypatch: pytest.MonkeyPatch,
    manifest_body: dict[str, Any],
    *,
    transport: asgi.Transport = "http",
    stateless_http: bool = False,
    prefix: str = "",
) -> AsyncIterator[Served]:
    """Serve ``create_app`` for ``manifest_body`` on a loopback port and yield its base URL.

    With ``prefix`` the app is mounted in a host application under that path, the host
    running the platform's lifespan, as the embed documents it.
    """
    boot_manifest = Manifest.model_validate(manifest_body)
    # A fresh sub-MCP router per served app: the process router caches sub-apps whose
    # lifespans run on the loop that built them, which ends with the test.
    monkeypatch.setattr(app, "_mcp_sub_app_router", SubMcpAppRouter(app=app))
    # ``sse_starlette`` ends every SSE response once it has seen a uvicorn server exit, through
    # a process-wide flag it never clears; each served app starts with it cleared.
    monkeypatch.setattr(AppStatus, "should_exit", False)
    monkeypatch.setattr(app.lifecycle, "read_boot_manifest", lambda: boot_manifest)
    monkeypatch.setattr(app.config.config_manager, "read_manifest", lambda: manifest_body)
    platform = asgi.create_app(transport=transport, stateless_http=stateless_http)
    if prefix:

        @asynccontextmanager
        async def host_lifespan(_host: Starlette) -> AsyncIterator[None]:
            async with asgi.lifespan(platform):
                yield

        served_app: Any = Starlette(routes=[Mount(prefix, app=platform)], lifespan=host_lifespan)
    else:
        served_app = platform
    config = uvicorn.Config(
        served_app,
        host="127.0.0.1",
        port=0,
        lifespan="on",
        log_level="warning",
        timeout_graceful_shutdown=1,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                task.result()
                raise RuntimeError("the served app stopped before it started")
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        yield Served(base_url=f"{origin}{prefix}", origin=origin)
    finally:
        server.should_exit = True
        await task
        gc.collect()


@dataclass
class ResponseLog:
    """Every response a client saw in a session, redirects included, as ``(method, path, status, content-type)``."""

    entries: list[tuple[str, str, int, str]] = field(default_factory=list)

    async def record(self, response: httpx.Response) -> None:
        self.entries.append(
            (
                response.request.method,
                response.request.url.path,
                response.status_code,
                response.headers.get("content-type", ""),
            )
        )

    def get_streams(self) -> list[tuple[str, str, int, str]]:
        return [entry for entry in self.entries if entry[0] == "GET"]


def logging_client_factory(log: ResponseLog) -> Callable[..., httpx.AsyncClient]:
    """The SDK's own HTTP-client factory with a hook recording every response it receives."""

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        client = create_mcp_http_client(headers=headers, timeout=timeout, auth=auth)
        client.event_hooks["response"].append(log.record)
        return client

    return factory


def bearer(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


async def streamable_session_tools(url: str, headers: dict[str, str], log: ResponseLog) -> list[str]:
    """Open a streamable-HTTP session at ``url``, list its tools, and wait for the GET stream's answer."""
    client = logging_client_factory(log)(headers=headers, timeout=httpx.Timeout(REQUEST_TIMEOUT_S))
    async with (
        asyncio.timeout(REQUEST_TIMEOUT_S),
        client,
        streamable_http_client(url, http_client=client) as (read, write, session_id),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        # A stateful session's SDK client opens its GET stream only after ``initialized`` and
        # treats a refusal as non-fatal, so wait until the GET's answer has been seen. A
        # stateless endpoint issues no session and the client opens no stream.
        while session_id() is not None and not log.get_streams():
            await asyncio.sleep(0.01)
        return [tool.name for tool in listed.tools]


async def sse_session_tools(url: str, headers: dict[str, str], log: ResponseLog) -> list[str]:
    """Open an SSE session at ``url`` and list its tools."""
    async with asyncio.timeout(REQUEST_TIMEOUT_S):
        async with (
            sse_client(
                url, headers=headers, timeout=REQUEST_TIMEOUT_S, httpx_client_factory=logging_client_factory(log)
            ) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            listed = await session.list_tools()
            return [tool.name for tool in listed.tools]


async def raw_request(
    origin: str, method: str, target: str, *, body: bytes = b"", headers: dict[str, str] | None = None
):
    """Send one HTTP/1.1 request with ``target`` on the wire exactly as given (no client-side path normalisation).

    Returns ``(status, headers, body)``.
    """
    host_port = origin.removeprefix("http://")
    host, port = host_port.split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    lines = [f"{method} {target} HTTP/1.1", f"Host: {host_port}", "Connection: close", f"Content-Length: {len(body)}"]
    lines.extend(f"{name}: {value}" for name, value in (headers or {}).items())
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body)
    await writer.drain()
    async with asyncio.timeout(REQUEST_TIMEOUT_S):
        raw = await reader.read()
    writer.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    status = int(status_line.split(" ")[1])
    parsed = {}
    for line in header_lines:
        name, _, value = line.partition(":")
        parsed[name.strip().lower()] = value.strip()
    return status, parsed, payload


INITIALIZE_BODY = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
).encode()

MCP_POST_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
