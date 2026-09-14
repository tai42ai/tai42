"""The MCP-serve CLI: the create_app worker (lifespan + dispatch), run_stdio /
run_debug, the logging-reload seam, and stale-UDS preparation.

The launcher wiring runs up to (but not through) the blocking server start —
``uvicorn.run`` / ``uvicorn.Server.serve`` / ``asyncio.run`` are mocked, and the
``app`` / ``Manifest`` seams are replaced with fakes — so the unit under test is
the CLI wiring, not the real MCP app.
"""

from __future__ import annotations

import logging
import os
import socket

import click
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

import tai42_skeleton.cli.mcp_app as mcp_app
from tai42_skeleton.connectors import meta_log_redactor

from .conftest import (  # noqa: F401
    _HTTP_SCOPE,
    _FakeApp,
    _FakeConfigManager,
    _FakeInnerApp,
    _FakeLifecycle,
    _FakeUvicorn,
    _stale_uds_socket,
)

_LOGGING_RELOAD_KEY = "tai42_skeleton.app.instance.apply_logging_settings"


@pytest.fixture(autouse=True)
def _restore_log_record_factory():
    """Save/restore the process-global record factory and its monotonic redaction
    scope around every test: the CLI seams install the connector-secret redactor at
    process scope, so this keeps one test's install from leaking onward."""
    saved_factory = logging.getLogRecordFactory()
    saved_scope = meta_log_redactor._SCOPE
    try:
        yield
    finally:
        logging.setLogRecordFactory(saved_factory)
        meta_log_redactor._SCOPE = saved_scope


@pytest.fixture(autouse=True)
def _bus_configured(monkeypatch: pytest.MonkeyPatch):
    """These serve-path tests exercise multi-worker runs, which the boot rules require
    the worker bus for. Report it configured so the launch wiring under test runs
    (nothing here opens a real bus — uvicorn.run and the in-process servers are faked)."""
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("TAI_BUS_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()
    try:
        yield
    finally:
        reset_all_settings()


def test_create_app_http_lifespan_and_dispatch(patch_app_seam, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_TRANSPORT", "http")
    inner = _FakeInnerApp()
    app = patch_app_seam(_FakeApp(inner))

    star = mcp_app.create_app()
    with TestClient(star) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.text == "ok"
    assert app.http_called is True
    assert app.sse_called is False
    assert inner.lifespan_entered is True


def test_create_app_sse_transport_selects_sse(patch_app_seam, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_TRANSPORT", "sse")
    inner = _FakeInnerApp()
    app = patch_app_seam(_FakeApp(inner))

    star = mcp_app.create_app()
    with TestClient(star) as client:
        client.get("/")

    assert app.sse_called is True
    assert app.http_called is False


def test_create_app_inner_without_lifespan_context(patch_app_seam, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_TRANSPORT", "http")
    inner = _FakeInnerApp(with_lifespan=False)
    patch_app_seam(_FakeApp(inner))

    star = mcp_app.create_app()
    with TestClient(star) as client:
        response = client.get("/")

    assert response.status_code == 200


def test_create_app_enters_lifespan_via_mcp_lifespan_app_when_wrapped(
    patch_app_seam, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``@app.http.middleware`` wraps the served app, ``http_app()`` returns
    a plain-ASGI wrapper with no lifespan of its own. ``finalize`` records the
    lifespan-bearing FastMCP app as ``mcp_lifespan_app``; the worker must enter
    THAT lifespan, then still handle the request through the wrapper — otherwise
    the streamable-http session-manager task group never starts."""
    monkeypatch.setenv("TAI_TRANSPORT", "http")
    inner = _FakeInnerApp()  # the lifespan-bearing FastMCP app

    class _PassThroughMiddleware:
        """Records that it ran, exposes no lifespan — like a finalized wrapper."""

        def __init__(self, app) -> None:
            self._app = app
            self.mcp_lifespan_app = app
            self.saw_request = False

        async def __call__(self, scope, receive, send) -> None:
            self.saw_request = True
            await self._app(scope, receive, send)

    wrapped = _PassThroughMiddleware(inner)
    app = _FakeApp(inner)
    monkeypatch.setattr(app, "http_app", lambda: wrapped)
    patch_app_seam(app)

    star = mcp_app.create_app()
    with TestClient(star) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.text == "ok"
    # The FastMCP lifespan was entered via ``mcp_lifespan_app`` despite the wrapper.
    assert inner.lifespan_entered is True
    # ...and the request still flowed through the middleware wrapper.
    assert wrapped.saw_request is True


def test_dispatch_forwards_inner_error_as_500(patch_app_seam, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_TRANSPORT", "http")
    inner = _FakeInnerApp(raise_on_call=True)
    patch_app_seam(_FakeApp(inner))

    star = mcp_app.create_app()
    with TestClient(star, raise_server_exceptions=False) as client:
        response = client.get("/")

    assert response.status_code == 500
    assert response.json()["error"] == "Internal Server Error"


def test_worker_lifespan_reraises_init_failure(patch_app_seam, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_TRANSPORT", "http")
    inner = _FakeInnerApp()
    patch_app_seam(_FakeApp(inner, raise_on_read=True))

    star = mcp_app.create_app()
    with pytest.raises(RuntimeError, match="manifest read failed"), TestClient(star):
        pass


async def test_dispatch_swallows_double_fault_when_response_started(
    patch_app_seam, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The inner app starts a response, then raises. The dispatch error handler
    # tries to send its own 500, but the ASGI stream already began → the second
    # ``http.response.start`` raises RuntimeError, which the handler swallows.
    monkeypatch.setenv("TAI_TRANSPORT", "http")

    class _StartThenRaiseInner(_FakeInnerApp):
        async def __call__(self, scope, receive, send) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            raise RuntimeError("inner boom after start")

    patch_app_seam(_FakeApp(_StartThenRaiseInner()))
    star = mcp_app.create_app()
    # Running the worker lifespan once populates ``app_state["app"]`` (the dict
    # in the dispatch closure persists after shutdown), so the dispatch call
    # below forwards to the inner app.
    with TestClient(star, raise_server_exceptions=False):
        pass
    mount = star.routes[0]
    assert isinstance(mount, Mount)  # create_app builds a single Mount("/", app=dispatch)
    dispatch = mount.app

    started = {"flag": False}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            if started["flag"]:
                raise RuntimeError("Response already started")
            started["flag"] = True

    # The error handler's send of the 500 hits the already-started stream and is
    # swallowed; the call returns without propagating.
    await dispatch(dict(_HTTP_SCOPE), receive, send)
    assert started["flag"] is True


async def test_dispatch_service_unavailable_before_init(patch_app_seam, monkeypatch: pytest.MonkeyPatch) -> None:
    # Reach the dispatch closure directly, without running the lifespan, so the
    # inner app is not yet wired → 503. Also drives the lifespan-scope early
    # return, which Starlette never forwards to a mounted sub-app.
    monkeypatch.setenv("TAI_TRANSPORT", "http")
    patch_app_seam(_FakeApp(_FakeInnerApp()))
    mount = mcp_app.create_app().routes[0]
    assert isinstance(mount, Mount)  # create_app builds a single Mount("/", app=dispatch)
    dispatch = mount.app

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    lifespan_sent: list = []

    async def send_lifespan(message):
        lifespan_sent.append(message)

    await dispatch({"type": "lifespan"}, receive, send_lifespan)
    assert lifespan_sent == []  # early return, nothing emitted

    http_sent: list = []

    async def send_http(message):
        http_sent.append(message)

    await dispatch(dict(_HTTP_SCOPE), receive, send_http)
    start = next(m for m in http_sent if m["type"] == "http.response.start")
    assert start["status"] == 503


# --- run_stdio / run_debug ------------------------------------------------


async def test_run_stdio_enters_context_and_runs(patch_app_seam) -> None:
    app = patch_app_seam(_FakeApp(_FakeInnerApp()))

    rc = await mcp_app.run_stdio()

    assert rc == 0
    assert app.run_async_transport == "stdio"


async def test_run_stdio_refuses_profile_apply_loudly(patch_app_seam) -> None:
    """stdio has no swappable serving surface and no fleet, so a profile apply /
    config reload is refused loudly. run_stdio wires the refusal as a reload handler;
    the epoch rebuild runs reload handlers, so the raise discards the build and leaves
    the running server serving."""
    app = patch_app_seam(_FakeApp(_FakeInnerApp()))

    await mcp_app.run_stdio()

    key = f"{mcp_app._refuse_stdio_profile_apply.__module__}.{mcp_app._refuse_stdio_profile_apply.__qualname__}"
    assert key in app.lifecycle.reload_handlers
    with pytest.raises(RuntimeError, match="stdio"):
        app.lifecycle.reload_handlers[key]()


async def test_run_debug_routes_through_create_app(patch_app_seam, monkeypatch: pytest.MonkeyPatch) -> None:
    """The debug run serves the SAME factory app as the multi-worker path — the shim
    Starlette (a single ``Mount("/")`` dispatch) built by ``create_app``, NOT a
    hand-built ``http_app`` that bypasses the epoch machinery. So a debug run gets the
    boot-epoch install + swap slot at lifespan time."""
    monkeypatch.setenv("TAI_TRANSPORT", "http")
    monkeypatch.delenv("TAI_STATELESS_HTTP", raising=False)
    patch_app_seam(_FakeApp(_FakeInnerApp()))
    fake_uvicorn = _FakeUvicorn()
    monkeypatch.setattr(mcp_app, "uvicorn", fake_uvicorn)

    config_kwargs: dict = {"host": "127.0.0.1", "port": 8000}
    rc = await mcp_app.run_debug(config_kwargs)

    assert rc == 0
    served_app = config_kwargs["app"]
    assert isinstance(served_app, Starlette)
    assert isinstance(served_app.routes[0], Mount)  # the shim's single dispatch mount
    assert fake_uvicorn.served is True


async def test_run_debug_serves_create_app_result(patch_app_seam, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_debug serves exactly what ``create_app`` returns — the delegation seam."""
    patch_app_seam(_FakeApp(_FakeInnerApp()))
    fake_uvicorn = _FakeUvicorn()
    monkeypatch.setattr(mcp_app, "uvicorn", fake_uvicorn)
    sentinel = object()
    calls: list = []
    monkeypatch.setattr(mcp_app, "create_app", lambda: calls.append(True) or sentinel)

    config_kwargs: dict = {"host": "127.0.0.1", "port": 8000}
    rc = await mcp_app.run_debug(config_kwargs)

    assert rc == 0
    assert calls == [True]
    assert config_kwargs["app"] is sentinel
    assert fake_uvicorn.served is True


# --- CLI-seam logging-reload registration ---------------------------------


def test_wrapper_registers_cli_logging_reload(
    patch_app_seam, root_logger_restored, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAI_TRANSPORT", "http")
    monkeypatch.delenv("TAI_STATELESS_HTTP", raising=False)
    app = patch_app_seam(_FakeApp(_FakeInnerApp()))

    mcp_app.create_app()

    assert _LOGGING_RELOAD_KEY in app.lifecycle.reload_handlers


async def test_run_stdio_registers_cli_logging_reload(patch_app_seam) -> None:
    app = patch_app_seam(_FakeApp(_FakeInnerApp()))

    await mcp_app.run_stdio()

    assert _LOGGING_RELOAD_KEY in app.lifecycle.reload_handlers


async def test_run_debug_registers_cli_logging_reload(patch_app_seam, monkeypatch: pytest.MonkeyPatch) -> None:
    app = patch_app_seam(_FakeApp(_FakeInnerApp()))
    monkeypatch.setattr(mcp_app, "uvicorn", _FakeUvicorn())

    await mcp_app.run_debug({"host": "127.0.0.1", "port": 8000})

    assert _LOGGING_RELOAD_KEY in app.lifecycle.reload_handlers


def test_wrapper_installs_process_scope_redactor(
    patch_app_seam, root_logger_restored, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This CLI-owned worker widens the connector-secret redactor to the whole process.
    monkeypatch.setenv("TAI_TRANSPORT", "http")
    monkeypatch.delenv("TAI_STATELESS_HTTP", raising=False)
    patch_app_seam(_FakeApp(_FakeInnerApp()))
    scopes: list[object] = []
    monkeypatch.setattr(mcp_app, "install_meta_log_redactor", lambda **kwargs: scopes.append(kwargs.get("scope")))

    mcp_app.create_app()

    assert scopes == ["process"]


async def test_run_stdio_installs_process_scope_redactor(patch_app_seam, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_app_seam(_FakeApp(_FakeInnerApp()))
    scopes: list[object] = []
    monkeypatch.setattr(mcp_app, "install_meta_log_redactor", lambda **kwargs: scopes.append(kwargs.get("scope")))

    await mcp_app.run_stdio()

    assert scopes == ["process"]


async def test_run_debug_installs_process_scope_redactor(patch_app_seam, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_app_seam(_FakeApp(_FakeInnerApp()))
    monkeypatch.setattr(mcp_app, "uvicorn", _FakeUvicorn())
    scopes: list[object] = []
    monkeypatch.setattr(mcp_app, "install_meta_log_redactor", lambda **kwargs: scopes.append(kwargs.get("scope")))

    await mcp_app.run_debug({"host": "127.0.0.1", "port": 8000})

    assert scopes == ["process"]


# --- run_mcp_app: validation branches -------------------------------------


def test_create_app_stateless_reaches_http_app_factory(patch_app_seam, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the env flag set, the worker factory builds ``http_app(stateless_http=True)``."""
    monkeypatch.setenv("TAI_TRANSPORT", "http")
    monkeypatch.setenv("TAI_STATELESS_HTTP", "1")
    inner = _FakeInnerApp()
    app = patch_app_seam(_FakeApp(inner))

    star = mcp_app.create_app()
    with TestClient(star) as client:
        client.get("/")

    assert app.http_called is True
    assert app.http_stateless is True


# --- serve hardening: stale UDS socket cleanup ----------------------------


def test_prepare_uds_unlinks_stale_socket(uds_dir) -> None:
    path = str(uds_dir / "stale.sock")
    _stale_uds_socket(path)
    assert os.path.exists(path)

    mcp_app._prepare_uds_path(path)

    assert not os.path.exists(path)  # stale socket removed, ready to rebind


def test_prepare_uds_refuses_live_socket(uds_dir) -> None:
    path = str(uds_dir / "live.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    try:
        with pytest.raises(click.BadParameter, match="already running"):
            mcp_app._prepare_uds_path(path)
        assert os.path.exists(path)  # a live server's socket is never unlinked
    finally:
        server.close()
        os.unlink(path)


def test_prepare_uds_refuses_non_socket_without_unlink(tmp_path) -> None:
    path = tmp_path / "regular.file"
    path.write_text("not a socket")
    with pytest.raises(click.BadParameter, match="not a socket"):
        mcp_app._prepare_uds_path(str(path))
    assert path.exists()  # a non-socket path is refused and left in place


def test_prepare_uds_missing_path_is_noop(tmp_path) -> None:
    # A path that does not exist binds fresh — no error, nothing created.
    mcp_app._prepare_uds_path(str(tmp_path / "absent.sock"))
