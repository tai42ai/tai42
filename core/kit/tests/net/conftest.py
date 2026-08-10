"""Fixtures for the SSRF guard + ``fetch_url`` tests.

A real localhost HTTP server on an ephemeral port drives the pinning download
end to end (the pinning transport connects to a validated address, so an
httpx-level mock cannot exercise it). Ambient ``TAI_URL_GUARD_*`` env is cleared
by the root ``tests/conftest.py`` fixture, so a test's injected policy is the
only input to the guard.
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest


class _ConfigurableHandler(http.server.BaseHTTPRequestHandler):
    def _respond(self) -> None:
        config = self.server.config  # type: ignore[attr-defined]
        self.server.requests.append(self.command)  # type: ignore[attr-defined]
        body: bytes = config["body"]
        location = config.get("location")
        if location:
            # A redirect may carry a body; ``fetch_url`` must never read it.
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(config["status"])
        self.send_header("Content-Type", config["content_type"])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _respond

    def log_message(self, *args: Any) -> None:  # silence per-request logging
        pass


class _LocalServer:
    def __init__(self, base_url: str, server: http.server.HTTPServer) -> None:
        self.base_url = base_url
        self._server = server

    @property
    def requests(self) -> list[str]:
        return self._server.requests  # type: ignore[attr-defined]

    def configure(
        self, *, body: bytes = b"", status: int = 200, content_type: str = "text/plain", location: str | None = None
    ) -> None:
        self._server.config = {  # type: ignore[attr-defined]
            "body": body,
            "status": status,
            "content_type": content_type,
            "location": location,
        }


def _serve() -> Iterator[_LocalServer]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _ConfigurableHandler)
    port = server.server_address[1]
    server.config = {"body": b"", "status": 200, "content_type": "text/plain", "location": None}  # type: ignore[attr-defined]
    server.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _LocalServer(f"http://127.0.0.1:{port}", server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def local_server() -> Iterator[_LocalServer]:
    """A real localhost HTTP server on an ephemeral port for the download tests."""
    yield from _serve()


@pytest.fixture
def redirect_target() -> Iterator[_LocalServer]:
    """A second loopback server, so a redirect chain has a distinct hop to land on."""
    yield from _serve()


class _OidcHandler(http.server.BaseHTTPRequestHandler):
    """Serve per-path OIDC responses (discovery, JWKS) configured by the test."""

    def do_GET(self) -> None:  # http.server dispatch name
        routes: dict[str, dict[str, Any]] = self.server.routes  # type: ignore[attr-defined]
        self.server.requests.append(self.path)  # type: ignore[attr-defined]
        route = routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        location = route.get("location")
        if location:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body: bytes = route["body"]
        self.send_response(route.get("status", 200))
        self.send_header("Content-Type", route.get("content_type", "application/json"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # silence per-request logging
        pass


class _OidcServer:
    """A loopback OIDC issuer whose responses each test configures per path."""

    def __init__(self, base_url: str, server: http.server.HTTPServer) -> None:
        self.base_url = base_url
        self._server = server

    @property
    def requests(self) -> list[str]:
        return self._server.requests  # type: ignore[attr-defined]

    def set_route(
        self,
        path: str,
        *,
        body: bytes = b"",
        status: int = 200,
        content_type: str = "application/json",
        location: str | None = None,
    ) -> None:
        self._server.routes[path] = {  # type: ignore[attr-defined]
            "body": body,
            "status": status,
            "content_type": content_type,
            "location": location,
        }

    def set_json(self, path: str, obj: Any) -> None:
        self.set_route(path, body=json.dumps(obj).encode())


def _serve_oidc() -> Iterator[_OidcServer]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _OidcHandler)
    port = server.server_address[1]
    server.routes = {}  # type: ignore[attr-defined]
    server.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _OidcServer(f"http://127.0.0.1:{port}", server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def oidc_server() -> Iterator[_OidcServer]:
    """A real loopback OIDC issuer serving test-configured discovery + JWKS."""
    yield from _serve_oidc()
