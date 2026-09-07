"""Suite-wide test infrastructure.

The provider-catalog listing serves the ``connector_category`` grouping rows when
the connector store is configured, reading them through the catalog store's pooled
``client_ctx``. A test that drives that read (the connectors suite sets
``CONNECTOR_STORE_*`` env) would otherwise open a real Postgres pool and hang on
the connection timeout. This suite is fully offline, so the autouse fixture below
injects a fake pooled Postgres client at the catalog store's ``client_ctx`` seam:
the real ``fetch_categories`` wiring runs, reads an empty category set, and returns
instantly.

Tests that exercise the category DB read itself patch their own client seam; this
fixture only covers the otherwise-incidental categories read.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

# ``prometheus_client`` freezes its value backend (the multiprocess mmap class vs
# the in-process mutex class) the first time it is imported, choosing mmap only
# when ``PROMETHEUS_MULTIPROC_DIR`` is already set. The writer entry points assert
# the mmap backend is active, mirroring production where the launcher sets this
# before any import. Set it to a fresh per-session dir here — at conftest
# module-import time, before any test module imports ``prometheus_client`` — so a
# package-scoped skeleton run freezes mmap. A whole-repo run's rootdir conftest
# sets it first, and this guard then defers to that shared dir. No test writes
# counters here; render tests point the collector at their own tmp dirs.
if "PROMETHEUS_MULTIPROC_DIR" not in os.environ:
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = tempfile.mkdtemp(prefix="tai42_prometheus_")
    atexit.register(shutil.rmtree, os.environ["PROMETHEUS_MULTIPROC_DIR"], ignore_errors=True)

# The manifest-driven suites name fixture packages by dotted path
# (``tests.fixtures.dummy_agent``, ``tests.app._fixtures.tools_b``) that the app's
# own importer resolves through ``importlib`` and, on reload, pops from
# ``sys.modules`` to re-import. Under ``--import-mode=importlib`` pytest never puts
# the package root on ``sys.path``, so a popped ``tests.*`` module would be
# unfindable. Anchor ``tests`` as a discoverable namespace package by putting the
# package root on ``sys.path`` — the implicit prepend-mode behavior, made explicit.
if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import logging
from contextlib import asynccontextmanager

import pytest
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.connectors.store.catalog_store as catalog_store
import tai42_skeleton.db.boot_gate as boot_gate
import tai42_skeleton.states.db as states_db
import tai42_skeleton.tool_meta.store as tool_meta_store
import tai42_skeleton.versioning.store as versioning_store

from ._fakes.interactions_redis import FakeRedis
from .tool_meta.conftest import FakeToolMetaPg, make_pg_ctx
from .versioning.conftest import FakeVersioningPg


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def fake_client_ctx(fake_redis: FakeRedis):
    """A drop-in for ``tai42_kit.clients.client_ctx`` that yields the shared fake
    for any client class, ignoring pool/fresh."""

    @asynccontextmanager
    async def _ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield fake_redis

    return _ctx


class _FakeCursor:
    """Records nothing and returns no rows — the offline catalog is empty."""

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def execute(self, *args, **kwargs) -> None:
        return None

    async def fetchall(self) -> list:
        return []


class _FakeConn:
    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor()


class _FakePool:
    def connection(self) -> _FakeConn:
        return _FakeConn()


@pytest.fixture(autouse=True)
def _offline_connector_categories(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        yield _FakePool()

    monkeypatch.setattr(catalog_store, "client_ctx", fake_client_ctx)


@pytest.fixture(autouse=True)
def _offline_schema_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The boot-time migration gate (:func:`assert_skeleton_schema_applied`) reads the
    ``tai_schema_history`` table through the kit runner whenever the skeleton database
    is configured. An offline app-boot test that sets the database password (to turn a
    feature ON) but fakes that store's Postgres would otherwise have the gate reach for
    a real Postgres it never provided. Neutralize it by reporting the database as
    unconfigured — the same offline posture the store-seam fakes above take. The gate's
    own suite (``tests/db/test_boot_gate.py``) re-patches this seam to drive the
    configured / pending / refusal branches."""
    monkeypatch.setattr(boot_gate, "component_store_configured", lambda component: False)
    # The ``states`` component owns a second skeleton chain with the same posture: its
    # boot gate + seed applier read the store only when configured. Report it unconfigured
    # too, so an offline app-boot that sets the database password (to turn another feature
    # ON) does not have the states gate reach a Postgres it never provided. The states
    # store/service suites drive the configured behavior directly (fake store / real PG).
    monkeypatch.setattr(states_db, "states_store_configured", lambda: False)


@pytest.fixture(autouse=True)
def _offline_versioned_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """The versioned-document store's boot handlers (role seeding, preset
    rehydration) open Postgres through the store's own pooled ``client_ctx`` whenever
    the skeleton database is configured. An offline app-boot test that configures the
    database (to turn another feature ON) would otherwise have those handlers reach a
    real Postgres. Fake the store's seam with the stateful in-memory ``FakeVersioningPg``
    so the boot handlers run against it and return instantly. The versioning/preset
    suites re-patch this seam with their own fresh fake to assert store behavior."""
    fake = FakeVersioningPg()

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        yield fake

    monkeypatch.setattr(versioning_store, "client_ctx", fake_client_ctx)


@pytest.fixture(autouse=True)
def _offline_tool_meta_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """The preset create/delete/rename ops each touch the tool-metadata overlay store
    (a stale-row clear on create, a delete on delete, a re-key on rename) through the
    store's own pooled ``client_ctx``, so an offline test that drives a preset op would
    otherwise open a real Postgres for the overlay and hang on the connection timeout.
    Fake the store's seam with the stateful in-memory ``FakeToolMetaPg`` so the real
    overlay-store code runs against it and returns instantly. The tool-meta store's own
    suite re-patches this seam with its own fresh fake to assert the overlay behavior."""
    monkeypatch.setattr(tool_meta_store, "client_ctx", make_pg_ctx(FakeToolMetaPg()))


class _ProbeRedis:
    """A plain-Redis stand-in: ``HGETALL`` on the probe key answers ``{}``, so the
    identity provider's ``healthcheck()`` passes without a real Redis."""

    async def hgetall(self, key: str) -> dict[str, str]:
        return {}


@pytest.fixture(autouse=True)
def _ensure_redis_identity_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test-side sane default for the extracted identity provider.

    The skeleton ships NO concrete identity provider — a deployment names one in its
    manifest ``lifecycle_modules``, which ``start()`` imports. Tests that don't boot a
    full app (adapter / router / management coverage) still resolve the default
    ``"redis"`` provider through the module-level registry. An app-boot test's
    ``start()`` calls ``reset_registry()`` and then re-registers from the manifest, so
    a minimal test manifest that omits the identity plugin would leave the registry
    empty — and the AC-enabled startup probe (``probe_identity_provider``) would fail
    to resolve the active provider. Emulate a manifest that lists the plugin: register
    ``"redis"`` before each test, AND wrap ``start()``'s registry reset so the default
    is re-registered after the clear (the reset still runs, so a manifest re-import's
    duplicate guard is unaffected). Suites that isolate the registry snapshot this
    baseline and restore it."""
    from tai42_contract.access_control import registry
    from tai42_identity_redis.redis_api_key_provider import RedisApiKeyProvider

    import tai42_skeleton.app.lifecycle as lifecycle

    def _ensure() -> None:
        # Guard/register against the WRITE TARGET (the staged generation during an epoch
        # build, else the committed map), so a reload's staged registry gets the default
        # even though the committed one already holds it.
        if "redis" not in registry._write_target():
            registry.register_identity_provider("redis", RedisApiKeyProvider)

    real_reset = lifecycle.reset_identity_registry

    def _reset_then_ensure() -> None:
        real_reset()
        _ensure()

    monkeypatch.setattr(lifecycle, "reset_identity_registry", _reset_then_ensure)
    _ensure()


@pytest.fixture(autouse=True)
def _identity_probe_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The access-control startup probe runs whenever access control is enabled (the
    default). This offline suite has no real Redis, so point the provider's client
    seam at a plain-Redis fake — the real probe wiring runs and passes.
    ``probe_identity_provider`` awaits the active provider's ``healthcheck()``, which
    reaches Redis through the plugin's own ``client_ctx``, so patch that seam. The
    probe's own tests re-patch it to drive the failure branches."""
    import tai42_identity_redis.redis_api_key_provider as redis_provider

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, *, fresh=False, **kwargs):
        yield _ProbeRedis()

    monkeypatch.setattr(redis_provider, "client_ctx", fake_client_ctx)


@pytest.fixture
def preset_manager_restored():
    """Snapshot and restore the process-global preset manager around a test.

    ``app.preset_manager`` outlives every ``app_context`` a test opens, so a preset one
    test registers stays registered for the rest of the session: a later suite
    registering the same name raises ``PresetExistsError``, and a spec whose base tool
    the earlier suite's registry teardown removed is advertised but unresolvable. Any
    test that registers a preset takes this."""
    from tai42_skeleton.app.instance import app

    specs = dict(app.preset_manager._specs)
    quarantine = dict(app.preset_manager._quarantine)
    try:
        yield app.preset_manager
    finally:
        app.preset_manager._specs = specs
        app.preset_manager._quarantine = quarantine


@pytest.fixture
def root_logger_restored():
    """Snapshot the root logger and restore it afterwards. Code under test may run
    ``setup_logging`` / ``apply_logging_settings`` (both ``force=True``), which
    replace the root logger's handlers and level; the restore keeps a failing
    assertion from leaking that mutation into later tests. The teardown also drops
    the settings caches, so a test's monkeypatched ``TAI_LOG_LEVEL`` never survives
    into a later test's ``logging_settings()`` read."""
    root = logging.getLogger()
    level, handlers = root.level, root.handlers[:]
    try:
        yield root
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        reset_all_settings()
