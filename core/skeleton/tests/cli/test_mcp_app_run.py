"""The MCP-serve CLI: run_mcp_app argument validation, launch paths and graceful
shutdown, and the cli / main entry points.

The launcher wiring runs up to (but not through) the blocking server start —
``uvicorn.run`` / ``uvicorn.Server.serve`` / ``asyncio.run`` are mocked, and the
``app`` / ``Manifest`` seams are replaced with fakes.
"""

from __future__ import annotations

import logging
import os

import click
import pytest
from click.testing import CliRunner
from starlette.applications import Starlette

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


def _defaults() -> tuple[str, int]:
    settings = mcp_app.app_args_settings()
    return settings.host, settings.port


def test_uds_on_windows_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_app.sys, "platform", "win32")
    host, port = _defaults()
    with pytest.raises(click.BadParameter, match="Unix Domain Sockets"):
        mcp_app.run_mcp_app("m.yaml", "sse", host, port, workers=1, uds="/tmp/s.sock")


def test_uds_with_stdio_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    host, port = _defaults()
    with pytest.raises(click.BadParameter, match="cannot be used with '--transport stdio'"):
        mcp_app.run_mcp_app("m.yaml", "stdio", host, port, workers=1, uds="/tmp/s.sock")


def test_stdio_with_nondefault_host_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    _, port = _defaults()
    with pytest.raises(click.BadParameter, match="should not be set"):
        mcp_app.run_mcp_app("m.yaml", "stdio", "0.0.0.0", port, workers=1)


def test_stdio_with_multiple_workers_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    host, port = _defaults()
    with pytest.raises(click.BadParameter, match="Multiple workers"):
        mcp_app.run_mcp_app("m.yaml", "stdio", host, port, workers=2)


# --- serve hardening: stateful-transport worker guard ---------------------


@pytest.mark.parametrize("transport", ["http", "streamable-http", "sse"])
def test_stateful_transport_multiple_workers_rejected(transport: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every stateful HTTP/SSE transport refuses workers>1, naming the fix."""
    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    host, port = _defaults()
    with pytest.raises(click.BadParameter) as excinfo:
        mcp_app.run_mcp_app("m.yaml", transport, host, port, workers=2)
    message = str(excinfo.value)
    assert "run one worker" in message
    if transport in {"http", "streamable-http"}:
        assert "--stateless-http" in message
    else:
        assert "no stateless mode" in message


@pytest.mark.parametrize("transport", ["http", "streamable-http"])
def test_stateless_http_lifts_multi_worker_refusal(transport: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    monkeypatch.delenv("TAI_RUN_MODE", raising=False)
    recorded: list = []
    monkeypatch.setattr(mcp_app.uvicorn, "run", lambda target, **kw: recorded.append((target, kw)))
    host, port = _defaults()

    rc = mcp_app.run_mcp_app("m.yaml", transport, host, port, workers=4, stateless_http=True)

    assert rc == 0
    assert recorded[0][1]["workers"] == 4
    assert mcp_app.os.environ["TAI_STATELESS_HTTP"] == "1"


@pytest.mark.parametrize("transport", ["sse", "stdio"])
def test_stateless_http_with_non_http_transport_rejected(transport: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    host, port = _defaults()
    # ``stdio``/``uds`` also refuse a non-default host, so keep them at defaults.
    args = (host, port) if transport == "sse" else _defaults()
    with pytest.raises(click.BadParameter, match="requires an http transport"):
        mcp_app.run_mcp_app("m.yaml", transport, *args, workers=1, stateless_http=True)


def test_stateless_http_clears_env_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run WITHOUT --stateless-http clears any stale env flag from a prior run."""
    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    monkeypatch.delenv("TAI_RUN_MODE", raising=False)
    monkeypatch.setenv("TAI_STATELESS_HTTP", "1")
    monkeypatch.setattr(mcp_app.uvicorn, "run", lambda target, **kw: None)
    host, port = _defaults()

    mcp_app.run_mcp_app("m.yaml", "http", host, port, workers=1)

    assert "TAI_STATELESS_HTTP" not in mcp_app.os.environ


def test_run_mcp_app_uds_cleans_stale_before_bind(monkeypatch: pytest.MonkeyPatch, uds_dir) -> None:
    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    monkeypatch.delenv("TAI_RUN_MODE", raising=False)
    path = str(uds_dir / "run.sock")
    _stale_uds_socket(path)
    recorded: list = []
    monkeypatch.setattr(mcp_app.uvicorn, "run", lambda target, **kw: recorded.append((target, kw)))
    host, port = _defaults()

    rc = mcp_app.run_mcp_app("m.yaml", "http", host, port, workers=1, uds=path)

    assert rc == 0
    assert recorded[0][1]["uds"] == path
    assert not os.path.exists(path)  # unlinked before the (faked) bind


# --- run_mcp_app: launch paths --------------------------------------------


def test_run_mcp_app_stdio_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    monkeypatch.delenv("TAI_RUN_MODE", raising=False)
    calls: list = []

    def fake_asyncio_run(coro):
        coro.close()
        calls.append(coro)
        return 0

    monkeypatch.setattr(mcp_app.asyncio, "run", fake_asyncio_run)
    host, port = _defaults()

    rc = mcp_app.run_mcp_app("manifest.yaml", "stdio", host, port, workers=1, uvicorn_kwargs=None)

    assert rc == 0
    assert len(calls) == 1
    assert os.environ["TAI_MANIFEST_PATH"] == "manifest.yaml"
    assert os.environ["TAI_TRANSPORT"] == "stdio"


def test_run_mcp_app_activates_multiproc_env_before_wipe(monkeypatch: pytest.MonkeyPatch) -> None:
    # The multiproc dir env must be published BEFORE the wipe is imported/called —
    # importing the wipe's module is the first thing that pulls in prometheus_client
    # (which freezes its value backend from the env). This pins that ordering.
    import tai42_skeleton.routers.metrics_settings as ms
    import tai42_skeleton.routers.prometheus as prom

    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    monkeypatch.delenv("TAI_RUN_MODE", raising=False)
    monkeypatch.setattr(mcp_app.asyncio, "run", lambda coro: coro.close() or 0)

    settings_dir = ms.metrics_settings().prometheus_multiproc_dir
    order: list = []

    def fake_activate() -> str:
        order.append("activate")
        os.environ["PROMETHEUS_MULTIPROC_DIR"] = settings_dir
        return settings_dir

    def fake_wipe() -> str:
        # Record the env visible at wipe time — it must already be the settings dir.
        order.append(("wipe", os.environ.get("PROMETHEUS_MULTIPROC_DIR")))
        return settings_dir

    monkeypatch.setattr(ms, "activate_multiproc_env", fake_activate)
    monkeypatch.setattr(prom, "wipe_prometheus_multiproc_dir", fake_wipe)
    host, port = _defaults()

    rc = mcp_app.run_mcp_app("manifest.yaml", "stdio", host, port, workers=1)

    assert rc == 0
    assert os.path.isabs(settings_dir)
    assert order == ["activate", ("wipe", settings_dir)]


def test_run_mcp_app_tcp_path_calls_uvicorn_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    monkeypatch.delenv("TAI_RUN_MODE", raising=False)
    recorded: list = []
    monkeypatch.setattr(mcp_app.uvicorn, "run", lambda target, **kw: recorded.append((target, kw)))
    host, port = _defaults()

    # >1 worker on an http transport is only allowed under --stateless-http.
    rc = mcp_app.run_mcp_app("manifest.yaml", "http", host, port, workers=3, stateless_http=True)

    assert rc == 0
    target, kwargs = recorded[0]
    assert target == "tai42_skeleton.cli.mcp_app:create_app"
    assert kwargs["factory"] is True
    assert kwargs["workers"] == 3
    assert kwargs["host"] == host
    assert kwargs["port"] == port
    # The flag travels to the factory worker by env.
    assert mcp_app.os.environ["TAI_STATELESS_HTTP"] == "1"


def test_run_mcp_app_uds_path_binds_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    monkeypatch.delenv("TAI_RUN_MODE", raising=False)
    recorded: list = []
    monkeypatch.setattr(mcp_app.uvicorn, "run", lambda target, **kw: recorded.append((target, kw)))
    host, port = _defaults()

    rc = mcp_app.run_mcp_app("manifest.yaml", "sse", host, port, workers=1, uds="/tmp/s.sock")

    assert rc == 0
    _, kwargs = recorded[0]
    assert kwargs["uds"] == "/tmp/s.sock"
    assert "host" not in kwargs
    assert "port" not in kwargs


def test_run_mcp_app_debug_environment_runs_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    monkeypatch.setenv("TAI_RUN_MODE", "debug")
    calls: list = []

    def fake_asyncio_run(coro):
        coro.close()
        calls.append(coro)
        return 0

    monkeypatch.setattr(mcp_app.asyncio, "run", fake_asyncio_run)
    host, port = _defaults()

    rc = mcp_app.run_mcp_app("manifest.yaml", "http", host, port, workers=1)

    assert rc == 0
    assert len(calls) == 1


def test_run_mcp_app_debug_run_mode_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``debug`` matches regardless of case.
    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    monkeypatch.setenv("TAI_RUN_MODE", "DEBUG")
    calls: list = []

    def fake_asyncio_run(coro):
        coro.close()
        calls.append(coro)
        return 0

    monkeypatch.setattr(mcp_app.asyncio, "run", fake_asyncio_run)
    host, port = _defaults()

    rc = mcp_app.run_mcp_app("manifest.yaml", "http", host, port, workers=1)

    assert rc == 0
    assert len(calls) == 1


def test_run_mcp_app_unknown_run_mode_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Any other non-empty value fails loudly rather than silently falling through
    # to the normal multi-worker path.
    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    monkeypatch.setenv("TAI_RUN_MODE", "production")
    monkeypatch.setattr(mcp_app.uvicorn, "run", lambda *a, **kw: pytest.fail("uvicorn.run must not be reached"))
    monkeypatch.setattr(mcp_app.asyncio, "run", lambda *a, **kw: pytest.fail("asyncio.run must not be reached"))
    host, port = _defaults()

    with pytest.raises(click.ClickException) as excinfo:
        mcp_app.run_mcp_app("manifest.yaml", "http", host, port, workers=1)

    message = str(excinfo.value)
    assert "TAI_RUN_MODE" in message
    assert "production" in message
    assert "debug" in message


def test_run_mcp_app_normal_path_logs_worker_count(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    monkeypatch.delenv("TAI_RUN_MODE", raising=False)
    monkeypatch.setattr(mcp_app.uvicorn, "run", lambda target, **kw: None)
    # run_mcp_app configures logging (basicConfig force=True, which would drop the
    # caplog handler); no-op it here so this test can capture the run-mode message.
    monkeypatch.setattr(mcp_app, "setup_logging", lambda *a, **k: None)
    host, port = _defaults()

    with caplog.at_level("INFO", logger=mcp_app.logger.name):
        # stateless-http lifts the single-worker restriction for the http transport.
        rc = mcp_app.run_mcp_app("manifest.yaml", "http", host, port, workers=3, stateless_http=True)

    assert rc == 0
    assert any("worker" in rec.getMessage() and "3" in rec.getMessage() for rec in caplog.records)


def test_run_mcp_app_configures_logging_on_serve_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shipped ``tai serve`` reaches ``run_mcp_app`` via ``cli`` (not ``main``),
    # so ``run_mcp_app`` must configure logging itself at its top — otherwise the
    # master/stdio/debug servers it dispatches to stay unconfigured. This is the
    # positive guard: ``setup_logging`` IS invoked with the resolved
    # ``logging_settings()`` before the (faked) uvicorn launch.
    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    monkeypatch.delenv("TAI_RUN_MODE", raising=False)
    monkeypatch.setattr(mcp_app.uvicorn, "run", lambda target, **kw: None)
    recorded: list = []
    monkeypatch.setattr(mcp_app, "setup_logging", lambda cfg: recorded.append(cfg))
    host, port = _defaults()

    rc = mcp_app.run_mcp_app("manifest.yaml", "http", host, port, workers=3, stateless_http=True)

    assert rc == 0
    assert recorded == [mcp_app.logging_settings()]


def test_run_mcp_app_merges_uvicorn_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    monkeypatch.delenv("TAI_RUN_MODE", raising=False)
    recorded: list = []
    monkeypatch.setattr(mcp_app.uvicorn, "run", lambda target, **kw: recorded.append((target, kw)))
    host, port = _defaults()

    mcp_app.run_mcp_app("manifest.yaml", "http", host, port, workers=1, uvicorn_kwargs={"timeout_keep_alive": 5})

    _, kwargs = recorded[0]
    assert kwargs["timeout_keep_alive"] == 5
    assert kwargs["ws"] == "wsproto"


# --- graceful-shutdown timeout --------------------------------------------


def test_run_mcp_app_sets_graceful_shutdown_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # The normal (uvicorn.run) path carries the settings-backed default.
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    monkeypatch.delenv("TAI_RUN_MODE", raising=False)
    monkeypatch.delenv("APP_ARGS_TIMEOUT_GRACEFUL_SHUTDOWN", raising=False)
    reset_all_settings()
    recorded: list = []
    monkeypatch.setattr(mcp_app.uvicorn, "run", lambda target, **kw: recorded.append((target, kw)))
    host, port = _defaults()

    rc = mcp_app.run_mcp_app("manifest.yaml", "http", host, port, workers=1)

    assert rc == 0
    assert recorded[0][1]["timeout_graceful_shutdown"] == 10
    reset_all_settings()


def test_run_mcp_app_debug_path_carries_graceful_shutdown(patch_app_seam, monkeypatch: pytest.MonkeyPatch) -> None:
    # The debug path builds a uvicorn.Config from the same config_kwargs, so the
    # setting reaches it too.
    patch_app_seam(_FakeApp(_FakeInnerApp()))
    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    monkeypatch.setenv("TAI_RUN_MODE", "debug")
    fake_uvicorn = _FakeUvicorn()
    monkeypatch.setattr(mcp_app, "uvicorn", fake_uvicorn)
    host, port = _defaults()

    rc = mcp_app.run_mcp_app("manifest.yaml", "http", host, port, workers=1)

    assert rc == 0
    # The debug path serves the shim factory app, carrying the setting into its Config.
    assert isinstance(fake_uvicorn.config_kwargs, dict)
    assert isinstance(fake_uvicorn.config_kwargs["app"], Starlette)
    assert (
        fake_uvicorn.config_kwargs["timeout_graceful_shutdown"] == mcp_app.app_args_settings().timeout_graceful_shutdown
    )


def test_cli_graceful_shutdown_extra_arg_overrides_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    # A shipped ``--timeout-graceful-shutdown`` CLI extra-arg wins over the default.
    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    monkeypatch.delenv("TAI_RUN_MODE", raising=False)
    recorded: list = []
    monkeypatch.setattr(mcp_app.uvicorn, "run", lambda target, **kw: recorded.append((target, kw)))
    host, port = _defaults()

    mcp_app.run_mcp_app("manifest.yaml", "http", host, port, workers=1, uvicorn_kwargs={"timeout_graceful_shutdown": 3})

    assert recorded[0][1]["timeout_graceful_shutdown"] == 3


def test_run_mcp_app_graceful_shutdown_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setattr(mcp_app.sys, "platform", "linux")
    monkeypatch.delenv("TAI_RUN_MODE", raising=False)
    monkeypatch.setenv("APP_ARGS_TIMEOUT_GRACEFUL_SHUTDOWN", "7")
    reset_all_settings()
    recorded: list = []
    monkeypatch.setattr(mcp_app.uvicorn, "run", lambda target, **kw: recorded.append((target, kw)))
    host, port = _defaults()

    try:
        rc = mcp_app.run_mcp_app("manifest.yaml", "http", host, port, workers=1)
        assert rc == 0
        assert recorded[0][1]["timeout_graceful_shutdown"] == 7
    finally:
        reset_all_settings()


# --- cli (Click entry) ----------------------------------------------------


def test_cli_forwards_parsed_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(mcp_app, "run_mcp_app", lambda **kw: captured.update(kw) or 0)

    result = CliRunner().invoke(
        mcp_app.cli,
        [
            "--manifest-path",
            "m.yaml",
            "--transport",
            "HTTP",
            "--host",
            "1.2.3.4",
            "--port",
            "9001",
            "--workers",
            "2",
            "--timeout-keep-alive",
            "5",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["manifest_path"] == "m.yaml"
    assert captured["transport"] == "http"  # lowercased before forwarding
    assert captured["host"] == "1.2.3.4"
    assert captured["port"] == 9001
    assert captured["workers"] == 2
    assert captured["uvicorn_kwargs"]["timeout_keep_alive"] == 5


def test_cli_manifest_default_comes_from_tai_manifest_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """TAI_MANIFEST_PATH is the single manifest env var: with no --manifest-path
    flag, the CLI resolves its default from it (via CoreSettings) end-to-end."""
    from tai42_skeleton.settings import cache

    monkeypatch.setenv("TAI_MANIFEST_PATH", "/etc/tai/from-env.yaml")
    cache.manifest_path.cache_clear()
    captured: dict = {}
    monkeypatch.setattr(mcp_app, "run_mcp_app", lambda **kw: captured.update(kw) or 0)
    try:
        result = CliRunner().invoke(mcp_app.cli, [])
    finally:
        cache.manifest_path.cache_clear()

    assert result.exit_code == 0, result.output
    assert captured["manifest_path"] == "/etc/tai/from-env.yaml"


def test_cli_keyboard_interrupt_exits_130(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(mcp_app, "run_mcp_app", boom)

    result = CliRunner().invoke(mcp_app.cli, ["--manifest-path", "m.yaml"])

    assert result.exit_code == 130


def test_cli_known_error_becomes_click_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kwargs):
        raise RuntimeError("launch failed")

    monkeypatch.setattr(mcp_app, "run_mcp_app", boom)

    result = CliRunner().invoke(mcp_app.cli, ["--manifest-path", "m.yaml"])

    assert result.exit_code != 0
    assert "launch failed" in result.output


def test_main_invokes_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list = []
    monkeypatch.setattr(mcp_app, "cli", lambda: called.append(True))
    monkeypatch.setattr(mcp_app, "config_mode", lambda: "external")

    mcp_app.main()

    assert called == [True]


def test_main_bootstraps_env_in_file_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(mcp_app, "cli", lambda: None)
    monkeypatch.setattr(mcp_app, "config_mode", lambda: "file")
    monkeypatch.setattr(mcp_app, "load_dotenv", lambda: called.append(True))

    mcp_app.main()

    assert called == [True]


def test_main_skips_env_bootstrap_in_a_non_file_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(mcp_app, "cli", lambda: None)
    monkeypatch.setattr(mcp_app, "config_mode", lambda: "external")
    monkeypatch.setattr(mcp_app, "load_dotenv", lambda: called.append(True))

    mcp_app.main()

    assert called == []
