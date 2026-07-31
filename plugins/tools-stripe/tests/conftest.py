"""Bind a null app to the ``tai42_app`` handle before any package import, and provide fixtures
shared across the tool and helper suites.

The Stripe tools register through ``tai42_app`` at import time, so a no-op app must be bound before
importing a module; individual tests rebind to a capturing or behavior-providing fake.
"""

from __future__ import annotations

import http.server
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


class _NullApp:
    tools = _NullTools()


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
    """Bind an app whose ``clients.client_ctx`` is tai42-kit's real pool, so the Stripe tools run
    against a live curl session. Restores the prior app."""
    previous = cast("Any", tai42_app)._impl
    tai42_app.bind(_ClientsApp())
    yield
    tai42_app.bind(previous)


class _ConfigurableHandler(http.server.BaseHTTPRequestHandler):
    def _respond(self) -> None:
        config = self.server.config  # type: ignore[attr-defined]
        self.server.requests.append(self.command)  # type: ignore[attr-defined]
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
    """A real localhost HTTP server on an ephemeral port for the redirect-target tests (the curl
    client cannot be intercepted by an httpx-level mock)."""
    # Bind directly to port 0 and read the assigned port — no probe-then-rebind window to race.
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


@pytest.fixture(autouse=True)
def _guard_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the SSRF guard for the tool tests: clear any ambient ``TAI_URL_GUARD_*`` env and
    disable the guard by default (the Stripe calls hit a loopback stub it would block)."""
    import os

    from tai42_kit.net import url_guard

    for key in list(os.environ):
        if key.startswith("TAI_URL_GUARD_"):
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setattr(url_guard, "url_guard_settings", lambda: url_guard.UrlGuardSettings(enabled=False))
