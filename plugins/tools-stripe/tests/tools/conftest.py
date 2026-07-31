"""Fixtures for the Stripe tool tests.

The package is contract-facing and cannot import the skeleton app, so a capturing ``tai42_app``
records what each module registers when it is (re)imported, letting a test assert the tool name.
The ``stub_server`` and ``stripe_env`` fixtures stand in for the Stripe API and the interaction
callback door and for the tools' settings, so no test reaches a real Stripe host.
"""

from __future__ import annotations

import http.server
import importlib
import sys
import threading
from collections.abc import Callable, Iterator
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import pytest
from tai42_contract.app import tai42_app
from tai42_kit.settings import reset_all_settings

# What a stub responder returns: (status, response-headers, body text).
Response = tuple[int, dict[str, str], str]
Responder = Callable[[dict[str, Any]], Response]


class _CaptureTools:
    """Records every tool registered through ``tai42_app.tools.tool``."""

    def __init__(self) -> None:
        self.registered: dict[str, Callable[..., Any]] = {}

    def tool(self, func: Callable[..., Any] | None = None, /, *args: Any, **kwargs: Any) -> Any:
        def register(f: Callable[..., Any]) -> Callable[..., Any]:
            self.registered[f.__name__] = f
            return f

        return register(func) if callable(func) else register


class _CaptureApp:
    def __init__(self) -> None:
        self.tools = _CaptureTools()


@pytest.fixture
def load_registrations() -> Iterator[Callable[[str], _CaptureApp]]:
    """Return a loader that (re)imports a module under a capturing app and hands
    back the app so the test can read ``app.tools.registered``.

    The previously bound app is restored afterwards so other tests are
    unaffected.
    """
    previous = cast("Any", tai42_app)._impl

    def load(module_name: str) -> _CaptureApp:
        app = _CaptureApp()
        tai42_app.bind(app)
        if module_name in sys.modules:
            importlib.reload(sys.modules[module_name])
        else:
            importlib.import_module(module_name)
        return app

    yield load
    tai42_app.bind(previous)


class _StubHandler(http.server.BaseHTTPRequestHandler):
    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        parsed = urlparse(self.path)
        request = {
            "method": self.command,
            "path": parsed.path,
            "query": parse_qs(parsed.query),
            "headers": {key.lower(): value for key, value in self.headers.items()},
            "body": body,
        }
        server: Any = self.server
        server.requests.append(request)
        status, headers, resp_body = server.responder(request)
        data = resp_body.encode()
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        if not any(key.lower() == "content-length" for key in headers):
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _handle
    do_POST = _handle

    def log_message(self, *args: Any) -> None:
        pass


class _Stub:
    """A recording loopback server: every request lands in ``requests`` and each is answered by the
    current ``responder`` (default a fresh-claim 200), so a test can route by path or count."""

    def __init__(self, base_url: str, server: http.server.HTTPServer) -> None:
        self.base_url = base_url
        self._server = server

    @property
    def requests(self) -> list[dict[str, Any]]:
        return self._server.requests  # type: ignore[attr-defined]

    def set_responder(self, responder: Responder) -> None:
        self._server.responder = responder  # type: ignore[attr-defined]


def _default_responder(_request: dict[str, Any]) -> Response:
    return 200, {"Content-Type": "application/json"}, '{"data": {"status": "answered"}}'


@pytest.fixture
def stub_server() -> Iterator[_Stub]:
    """A real localhost server recording every request and answering via a settable responder,
    used as the Stripe API and/or the interaction callback door (routed by path)."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
    server.requests = []  # type: ignore[attr-defined]
    server.responder = _default_responder  # type: ignore[attr-defined]
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Stub(f"http://127.0.0.1:{port}", server)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def stripe_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., None]]:
    """Configure the Stripe settings through real env parsing (so empty-string cases exercise the
    same path production does) and clear the settings cache. Any ``STRIPE_*`` and the two unprefixed
    names are removed first so the ambient environment cannot leak in."""
    import os

    for key in list(os.environ):
        if key.startswith("STRIPE_") or key in ("TAI_BRIDGE_CALLBACK_SECRET", "INTERACTIONS_PUBLIC_BASE_URL"):
            monkeypatch.delenv(key, raising=False)

    def configure(
        *,
        secret_key: str | None = "sk_test_123",
        api_base: str | None = None,
        bridge_secret: str | None = "bridge-secret",
        public_base: str | None = None,
        interval: float | None = None,
    ) -> None:
        if secret_key is not None:
            monkeypatch.setenv("STRIPE_SECRET_KEY", secret_key)
        if api_base is not None:
            monkeypatch.setenv("STRIPE_API_BASE", api_base)
        if bridge_secret is not None:
            monkeypatch.setenv("TAI_BRIDGE_CALLBACK_SECRET", bridge_secret)
        if public_base is not None:
            monkeypatch.setenv("INTERACTIONS_PUBLIC_BASE_URL", public_base)
        if interval is not None:
            monkeypatch.setenv("STRIPE_RECONCILE_ANSWER_INTERVAL_SECONDS", str(interval))
        reset_all_settings()

    reset_all_settings()
    yield configure
    reset_all_settings()
