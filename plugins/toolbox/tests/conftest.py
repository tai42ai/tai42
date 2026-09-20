"""Bind a null app to the ``tai42_app`` handle before any toolbox import, and provide fixtures
shared across the tool and helper suites.

Toolbox modules register through ``tai42_app`` at import time, so a no-op app must be bound before
importing a module; individual tests rebind to a capturing or behavior-providing fake.
"""

from __future__ import annotations

import builtins
import http.server
import sys
import threading
from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest
from tai42_contract.app import tai42_app
from tai42_kit.clients import client_ctx as _kit_client_ctx


class _NullTools:
    def __init__(self) -> None:
        self.tags: dict[str, set[str]] = {}

    def tool(self, func: Callable[..., Any] | None = None, /, *args: Any, **kwargs: Any) -> Any:
        tags = kwargs.get("tags", set())

        def record(f: Callable[..., Any]) -> Callable[..., Any]:
            self.tags[f.__name__] = tags
            return f

        return record(func) if callable(func) else record


class _NullExtensions:
    def extension(
        self,
        f: Callable[..., Any] | None = None,
        *,
        kind: Any = None,
        name: str | None = None,
        requires_body_locality: bool = False,
    ) -> Any:
        if callable(f):
            return f

        def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
            return fn

        return decorate


class _NullApp:
    tools = _NullTools()
    extensions = _NullExtensions()


_null_app = _NullApp()
tai42_app.bind(_null_app)


@pytest.fixture
def null_app() -> _NullApp:
    return _null_app


class _KitClients:
    """Forwards ``client_ctx`` to tai42-kit's real pooled facade."""

    def client_ctx(self, client_cls: Any, settings: Any = None, *, fresh: bool = False, **kwargs: Any) -> Any:
        return _kit_client_ctx(client_cls, settings, fresh=fresh, **kwargs)


class _ClientsApp:
    clients = _KitClients()


@pytest.fixture
def curl_app() -> Iterator[None]:
    """Bind an app whose ``clients.client_ctx`` is tai42-kit's real pool, so the
    ``http`` tool runs against a live curl session. Restores the prior app."""
    previous = cast("Any", tai42_app)._impl
    tai42_app.bind(_ClientsApp())
    yield
    tai42_app.bind(previous)


class _ConfigurableHandler(http.server.BaseHTTPRequestHandler):
    def _respond(self) -> None:
        config = self.server.config  # type: ignore[attr-defined]
        self.server.requests.append(self.command)  # type: ignore[attr-defined]
        # Record the Cookie header each request carried so a test can prove a keyed session's
        # cookie jar persists, and hand back a cookie for the client to store.
        self.server.received_cookies.append(self.headers.get("Cookie"))  # type: ignore[attr-defined]
        location = config.get("location")
        if location:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body: bytes = config["body"]
        self.send_response(config["status"])
        self.send_header("Content-Type", config["content_type"])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "sid=session-marker")
        self.end_headers()
        self.wfile.write(body)

    do_GET = _respond
    do_POST = _respond

    def log_message(self, *args: Any) -> None:  # silence per-request logging
        pass


class _LocalServer:
    def __init__(self, base_url: str, server: http.server.HTTPServer) -> None:
        self.base_url = base_url
        self._server = server

    @property
    def requests(self) -> list[str]:
        return self._server.requests  # type: ignore[attr-defined]

    @property
    def received_cookies(self) -> list[str | None]:
        return self._server.received_cookies  # type: ignore[attr-defined]

    def configure(
        self, *, body: bytes = b"", status: int = 200, content_type: str = "text/plain", location: str | None = None
    ) -> None:
        self._server.config = {  # type: ignore[attr-defined]
            "body": body,
            "status": status,
            "content_type": content_type,
            "location": location,
        }


@pytest.fixture
def local_server() -> Iterator[_LocalServer]:
    """A real localhost HTTP server on an ephemeral port for end-to-end network
    tests (the curl client cannot be intercepted by an httpx-level mock)."""
    # Bind directly to port 0 and read the assigned port — no probe-then-rebind window to race.
    server = http.server.HTTPServer(("127.0.0.1", 0), _ConfigurableHandler)
    port = server.server_address[1]
    server.config = {"body": b"", "status": 200, "content_type": "text/plain", "location": None}  # type: ignore[attr-defined]
    server.requests = []  # type: ignore[attr-defined]
    server.received_cookies = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _LocalServer(f"http://127.0.0.1:{port}", server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _guard_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the SSRF guard for the tool tests: clear any ambient ``TAI_URL_GUARD_*`` env and
    disable the guard by default (the transport tests hit a loopback server it would block).
    Guard-specific tests re-enable it with their own ``url_guard_settings`` monkeypatch."""
    import os

    from tai42_kit.net import url_guard

    for key in list(os.environ):
        if key.startswith("TAI_URL_GUARD_"):
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setattr(url_guard, "url_guard_settings", lambda: url_guard.UrlGuardSettings(enabled=False))


@pytest.fixture
def force_missing_import(monkeypatch: pytest.MonkeyPatch) -> Callable[[str, list[str]], None]:
    """Return a helper that simulates a missing backing dependency.

    It makes importing ``dep`` (and its submodules) raise ``ImportError`` and drops the given
    modules from the import cache so they re-execute, restoring them on teardown.
    """

    def force(dep: str, reset_modules: list[str]) -> None:
        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == dep or name.startswith(f"{dep}."):
                raise ImportError(f"forced-missing: {dep}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        for module_name in reset_modules:
            monkeypatch.delitem(sys.modules, module_name, raising=False)

    return force
