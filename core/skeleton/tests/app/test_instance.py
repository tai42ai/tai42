"""The process app singleton: ``instance.app`` is a built ``TaiMCP`` whose
``lifespan`` context opens (and, on exit, closes) the sub-app router lifespan.
Process-wide resource teardown lives on ``app_context`` (see ``test_lifecycle``),
not here.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import pytest
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.app.instance as instance
from tai42_skeleton.app.server import TaiMCP


def test_app_singleton_is_taimcp():
    assert isinstance(instance.app, TaiMCP)
    assert instance.app.fastmcp.name == "Tai"


def test_build_app_is_idempotent():
    assert instance.build_app() is instance.build_app()
    assert instance.build_app() is instance.app


def test_rehydrate_presets_wired_as_startup_and_reload():
    # Versioned presets rehydrate at boot AND on every in-place reload, so a
    # persisted preset survives a restart and a reload_config().
    assert instance.rehydrate_versioned_presets_if_store_in_use in instance.app._startup_handlers.values()
    assert instance.rehydrate_versioned_presets_if_store_in_use in instance.app._reload_handlers.values()


def test_access_control_probe_wired_as_startup_when_enabled():
    # The process app singleton is built with access control enabled (the default), so
    # the active identity provider's healthcheck must be registered as a startup
    # handler: it is THE boot probe. Dropping it silently re-opens the boot trap (a
    # broken backend boots clean and dies on the first authenticated request), so this
    # pins the wiring, not just the probe function.
    from tai42_skeleton.access_control.startup import probe_identity_provider

    startup_handlers = instance.app._startup_handlers.values()
    assert probe_identity_provider in startup_handlers


def test_fenced_route_audit_wired_as_startup_and_reload_when_enabled():
    # A reload can mount a new fenced route, so the fence-resolvability audit must run
    # on reload as well as at boot — otherwise a reload-added fenced route fails open
    # until restart. Both registrations are pinned.
    from tai42_skeleton.access_control.startup import check_fenced_routes_resolvable

    assert check_fenced_routes_resolvable in instance.app._startup_handlers.values()
    assert check_fenced_routes_resolvable in instance.app._reload_handlers.values()


def test_route_row_audit_wired_as_startup_when_enabled():
    # A route row stored in a form no request path reduces to must fail the boot, so the
    # canonical-row audit is a startup handler of the access-control build.
    from tai42_skeleton.access_control.startup import check_route_rows_canonical

    assert check_route_rows_canonical in instance.app._startup_handlers.values()


def test_mount_change_listener_invalidates_the_policy_cache():
    # A sub-MCP mount change reaches the access-control policy cache only through the
    # listener the composition root wires on the sub-MCP write chokepoint.
    from tai42_skeleton.sub_mcp import service

    instance.build_app()
    assert service._mount_change_listeners.count(instance._invalidate_policy_cache) == 1


async def test_invalidate_policy_cache_bumps_the_policy_version(monkeypatch):
    from tai42_skeleton.access_control import management

    bumps: list[int] = []

    async def _bump() -> int:
        bumps.append(1)
        return len(bumps)

    monkeypatch.setattr(management, "bump_policy_version", _bump)
    await instance._invalidate_policy_cache()
    assert bumps == [1]


# --- connectors gate --------------------------------------------------------


def _clear_database_env(monkeypatch) -> None:
    # Every skeleton store gates on the one bound-database password; clearing it (and
    # any connector redis env) leaves the DB-backed features cleanly OFF.
    import os

    for key in list(os.environ):
        if key.startswith(("CONNECTORS_", "CONNECTOR_STORE_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)


def test_connectors_gate_off_when_store_unconfigured(monkeypatch):
    # The connectors gate is the store-configured gate alone: with the skeleton
    # database unconfigured, connectors read OFF.
    _clear_database_env(monkeypatch)
    assert instance.connectors_in_use() is False


def test_connectors_gate_on_when_store_configured(monkeypatch):
    # The bound-database password is the single signal that flips connectors ON.
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    assert instance.connectors_in_use() is True


# --- versioned-preset rehydration gate --------------------------------------


def _record_rehydrate(monkeypatch) -> list[bool]:
    calls: list[bool] = []

    async def fake_rehydrate() -> None:
        calls.append(True)

    monkeypatch.setattr(instance.build_app().preset_manager, "rehydrate", fake_rehydrate)
    return calls


async def test_rehydrate_skipped_when_versioning_store_unused(monkeypatch):
    # Skeleton database unconfigured: the handler skips the load (no Postgres at boot).
    _clear_database_env(monkeypatch)
    calls = _record_rehydrate(monkeypatch)

    await instance.rehydrate_versioned_presets_if_store_in_use()

    assert calls == []


async def test_rehydrate_runs_when_versioning_store_env_present(monkeypatch):
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    calls = _record_rehydrate(monkeypatch)

    await instance.rehydrate_versioned_presets_if_store_in_use()

    assert calls == [True]


# --- logging reload handler -------------------------------------------------


def test_apply_logging_wired_only_by_cli_seam_registration(monkeypatch):
    # A bare ``build_app()`` does NOT register the root-logger reload handler — an
    # embedded app never reconfigures the host's logging. The CLI seams register it
    # explicitly via ``register_cli_logging_reload``, and only as a reload handler
    # (never a startup handler; process start is covered by the CLI's own
    # ``setup_logging`` call). The singleton is reset first so a prior CLI-seam
    # registration on the process singleton (e.g. from the backend beat test) cannot
    # spuriously FAIL the absence assertion by collection-order luck.
    monkeypatch.setattr(instance, "_app", None)

    app = instance.build_app()
    assert instance.apply_logging_settings not in app._reload_handlers.values()
    assert instance.apply_logging_settings not in app._startup_handlers.values()

    instance.register_cli_logging_reload()
    assert instance.apply_logging_settings in app._reload_handlers.values()
    assert instance.apply_logging_settings not in app._startup_handlers.values()


def test_build_app_installs_redactor_at_tai_scope(monkeypatch):
    # The REAL ``build_app()`` wire: it installs the connector-secret redactor at
    # its default ``tai`` scope — a tai-family record is scrubbed, a host-app
    # record passes through untouched. Factory and scope are snapshotted and
    # restored so this test neither depends on nor leaks redactor state; the
    # singleton is reset so the first-build branch (where the install lives)
    # actually runs.
    from tai42_skeleton.connectors import meta_log_redactor

    saved_factory = logging.getLogRecordFactory()
    saved_scope = meta_log_redactor._SCOPE
    logging.setLogRecordFactory(logging.LogRecord)
    meta_log_redactor._SCOPE = "tai"
    monkeypatch.setattr(instance, "_app", None)
    try:
        instance.build_app()

        factory = logging.getLogRecordFactory()
        secret = '{"_meta": {"tai_hub.access_token": "WIRE-SECRET"}}'
        tai42_rec = factory("tai42_skeleton.connectors", logging.INFO, __file__, 1, secret, None, None)
        host_rec = factory("myhost.app", logging.INFO, __file__, 1, secret, None, None)
        assert "WIRE-SECRET" not in tai42_rec.getMessage()
        assert "WIRE-SECRET" in host_rec.getMessage()
    finally:
        logging.setLogRecordFactory(saved_factory)
        meta_log_redactor._SCOPE = saved_scope


def test_apply_logging_settings_applies_configured_level(monkeypatch, root_logger_restored):
    monkeypatch.setenv("TAI_LOG_LEVEL", "debug")
    reset_all_settings()

    instance.apply_logging_settings()

    assert root_logger_restored.level == logging.DEBUG
    # A root handler with the kit format (which names the logger) is installed.
    formatter = root_logger_restored.handlers[0].formatter
    assert formatter is not None
    assert "%(name)s" in formatter._fmt  # type: ignore[union-attr]


def test_apply_logging_settings_reapplies_after_level_change(monkeypatch, root_logger_restored):
    # The reload path runs reset_all_settings() before the handler, so a changed
    # TAI_LOG_LEVEL is re-read and re-applied without a process restart.
    monkeypatch.setenv("TAI_LOG_LEVEL", "warning")
    reset_all_settings()
    instance.apply_logging_settings()
    assert root_logger_restored.level == logging.WARNING

    monkeypatch.setenv("TAI_LOG_LEVEL", "error")
    reset_all_settings()
    instance.apply_logging_settings()
    assert root_logger_restored.level == logging.ERROR


async def test_lifespan_enters_and_exits_router_lifespan(monkeypatch):
    # The lifespan opens the sub-app router lifespan and closes it on exit; it
    # does not itself tear down resources (that is app_context's job).
    events: list[str] = []

    @asynccontextmanager
    async def fake_router_lifespan(_app):
        events.append("router-open")
        try:
            yield
        finally:
            events.append("router-close")

    monkeypatch.setattr(instance.app.sub_app.mcp_sub_app_router, "lifespan", fake_router_lifespan)

    async with instance.lifespan(instance.app):
        assert events == ["router-open"]

    assert events == ["router-open", "router-close"]


# --- the app's own readiness and drain declarations -------------------------


def test_a_rebuilt_singleton_declares_its_readiness_and_drain_again(monkeypatch):
    # Every build of the singleton declares the platform's readiness contributors and the
    # tool-runs drain budget on the app it builds, so a reset singleton rebuilds cleanly
    # and the fresh app carries its own declarations.
    monkeypatch.setattr(instance, "_app", None)
    first = instance.build_app()
    monkeypatch.setattr(instance, "_app", None)
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", "redis://conversations")
    monkeypatch.setenv("TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS", "23")
    reset_all_settings()
    try:
        second = instance.build_app()
        assert second is not first
        assert "conversations" in [target.name for target in second.readiness.wired_targets()]
        assert second.drain_budgets.budget() == 23.0
    finally:
        monkeypatch.delenv("CONVERSATIONS_REDIS_URL")
        monkeypatch.delenv("TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS")
        reset_all_settings()


def test_a_duplicate_declaration_within_one_app_is_refused(monkeypatch):
    monkeypatch.setattr(instance, "_app", None)
    app = instance.build_app()
    with pytest.raises(ValueError, match="'bus'"):
        app.readiness.register("bus", list)
    with pytest.raises(ValueError, match="'tool_runs'"):
        app.drain_budgets.register("tool_runs", lambda: 1.0)


def test_a_failed_build_raises_its_own_error_again_on_the_next_build(monkeypatch):
    # A build that fails leaves no singleton and no half-made declarations behind, so the
    # next build fails on the same root cause rather than on a leftover registration.
    monkeypatch.setattr(instance, "_app", None)

    def _failing_install() -> None:
        raise RuntimeError("the redactor could not be installed")

    monkeypatch.setattr(instance, "install_meta_log_redactor", _failing_install)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="the redactor could not be installed"):
            instance.build_app()
    assert instance._app is None
