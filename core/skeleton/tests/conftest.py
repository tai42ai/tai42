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
from collections.abc import Iterator
from contextlib import asynccontextmanager

import pytest
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.connectors.store.catalog_store as catalog_store
import tai42_skeleton.db.boot_gate as boot_gate
import tai42_skeleton.db.locks as db_locks
import tai42_skeleton.runs.store as runs_store
import tai42_skeleton.states.db as states_db
import tai42_skeleton.tool_meta.store as tool_meta_store
import tai42_skeleton.versioning.store as versioning_store

from ._fakes.advisory_locks import FakeAdvisoryLocks
from ._fakes.interactions_redis import FakeRedis
from ._module_identity import restore_states_module_identity
from ._staged_generations import preserved_staged_generations
from .runs.conftest import FakeRunIndexPg
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
def _preserve_generation_globals() -> Iterator[None]:
    """Restore every per-generation global around each test.

    An app boot rewrites the committed generations (the connector, identity, accounts,
    operation, monitoring and Studio registries, the route registry's shape index, and the
    access-control gate state) from its own manifest, and an epoch build promotes a staged
    generation over them. A test that boots a narrow manifest, or drives a build with a
    no-op rebuild, leaves those globals holding only what it registered; the router and
    plugin modules stay cached in ``sys.modules``, so nothing repopulates them for the next
    test on the same worker. Every later reader then sees the narrowed surface, e.g. the
    access-control verifier no longer matching a declared-public route. Autouse fixtures of one
    conftest run in name order; the snapshot is taken on entry and the restore on exit of this
    fixture, so a registration another autouse fixture makes before it is part of the restored
    state and one made after it is undone with the test."""
    with preserved_staged_generations():
        yield


@pytest.fixture(autouse=True, scope="session")
def _load_full_route_universe():
    """Load the whole offline route universe once per worker, before any test boots an app.

    ``load_api_routes`` / ``load_all_routes`` return ``route_registry.routes()`` — the
    process-wide dedup map of every registered route. The shared importer fills that map
    from the WHOLE ``tai42_skeleton.routers`` package in an unbound/offline process, but
    a STARTED app narrows the import to its manifest's effective router set. The process
    app is a singleton whose ``_manifest`` a boot sets and never clears, and the test
    conftests bind that singleton onto ``tai42_app`` at import time, so a test that boots
    a narrow manifest leaves the singleton answering a narrowed effective set. A later
    offline reader on the same xdist worker then takes the narrowed import branch, and if
    no full import has run first the dedup map is missing the routers the narrow boot
    omitted — a ``@covers`` attribution, a generated route-column table, or an OpenAPI
    assertion for one of those routes reads as stale and reds, nondeterministically by
    boot order.

    Import the whole router package up front under the offline spec harness so the dedup
    map is complete before any boot runs. The map only grows — a core ``/api`` route is
    never removed, and a narrow boot only pops and re-registers its own subset — so one
    load per worker keeps every offline reader enumerating the full surface each expects,
    independent of boot order."""
    from tai42_contract.app import tai42_app

    from tai42_skeleton.app.route_registry import _import_all_router_modules, _SpecApp

    with tai42_app.bound(_SpecApp()):
        _import_all_router_modules()


@pytest.fixture(autouse=True)
def _reset_settings_caches_between_tests():
    """Drop every cached settings accessor and settings-derived singleton around each test.

    Accessors such as ``interactions_settings`` and singletons such as the
    conversations manager snapshot their env once and cache it process-wide (only
    ``reset_all_settings`` clears them). A test that sets an env like
    ``INTERACTIONS_REDIS_URL`` / ``CONVERSATIONS_REDIS_URL`` and one that leaves it
    unset then read the same stale value. Resetting before and after each test keeps
    a value cached under one env from being read by a test running under the other —
    the leak parallel workers hit, running tests from different files in an
    unpredictable order within one process."""
    reset_all_settings()
    yield
    reset_all_settings()


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
def _offline_advisory_locks(monkeypatch: pytest.MonkeyPatch) -> FakeAdvisoryLocks:
    """A preset create claims its name under the fleet-wide advisory lock, which opens a
    Postgres of its OWN (a dedicated one-shot pool, not the store's), so an offline test
    that drives a create with the database configured would reach a real Postgres it never
    provided. Point the lock's ``pinned_connection`` seam at in-process locks with the same
    per-key exclusion; a suite that asserts contention re-patches this seam with its own
    fresh fake, and the real-Postgres suites restore the genuine one.

    The lock resolves its DSN before it dials, so a host is needed even for the fake — set
    a non-dialable one only when the environment names none, leaving a real-Postgres run's
    own host alone."""
    if not os.environ.get("TAI_DATABASE_DEFAULT_PG_HOST"):
        monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_HOST", "offline.invalid")
    locks = FakeAdvisoryLocks()
    monkeypatch.setattr(db_locks, "pinned_connection", locks.pinned_connection)
    return locks


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


@pytest.fixture(autouse=True)
def _offline_run_index_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runs-index chokepoint writes a ``run_index`` row around every outermost
    registered-preset dispatch through the store's own pooled ``client_ctx`` whenever
    the skeleton database is configured, so an offline app-boot test that runs a preset
    tool with the database configured would otherwise open a real Postgres for the index
    and block on the connection timeout. Fake the store's seam with the stateful
    in-memory ``FakeRunIndexPg`` so the real chokepoint code runs against it and returns
    instantly. The runs-index store's own suite re-patches this seam with its own fresh
    fake to assert the index behavior.

    Under the real-Postgres integration opt-in (``TAI42_SKELETON_REAL_PG``) this stands
    down: the ``integration`` suite drives the genuine store against the operator-provided
    TAI_DATABASE_DEFAULT_PG_*, where the ``run_index`` CHECK constraint and COALESCE
    write rules must fire — a fake would mask them."""
    if os.environ.get("TAI42_SKELETON_REAL_PG") in ("1", "true", "True"):
        return
    fake = FakeRunIndexPg()

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        yield fake

    monkeypatch.setattr(runs_store, "client_ctx", fake_client_ctx)


class _ProbeRedis:
    """A plain-Redis stand-in: ``HGETALL`` on the probe key answers ``{}``, so the
    identity provider's ``healthcheck()`` passes without a real Redis; ``SET NX`` reports
    a win so the setup auto-token fix passes too."""

    async def hgetall(self, key: str) -> dict[str, str]:
        return {}

    async def set(self, key: str, value: str, *, nx: bool = False, **_: object) -> bool:
        return True


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
    from tai42_identity_redis.redis_api_key_provider import RedisApiKeyProvider
    from tai42_kit.access_control import registry

    import tai42_skeleton.app.lifecycle as lifecycle

    def _ensure() -> None:
        # Guard/register against the WRITE TARGET (the staged generation during an epoch
        # build, else the committed map), so a reload's staged registry gets the default
        # even though the committed one already holds it.
        if "redis" not in registry.iter_identity_provider_names_staged():
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
    # The setup-token fix (``ensure_setup_token``) runs on the same AC-enabled boot and
    # opens the AC Redis through its OWN ``client_ctx`` for the SET NX; point that seam at
    # the same fake so an offline boot fixes the token without a real Redis.
    import tai42_skeleton.access_control.setup_gate as setup_gate

    monkeypatch.setattr(setup_gate, "client_ctx", fake_client_ctx)


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


def pytest_runtest_setup(item: pytest.Item) -> None:
    # Restore the states package's submodules to their canonical objects before every test, so
    # an orphaned re-import left by an earlier suite's reload exercise cannot answer the states
    # feature gate (which resolves through the parent package). Runs before the item's fixtures.
    restore_states_module_identity()
