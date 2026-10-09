"""A real loopback MCP server for the credential-path tests: a FastMCP app behind an auth gate.

The gate answers each HTTP request by a per-test policy over its ``Authorization`` header — serve it,
refuse it ``401`` (a Bearer challenge), or refuse it ``403`` with an ``insufficient_scope`` challenge —
and records every request, so a test counts connection attempts and the credentials that reached the
server. The server is a protocol stand-in, not any vendor.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import uvicorn
from fastmcp import FastMCP
from starlette.responses import Response

# A policy maps the bearer token a request carries (``None`` when it carries none) to the refusal
# to answer with, or ``None`` to serve the request.
Policy = Callable[[str | None], "Refusal | None"]


@dataclass(frozen=True)
class Refusal:
    status: int
    challenge: str


UNAUTHORIZED = Refusal(401, 'Bearer realm="mcp"')


def insufficient_scope(scope: str | None) -> Refusal:
    params = ['error="insufficient_scope"']
    if scope is not None:
        params.append(f'scope="{scope}"')
    return Refusal(403, "Bearer " + ", ".join(params))


def anonymous(_token: str | None) -> Refusal | None:
    return None


def bearer_only(*accepted: str) -> Policy:
    """Serve only a request carrying one of ``accepted``; refuse every other one ``401``."""

    def policy(token: str | None) -> Refusal | None:
        return None if token in accepted else UNAUTHORIZED

    return policy


@dataclass
class Seen:
    method: str
    rpc_method: str | None
    authorization: str | None


class FakeMcpServer:
    """The running server: set ``policy``; read ``seen`` / ``initializes``."""

    def __init__(self, port: int) -> None:
        self.url = f"http://127.0.0.1:{port}/mcp"
        self.policy: Policy = anonymous
        self.seen: list[Seen] = []
        self._lock = threading.Lock()

    def reset(self, policy: Policy = anonymous) -> None:
        with self._lock:
            self.policy = policy
            self.seen.clear()

    @property
    def initializes(self) -> list[Seen]:
        """Every ``initialize`` request: one per connection attempt."""
        with self._lock:
            return [s for s in self.seen if s.rpc_method == "initialize"]

    @property
    def authorizations(self) -> list[str | None]:
        with self._lock:
            return [s.authorization for s in self.seen]

    def record(self, seen: Seen) -> Policy:
        with self._lock:
            self.seen.append(seen)
            return self.policy


def _build_app(server_ref: dict[str, FakeMcpServer]) -> Any:
    mcp = FastMCP("credential-path-fake")

    @mcp.tool
    def ping() -> str:
        return "pong"

    inner = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/mcp"):
            await inner(scope, receive, send)
            return
        messages = []
        body = b""
        while True:
            message = await receive()
            messages.append(message)
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        rpc_method = None
        if body:
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                rpc_method = payload.get("method")
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        authorization = headers.get("authorization")
        token = authorization.removeprefix("Bearer ").strip() if authorization else None
        policy = server_ref["server"].record(Seen(scope["method"], rpc_method, authorization))
        refusal = policy(token)
        if refusal is not None:
            await Response(status_code=refusal.status, headers={"WWW-Authenticate": refusal.challenge})(
                scope, receive, send
            )
            return

        replay = iter(messages)

        async def replay_receive() -> Any:
            try:
                return next(replay)
            except StopIteration:
                return await receive()

        await inner(scope, replay_receive, send)

    return app


def serve() -> Iterator[FakeMcpServer]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    ref: dict[str, FakeMcpServer] = {"server": FakeMcpServer(port)}
    config = uvicorn.Config(_build_app(ref), host="127.0.0.1", port=port, log_level="error", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the fake MCP server did not start")
        time.sleep(0.02)
    try:
        yield ref["server"]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
