"""Protocol fixtures for the OAuth discovery and seam tests: one loopback HTTP server that plays an MCP
server and its authorization server, recording every request it receives.

The server is a protocol stand-in (RFC 9728 / RFC 8414 / RFC 6749 shapes), not any vendor. Routes are
keyed ``(method, path)``; a route answers with a fixed response or with the next one of a queue.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit


@dataclass
class Reply:
    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    # Seconds to wait before answering (the whole answer, headers included).
    delay: float = 0.0
    raw: bytes | None = None
    # Called on the server thread after the request is recorded and before the answer (a test holds or
    # synchronizes the answer here).
    hold: Callable[[], object] | None = None


@dataclass
class Received:
    method: str
    path: str
    query: str
    headers: dict[str, str]
    body: bytes

    @property
    def form(self) -> dict[str, str]:
        return dict(parse_qsl(self.body.decode("ascii")))

    @property
    def json(self) -> Any:
        return json.loads(self.body)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _serve(self) -> None:
        server: Any = self.server
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        split = urlsplit(self.path)
        received = Received(
            method=self.command,
            path=split.path,
            query=split.query,
            headers={k.lower(): v for k, v in self.headers.items()},
            body=body,
        )
        with server.lock:
            server.received.append(received)
            route = server.routes.get((self.command, split.path))
            reply = (route.pop(0) if route else Reply(status=599)) if isinstance(route, list) else route
        if reply is None:
            reply = Reply(status=404, body={"error": "not_found"})
        if reply.hold is not None:
            reply.hold()
        if reply.delay:
            time.sleep(reply.delay)
        payload = (
            reply.raw if reply.raw is not None else (b"" if reply.body is None else json.dumps(reply.body).encode())
        )
        self.send_response(reply.status)
        if reply.body is not None and reply.raw is None:
            self.send_header("Content-Type", "application/json")
        for key, value in reply.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _serve
    do_POST = _serve
    do_DELETE = _serve

    def log_message(self, *args: Any) -> None:
        pass


class FakeOAuthServer:
    """A loopback server whose routes each test sets; ``received`` lists every request."""

    def __init__(self, server: Any) -> None:
        self._server = server
        self.base = f"http://127.0.0.1:{server.server_address[1]}"

    @property
    def received(self) -> list[Received]:
        return self._server.received

    def requests_to(self, path: str, method: str | None = None) -> list[Received]:
        return [r for r in self.received if r.path == path and (method is None or r.method == method)]

    def route(self, method: str, path: str, reply: Reply | list[Reply]) -> None:
        with self._server.lock:
            self._server.routes[(method, path)] = reply

    def json(self, path: str, body: Any, *, status: int = 200) -> None:
        self.route("GET", path, Reply(status=status, body=body))

    def clear(self) -> None:
        with self._server.lock:
            self._server.received.clear()

    # -- standard shapes ---------------------------------------------------------------

    def protected_mcp(self, mcp_path: str, *, pointer: str | None, scope: str | None = None) -> None:
        """An MCP endpoint that answers an unauthenticated request 401 with a Bearer challenge."""
        params = []
        if pointer is not None:
            params.append(f'resource_metadata="{pointer}"')
        if scope is not None:
            params.append(f'scope="{scope}"')
        challenge = "Bearer " + ", ".join(params) if params else 'Bearer realm="mcp"'
        self.route(
            "POST", mcp_path, Reply(status=401, body={"error": "unauthorized"}, headers={"WWW-Authenticate": challenge})
        )

    def authorization_server(
        self,
        issuer_path: str = "",
        *,
        well_known: str | None = None,
        pkce: list[str] | None = None,
        issuer: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Publish RFC 8414 metadata for the issuer ``self.base + issuer_path`` with this server's endpoints."""
        issuer_value = issuer if issuer is not None else f"{self.base}{issuer_path}"
        metadata: dict[str, Any] = {
            "issuer": issuer_value,
            "authorization_endpoint": f"{self.base}{issuer_path}/authorize",
            "token_endpoint": f"{self.base}{issuer_path}/token",
            "code_challenge_methods_supported": ["S256"] if pkce is None else pkce,
        }
        metadata.update(extra)
        path = well_known if well_known is not None else f"/.well-known/oauth-authorization-server{issuer_path}"
        self.json(path, metadata)
        return metadata


def serve() -> Iterator[FakeOAuthServer]:
    srv: Any = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.routes = {}
    srv.received = []
    srv.lock = threading.Lock()
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield FakeOAuthServer(srv)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)
