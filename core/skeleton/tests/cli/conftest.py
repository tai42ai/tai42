"""Shared fixtures and fakes for the CLI tests.

``_restore_environ`` keeps the launcher's direct ``os.environ`` writes hermetic; the
fakes stand in for the app, its lifespan, and uvicorn so the MCP-serve launch paths
run without a real server; ``spec`` / ``api_routes`` / ``_operation`` back the OpenAPI
emission tests.
"""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import tai42_skeleton.cli.mcp_app as mcp_app
from tai42_skeleton.app.route_registry import RouteMetadata, load_api_routes
from tai42_skeleton.cli.openapi import _openapi_path, build_openapi_spec


@pytest.fixture(autouse=True)
def _restore_environ() -> Iterator[None]:
    saved = os.environ.copy()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


_HTTP_SCOPE = {
    "type": "http",
    "http_version": "1.1",
    "method": "GET",
    "path": "/",
    "raw_path": b"/",
    "query_string": b"",
    "headers": [],
    "scheme": "http",
    "server": ("testserver", 80),
    "client": ("testclient", 1234),
}


# --- fakes for the app / manifest seams -----------------------------------


class _FakeConfigManager:
    def __init__(self, raise_on_read: bool = False) -> None:
        self.raise_on_read = raise_on_read

    def read_manifest(self) -> dict:
        if self.raise_on_read:
            raise RuntimeError("manifest read failed")
        return {}


class _FakeInnerApp:
    """ASGI stand-in for ``app.http_app()`` / ``app.sse_app()``."""

    def __init__(self, *, raise_on_call: bool = False, with_lifespan: bool = True) -> None:
        self.raise_on_call = raise_on_call
        self.lifespan_entered = False
        self.calls = 0
        if with_lifespan:
            # Mirrors ``StarletteWithLifespan.lifespan`` (a callable taking the app
            # and returning the lifespan context manager), which the worker enters.
            self.lifespan = self._lifespan_context

    @asynccontextmanager
    async def _lifespan_context(self, _app):
        self.lifespan_entered = True
        yield

    async def __call__(self, scope, receive, send) -> None:
        self.calls += 1
        if self.raise_on_call:
            raise RuntimeError("inner boom")
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})


class _FakeLifecycle:
    """Records reload-handler registrations by their ``module.qualname`` key, so a
    test can assert ``register_cli_logging_reload`` wired ``apply_logging_settings``,
    and stands in for the cold-boot manifest read the door delegates here — mirroring
    the real seam, it reads through the config manager so a failing read still
    propagates."""

    def __init__(self, config_manager: _FakeConfigManager) -> None:
        self.reload_handlers: dict[str, object] = {}
        self._config_manager = config_manager

    def on_reload(self, func):
        self.reload_handlers[f"{func.__module__}.{func.__qualname__}"] = func
        return func

    def read_boot_manifest(self):
        self._config_manager.read_manifest()
        return SimpleNamespace()


class _FakeApp:
    def __init__(self, inner: _FakeInnerApp, *, raise_on_read: bool = False) -> None:
        self.inner = inner
        # The config manager is reached through the ``config`` facet namespace.
        self.config = SimpleNamespace(config_manager=_FakeConfigManager(raise_on_read=raise_on_read))
        # The CLI seams register the root-logger reload handler through the app's
        # lifecycle; the recorder captures it without a real lifecycle.
        self.lifecycle = _FakeLifecycle(self.config.config_manager)
        self.http_called = False
        self.http_stateless: bool | None = None
        self.sse_called = False
        self.run_async_transport: str | None = None

    @asynccontextmanager
    async def app_context(self, manifest):
        # Mirror the real app_context: install boot epoch 0's core so the worker
        # lifespan reads the epoch and attaches the serving app, and drop it on exit.
        from tai42_skeleton.app import epoch

        epoch.install_boot_core(SimpleNamespace())  # type: ignore[arg-type]
        try:
            yield
        finally:
            await epoch.clear_epoch()

    def http_app(self, stateless_http: bool | None = None) -> _FakeInnerApp:
        self.http_called = True
        self.http_stateless = stateless_http
        return self.inner

    def sse_app(self) -> _FakeInnerApp:
        self.sse_called = True
        return self.inner

    async def run_async(self, transport: str) -> None:
        self.run_async_transport = transport


@pytest.fixture
def patch_app_seam(monkeypatch: pytest.MonkeyPatch):
    """Install a fake ``app``; its ``lifecycle.read_boot_manifest`` stands in for the
    cold-boot manifest read the door delegates there."""

    def _install(app: _FakeApp) -> _FakeApp:
        # The launcher obtains the app via the deferred factory ``instance.build_app``.
        monkeypatch.setattr(mcp_app.instance, "build_app", lambda: app)
        return app

    return _install


# --- create_app: worker lifespan + dispatch -------------------------------


class _FakeUvicorn:
    def __init__(self) -> None:
        self.config_kwargs: dict | None = None
        self.served = False

    def Config(self, **kwargs):
        self.config_kwargs = kwargs
        return ("config", kwargs)

    def Server(self, config):
        outer = self

        class _Server:
            async def serve(self) -> None:
                outer.served = True

        return _Server()


def _stale_uds_socket(path: str) -> None:
    """Create a socket-typed path with no listener — a connect refuses it."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.bind(path)  # the filesystem entry persists; nothing is listening on it


@pytest.fixture
def uds_dir():
    """A short-path directory for AF_UNIX sockets: ``tmp_path`` nests test-name
    subdirs that can push a socket path past the ~104-char ``sun_path`` limit, so
    bind under a shorter ``mkdtemp`` base instead."""
    directory = tempfile.mkdtemp()
    try:
        yield Path(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(scope="module")
def spec() -> dict:
    return build_openapi_spec()


@pytest.fixture(scope="module")
def api_routes() -> list[RouteMetadata]:
    return load_api_routes()


def _operation(spec: dict, meta: RouteMetadata, method: str) -> dict:
    # Normalize the Starlette path to its OpenAPI form exactly as the emitter does
    # (dropping the ``:path`` converter from any param), so a ``{name:path}`` route
    # is matched however it is named.
    oapath = _openapi_path(meta.path)
    assert oapath in spec["paths"], f"route {meta.path} missing from spec"
    op = spec["paths"][oapath].get(method.lower())
    assert op is not None, f"{method} {meta.path} missing from spec"
    return op
