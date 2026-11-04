"""Worker-backend launcher.

``run_backend`` launches every invocation inside ``app_context`` — which binds
the ``tai42_app`` handle and imports the manifest's backend module, so a plugin
registering its Backend at import lands in the bound holder. It joins the worker
bus under the ``backend`` origin kind, and publishes the resolved manifest into the
env for the WORKER runtime only — the one whose forked children need it. The
``main`` Click entry just selects an event loop (uvloop, else asyncio; asyncio in a
process started through a process prelude), hands the coroutine to its blocking
``run`` and restarts the process through the prelude a launch refused it for — that
blocking run is mocked so the launcher wiring (env publish, arg merge) is exercised
without a real loop.
"""

from __future__ import annotations

import asyncio
import asyncio.futures
import logging
import os
import signal
import sys
from collections.abc import Iterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from click.testing import CliRunner
from tai42_contract.app import tai42_app
from tai42_contract.backend.runtime import BackendRuntime, ExecutionMode, ProgressLiveness
from tai42_kit.backend import ManagedBackend, future_chaining_bound
from tai42_kit.logging import redaction as kit_redaction
from tai42_kit.signals import STOP_SIGNALS, signal_chain

import tai42_skeleton.cli.backend as backend
from tai42_skeleton.app.bus import WorkerBus, WorkerKind
from tai42_skeleton.app.lifecycle.exit_drain import ExitDrain

_LOGGING_KEY = "tai42_skeleton.app.instance.apply_logging_settings"


@pytest.fixture(autouse=True)
def _restore_log_record_factory():
    """Save/restore the process-global record factory and its monotonic redaction
    scope around every test: ``run_backend`` installs the connector-secret redactor
    at process scope, so this keeps one test's install from leaking onward."""
    saved_factory = logging.getLogRecordFactory()
    saved_scope = kit_redaction._SCOPE
    try:
        yield
    finally:
        logging.setLogRecordFactory(saved_factory)
        kit_redaction._SCOPE = saved_scope


class _FakeConfigManager:
    def read_manifest(self) -> dict:
        return {}


class _FakeManifest:
    backend_module = "my.backend.module"

    def model_dump_json(self) -> str:
        return '{"backend_module": "my.backend.module"}'


class _FakeLifecycle:
    """Records reload-handler registrations by their ``module.qualname`` key, so a
    test can assert ``register_cli_process_state`` wired ``apply_logging_settings``,
    and stands in for the cold-boot manifest read the launcher delegates here."""

    def __init__(self) -> None:
        self.reload_handlers: dict[str, object] = {}

    def on_reload(self, func):
        self.reload_handlers[f"{func.__module__}.{func.__qualname__}"] = func
        return func

    # The worker kind the boot manifest read built the bus for.
    kind = None

    async def read_boot_manifest(self, *, kind) -> _FakeManifest:
        self.kind = kind
        return _FakeManifest()


class _StopObservingApp:
    """The app's observation of the stop's start, on a real local backend bus.

    Records each reason the host passed and whether that call began the leaving.
    """

    def __init__(self) -> None:
        self.bus = WorkerBus.local(WorkerKind.backend)
        self.stops_began: list[tuple[str, bool]] = []

    def stop_began(self, reason: str) -> bool:
        began = self.bus.stop_began(reason)
        self.stops_began.append((reason, began))
        return began


class _FakeApp(_StopObservingApp):
    def __init__(self) -> None:
        super().__init__()
        # The config manager is reached through the ``config`` facet namespace.
        self.config = SimpleNamespace(config_manager=_FakeConfigManager())
        # ``run_backend`` registers the root-logger reload handler through the app's
        # lifecycle; the recorder captures it without a real lifecycle.
        self.lifecycle = _FakeLifecycle()
        # The boot env handlers, recorded by their ``module.qualname`` key like the reload ones.
        self.boot_env_handlers: dict[str, object] = {}
        self.run_backend_args: list = []
        self.context_entered = False
        # The env as it stands the moment the context opens. A worker forks job
        # children that inherit the resolved manifest, so the launcher must publish it
        # BEFORE the context opens (start() imports the backend module inside it).
        self.manifest_env_at_entry: str | None = None
        # The worker kind the launcher's boot manifest read built the worker bus for, as the
        # context opens: a backend runtime joins as ``backend``, not ``serve``.
        self.kind_at_entry = None

    def _on_boot_env_applied(self, func):
        self.boot_env_handlers[f"{func.__module__}.{func.__qualname__}"] = func
        return func

    @asynccontextmanager
    async def app_context(self, manifest):
        self.context_entered = True
        self.kind_at_entry = self.lifecycle.kind
        settings = backend.base_backend_settings()
        self.manifest_env_at_entry = os.environ.get(settings.manifest_key)
        yield

    async def run_backend(self, args) -> None:
        self.run_backend_args.append(args)


@pytest.fixture(autouse=True)
def _bus_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """A backend runtime always registers a task backend, so ``run_backend``'s boot
    rule requires the worker bus and a shared hooks registry. Report both configured so the launcher-wiring tests
    exercise the launch path without a real Redis (the app itself builds the no-op
    local bus, which never connects)."""
    import tai42_skeleton.app.boot_rules as boot_rules

    monkeypatch.setattr(boot_rules, "_bus_configured", lambda: True)
    monkeypatch.setattr(boot_rules, "_hooks_store_shared", lambda: True)


@pytest.fixture
def fake_app(monkeypatch: pytest.MonkeyPatch) -> _FakeApp:
    app = _FakeApp()
    # The launcher obtains the app via the deferred factory ``instance.build_app``.
    monkeypatch.setattr(backend.instance, "build_app", lambda: app)
    return app


async def test_run_backend_refuses_an_in_memory_hooks_registry_before_the_context(
    fake_app: _FakeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tai42_skeleton.app.boot_rules as boot_rules

    # The backend process matches the events it raises against its own hooks registry;
    # an in-memory one holds none of the hooks registered through the server.
    monkeypatch.setattr(boot_rules, "_hooks_store_shared", lambda: False)

    with pytest.raises(boot_rules.BackendNeedsHooksStoreError, match="HOOKS_REDIS_URL"):
        await backend.run_backend(["worker"])

    assert fake_app.context_entered is False
    assert fake_app.run_backend_args == []


async def test_run_backend_non_worker_enters_context_as_backend_origin(
    fake_app: _FakeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_skeleton.app.bus import WorkerKind

    settings = backend.base_backend_settings()
    monkeypatch.delenv(settings.manifest_key, raising=False)

    # A non-worker backend runtime (e.g. a scheduler ``beat``): it launches inside
    # app_context and joins the worker bus under the ``backend`` origin kind. It forks
    # no job children, so the secret-bearing manifest is NOT exported into it.
    await backend.run_backend(["beat"])

    assert fake_app.context_entered is True
    assert fake_app.run_backend_args == [["beat"]]
    assert fake_app.kind_at_entry is WorkerKind.backend
    assert fake_app.manifest_env_at_entry is None
    assert settings.manifest_key not in os.environ


async def test_run_backend_worker_publishes_manifest_and_enters_context(
    fake_app: _FakeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = backend.base_backend_settings().manifest_key
    monkeypatch.delenv(key, raising=False)

    await backend.run_backend(["worker"])

    assert fake_app.context_entered is True
    assert fake_app.run_backend_args == [["worker"]]
    # The resolved manifest is published before the context opens so the worker's
    # prefork children inherit it.
    assert fake_app.manifest_env_at_entry == '{"backend_module": "my.backend.module"}'
    assert os.environ[key] == '{"backend_module": "my.backend.module"}'


async def test_run_backend_publishes_the_manifest_under_the_key_the_boot_bridge_applied(
    fake_app: _FakeApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_kit.settings import reset_all_settings

    stored_key = "BACKEND_TEST_STORED_MANIFEST_KEY"
    default_key = backend.base_backend_settings().manifest_key
    monkeypatch.delenv("BACKEND_MANIFEST_KEY", raising=False)
    monkeypatch.delenv(default_key, raising=False)
    monkeypatch.delenv(stored_key, raising=False)
    read = fake_app.lifecycle.read_boot_manifest

    async def bridge_then_read(*, kind) -> _FakeManifest:
        # The boot bridge applies a stored ``BACKEND_MANIFEST_KEY`` into the process env
        # and resets the settings caches, as the real bridge does.
        os.environ["BACKEND_MANIFEST_KEY"] = stored_key
        reset_all_settings()
        return await read(kind=kind)

    monkeypatch.setattr(fake_app.lifecycle, "read_boot_manifest", bridge_then_read)
    try:
        await backend.run_backend(["worker"])

        # The forked children read the key the applied env names, so the manifest is there.
        assert os.environ.get(stored_key) == '{"backend_module": "my.backend.module"}'
        assert default_key not in os.environ
    finally:
        os.environ.pop("BACKEND_MANIFEST_KEY", None)
        os.environ.pop(stored_key, None)
        os.environ.pop(default_key, None)
        reset_all_settings()


async def test_run_backend_registers_cli_logging(fake_app: _FakeApp) -> None:
    # This CLI-configured backend process keeps its root logger on the applied env, so
    # ``run_backend`` registers the re-apply after the boot env bridge and on reload.
    await backend.run_backend(["worker"])

    assert _LOGGING_KEY in fake_app.lifecycle.reload_handlers
    assert _LOGGING_KEY in fake_app.boot_env_handlers


async def test_run_backend_installs_process_scope_redactor(fake_app: _FakeApp, monkeypatch: pytest.MonkeyPatch) -> None:
    # This CLI-owned backend process widens the connector-secret redactor to the
    # whole process.
    scopes: list[object] = []
    monkeypatch.setattr(backend, "install_record_redaction", lambda **kwargs: scopes.append(kwargs.get("scope")))

    await backend.run_backend(["beat"])

    assert scopes == ["process"]


@pytest.fixture
def _cli_process_state_of_its_own(monkeypatch: pytest.MonkeyPatch):
    """The process state a real ``run_backend`` changes, as it stood before the test, comes back after it.

    ``run_backend`` registers the CLI process handlers on the process app
    (:func:`~tai42_skeleton.app.instance.register_cli_process_state`), and its boot manifest read
    runs them: the root logger is configured again, fastmcp's settings are read again and
    fastmcp's logger is configured from them. Its lifespan exit leaves the serving core it started
    on the app as the read target of a later read, holding every registration ``start()`` made.
    Every handler registry of the app, the app's serving core, the root and fastmcp loggers and
    every fastmcp setting are restored, so a later boot on the same app runs none of those
    handlers, a later read finds none of those registrations, and the host's logging and fastmcp
    settings are as they were.
    """
    import fastmcp
    from fastmcp.settings import Settings

    from tai42_skeleton.app import instance

    real_app = instance.build_app()
    for name, value in list(vars(real_app).items()):
        if name.endswith("_handlers") and isinstance(value, dict):
            monkeypatch.setattr(real_app, name, dict(value))
    monkeypatch.setattr(real_app, "_scaffold_core", real_app._scaffold_core)
    loggers = [logging.getLogger(), logging.getLogger("fastmcp")]
    saved_loggers = [(logger, logger.handlers[:], logger.level, logger.propagate) for logger in loggers]
    saved_settings = {name: getattr(fastmcp.settings, name) for name in Settings.model_fields}
    try:
        yield
    finally:
        for logger, handlers, level, propagate in saved_loggers:
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate
        for name, value in saved_settings.items():
            fastmcp.settings.set_setting(name, value)


@pytest.mark.usefixtures("_cli_process_state_of_its_own")
async def test_run_backend_beat_reaches_import_registered_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """A beat-style (non-worker) invocation with a REAL app and a plugin module
    that registers its Backend at import: ``run_backend`` must bind the app
    (``app_context`` -> ``start()``) BEFORE the backend module import runs, so
    the import-time ``tai42_app.backends.register_backend`` lands in the bound
    holder and ``launch`` receives the args. Importing the module before the
    bind instead hits the unbound ``tai42_app`` handle (AttributeError) and the
    invocation never reaches ``launch``."""
    from tai42_skeleton.app import instance

    real_app = instance.build_app()
    # The boot manifest read builds the process's bus on the app; the test's own ends with it.
    monkeypatch.setattr(real_app, "_bus", None)
    # The launch declares the backend's drain as the process's exit drain; the test's own ends with it.
    monkeypatch.setattr(real_app, "_exit_drain", ExitDrain())
    saved_backend = real_app._backend_holder._backend
    real_app._backend_holder._backend = None
    # Model a fresh process: the handle is unbound until start() binds it, so an
    # import-before-bind registration raises instead of landing anywhere. The
    # plugin module is therefore NOT imported here — start() imports it, after
    # the bind. The unbind is scoped, so whatever this process had bound is back
    # when the test ends.
    try:
        with tai42_app.bound(None):
            monkeypatch.setattr(
                real_app.config.config_manager,
                "read_manifest",
                lambda: {"backend_module": "tests._fakes.beat_backend"},
            )

            await backend.run_backend(["beat"])

            # start() freshly (re)imported the module inside app_context, so its
            # import-time registration landed and launch received the args.
            plugin = sys.modules["tests._fakes.beat_backend"]
            assert plugin.LaunchRecordingBackend.launched == [["beat"]]
            plugin.LaunchRecordingBackend.launched.clear()
    finally:
        real_app._backend_holder._backend = saved_backend


def test_main_sets_manifest_env_and_runs_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_MANIFEST_PATH", raising=False)

    runs: list = []

    def fake_loop_run(coro):
        coro.close()  # do not execute the launcher coroutine; just record the launch
        runs.append(coro)

    captured: list = []

    def fake_run_backend(args):
        captured.append(args)

        async def _coro() -> None:
            return None

        return _coro()

    monkeypatch.setattr(backend, "run_backend", fake_run_backend)
    # uvloop is the selected loop when importable; mock its blocking run.
    import uvloop

    monkeypatch.setattr(uvloop, "run", fake_loop_run)

    result = CliRunner().invoke(backend.main, ["--manifest-path", "/tmp/m.yaml", "worker", "--flag"])

    assert result.exit_code == 0, result.output
    assert os.environ["TAI_MANIFEST_PATH"] == "/tmp/m.yaml"
    assert len(runs) == 1
    # Unknown options + positional extra args are merged and forwarded.
    assert captured == [["--flag", "worker"]] or captured == [["worker", "--flag"]]


def test_main_fails_when_the_backend_refuses_its_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """A runtime that refuses an option it is handed (``tai backend worker --scheduler x``) fails the command."""
    captured: list[list[str]] = []

    async def refusing_run_backend(args: list[str]) -> None:
        captured.append(list(args))
        raise ValueError("tai backend worker refuses --scheduler")

    monkeypatch.setattr(backend, "run_backend", refusing_run_backend)
    import uvloop

    monkeypatch.setattr(uvloop, "run", asyncio.run)

    result = CliRunner().invoke(backend.main, ["worker", "--scheduler", "x"])

    assert result.exit_code == 1
    assert isinstance(result.exception, ValueError)
    assert str(result.exception) == "tai backend worker refuses --scheduler"
    assert captured == [["worker", "--scheduler", "x"]]


def test_main_falls_back_to_asyncio_when_uvloop_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the ``import uvloop`` to fail so the asyncio fallback branch runs.
    monkeypatch.setitem(sys.modules, "uvloop", None)

    runs: list = []

    def fake_loop_run(coro):
        coro.close()
        runs.append(coro)

    monkeypatch.setattr(backend.asyncio, "run", fake_loop_run)
    monkeypatch.setattr(backend, "run_backend", lambda args: _noop_coro())

    result = CliRunner().invoke(backend.main, ["worker"])

    assert result.exit_code == 0, result.output
    assert len(runs) == 1


@pytest.mark.parametrize(
    ("marker", "expected"),
    [("engine.patch:apply", [("asyncio", True)]), (None, [("uvloop", False)])],
)
def test_main_runs_on_the_standard_loop_with_future_chaining_bound_only_when_started_through_a_prelude(
    monkeypatch: pytest.MonkeyPatch, marker: str | None, expected: list[tuple[str, bool]]
) -> None:
    import uvloop

    ran_on: list[tuple[str, bool]] = []

    def on_asyncio(coro):
        coro.close()
        ran_on.append(("asyncio", future_chaining_bound()))

    def on_uvloop(coro):
        coro.close()
        ran_on.append(("uvloop", future_chaining_bound()))

    if marker is None:
        monkeypatch.delenv(backend.PROCESS_PRELUDE_ENV, raising=False)
    else:
        monkeypatch.setenv(backend.PROCESS_PRELUDE_ENV, marker)
    # Recorded so teardown undoes the binding ``main`` installs.
    monkeypatch.setattr(asyncio.futures, "_chain_future", asyncio.futures._chain_future)  # type: ignore[attr-defined]
    monkeypatch.setattr(backend.asyncio, "run", on_asyncio)
    monkeypatch.setattr(uvloop, "run", on_uvloop)
    monkeypatch.setattr(backend, "run_backend", lambda args: _noop_coro())
    assert not future_chaining_bound()

    result = CliRunner().invoke(backend.main, ["worker"])

    assert result.exit_code == 0, result.output
    assert ran_on == expected


def test_main_restarts_through_the_prelude_the_launch_requires_once_the_run_has_returned(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import uvloop

    events: list[str] = []

    def refused_run(coro):
        coro.close()
        events.append("run returned")
        raise backend.ProcessPreludeRequired("engine.patch:apply")

    def fake_relaunch(name: str, env: Mapping[str, str]):
        events.append(f"relaunch {name}")
        raise SystemExit(0)

    monkeypatch.delenv(backend.PROCESS_PRELUDE_ENV, raising=False)
    monkeypatch.setattr(uvloop, "run", refused_run)
    monkeypatch.setattr(backend, "run_backend", lambda args: _noop_coro())
    monkeypatch.setattr(backend, "setup_logging", lambda settings: None)
    monkeypatch.setattr(backend.logging, "shutdown", lambda: events.append("logging shut down"))
    monkeypatch.setattr(backend.process_prelude, "relaunch", fake_relaunch)

    with caplog.at_level(logging.INFO, logger=backend.logger.name):
        result = CliRunner().invoke(backend.main, ["worker", "-P", "gevent"])

    assert result.exit_code == 0, result.output
    assert events == ["run returned", "logging shut down", "relaunch engine.patch:apply"]
    assert [r.getMessage() for r in caplog.records if "restarting" in r.getMessage()] == [
        "restarting with process prelude engine.patch:apply"
    ]


@pytest.fixture
def _process_env_restored() -> Iterator[None]:
    """Restore ``os.environ``, the applied-key tracker and the settings caches after a run of the boot bridge."""
    from tai42_kit.settings import reset_all_settings

    from tai42_skeleton.app import applied_env

    snapshot = dict(os.environ)
    loaded_before = set(applied_env._loaded_env_keys)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    applied_env._loaded_env_keys = loaded_before
    reset_all_settings()


@pytest.mark.usefixtures("_process_env_restored")
def test_main_restarts_with_the_deployment_env_not_the_env_the_boot_built(monkeypatch: pytest.MonkeyPatch) -> None:
    """The restarted image starts with the env the deployment started the process with, no key the boot applied."""
    from tai42_skeleton.app.instance import app
    from tai42_skeleton.config import deployment_env

    handed: list[dict[str, str]] = []

    def fake_execv(path: str, argv: list[str]) -> None:
        handed.append(dict(os.environ))
        raise SystemExit(0)

    def fake_execve(path: str, argv: list[str], env: Mapping[str, str]) -> None:
        handed.append(dict(env))
        raise SystemExit(0)

    async def booting_then_refused(args: list[str]) -> None:
        await app._apply_stored_env_at_boot(kind=WorkerKind.backend)
        raise backend.ProcessPreludeRequired("engine.patch:apply")

    monkeypatch.delenv(backend.PROCESS_PRELUDE_ENV, raising=False)
    monkeypatch.delenv("STORE_ONLY", raising=False)
    monkeypatch.setattr(app, "_bus", None)
    monkeypatch.setattr(app.config.config_manager, "read_env", lambda: {"STORE_ONLY": "from-store"})
    monkeypatch.setattr(backend, "run_backend", booting_then_refused)
    monkeypatch.setattr(backend, "setup_logging", lambda settings: None)
    monkeypatch.setattr(backend.logging, "shutdown", lambda: None)
    monkeypatch.setattr(os, "execv", fake_execv)
    monkeypatch.setattr(os, "execve", fake_execve)
    # The boot env bridge of this run captures the env ``main`` started it with.
    deployment_env.reset_deployment_env()

    result = CliRunner().invoke(backend.main, ["worker", "-P", "gevent"])

    assert result.exit_code == 0, result.output
    assert os.environ["STORE_ONLY"] == "from-store"
    assert len(handed) == 1
    assert "STORE_ONLY" not in handed[0]
    assert handed[0] == deployment_env.captured_deployment_env()


def test_main_does_not_restart_a_run_that_requires_no_prelude(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvloop

    relaunched: list[str] = []
    monkeypatch.delenv(backend.PROCESS_PRELUDE_ENV, raising=False)
    monkeypatch.setattr(uvloop, "run", lambda coro: coro.close())
    monkeypatch.setattr(backend, "run_backend", lambda args: _noop_coro())
    monkeypatch.setattr(backend.process_prelude, "relaunch", lambda name, env: relaunched.append(name))

    result = CliRunner().invoke(backend.main, ["worker"])

    assert result.exit_code == 0, result.output
    assert relaunched == []


def test_main_bootstraps_env_in_file_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(backend, "config_mode", lambda: "file")
    monkeypatch.setattr(backend, "load_boot_dotenv", lambda path: called.append(True))
    monkeypatch.setattr(backend, "run_backend", lambda args: _noop_coro())

    import uvloop

    monkeypatch.setattr(uvloop, "run", lambda coro: coro.close())

    result = CliRunner().invoke(backend.main, ["worker"])

    assert result.exit_code == 0, result.output
    assert called == [True]


def test_main_skips_env_bootstrap_in_a_non_file_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []
    monkeypatch.setattr(backend, "config_mode", lambda: "external")
    monkeypatch.setattr(backend, "load_boot_dotenv", lambda path: called.append(True))
    monkeypatch.setattr(backend, "run_backend", lambda args: _noop_coro())

    import uvloop

    monkeypatch.setattr(uvloop, "run", lambda coro: coro.close())

    result = CliRunner().invoke(backend.main, ["dashboard"])

    assert result.exit_code == 0, result.output
    assert called == []


def test_main_labels_backend_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    # The backend process's tool metrics must carry ``runtime="backend"`` so a
    # scrape tells them from the server's; ``main`` publishes the label.
    monkeypatch.delenv("PROMETHEUS_RUNTIME", raising=False)
    monkeypatch.setattr(backend, "run_backend", lambda args: _noop_coro())

    import uvloop

    monkeypatch.setattr(uvloop, "run", lambda coro: coro.close())

    result = CliRunner().invoke(backend.main, ["worker"])

    assert result.exit_code == 0, result.output
    assert os.environ["PROMETHEUS_RUNTIME"] == "backend"


def test_main_respects_explicit_runtime_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # An operator-set runtime label is honored, not clobbered.
    monkeypatch.setenv("PROMETHEUS_RUNTIME", "custom")
    monkeypatch.setattr(backend, "run_backend", lambda args: _noop_coro())

    import uvloop

    monkeypatch.setattr(uvloop, "run", lambda coro: coro.close())

    result = CliRunner().invoke(backend.main, ["worker"])

    assert result.exit_code == 0, result.output
    assert os.environ["PROMETHEUS_RUNTIME"] == "custom"


def test_main_publishes_absolute_multiproc_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``main`` sets the multiproc dir env (from the absolute settings value) before
    # ``run_backend`` builds the app and imports prometheus_client. The suite-wide
    # conftest pre-sets ``PROMETHEUS_MULTIPROC_DIR`` (to freeze the mmap value class),
    # so asserting the env ``==`` a fresh ``metrics_settings()`` reading that same env
    # would hold even if ``main`` published nothing. Drop the env and reset the settings
    # cache so ``metrics_settings()`` falls back to its CODED absolute default, then
    # assert ``main`` published exactly that — a value only production's
    # ``activate_multiproc_env()`` can put back on the env.
    import tempfile

    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    reset_all_settings()
    monkeypatch.setattr(backend, "run_backend", lambda args: _noop_coro())

    import uvloop

    monkeypatch.setattr(uvloop, "run", lambda coro: coro.close())

    try:
        result = CliRunner().invoke(backend.main, ["worker"])

        assert result.exit_code == 0, result.output
        # ``main``'s ``activate_multiproc_env()`` must publish the coded default: the
        # host-tempdir absolute path the settings field resolves to with the env unset.
        expected = os.path.join(tempfile.gettempdir(), "tai42_prometheus")
        assert os.environ["PROMETHEUS_MULTIPROC_DIR"] == expected
        assert os.path.isabs(os.environ["PROMETHEUS_MULTIPROC_DIR"])
    finally:
        # ``main`` cached ``metrics_settings()`` at the coded default while the env was
        # unset; clear the cache so it rebuilds against the conftest env (restored by
        # ``cli/conftest.py``) that the rest of the suite relies on.
        reset_all_settings()


# -- stop-signal handling ----------------------------------------------------


@pytest.mark.parametrize("sig", STOP_SIGNALS, ids=lambda sig: sig.name)
async def test_run_backend_stop_signal_cancels_main_and_runs_teardown(
    monkeypatch: pytest.MonkeyPatch, sig: signal.Signals
) -> None:
    # ``run_backend`` subscribes, on each stop signal, a handler that cancels the main task;
    # the cancellation must unwind through ``app_context``'s teardown before surfacing.
    exited = {"value": False}
    entered = asyncio.Event()

    class _SigApp(_StopObservingApp):
        def __init__(self) -> None:
            super().__init__()
            self.config = SimpleNamespace(config_manager=_FakeConfigManager())
            self.lifecycle = _FakeLifecycle()

        def _on_boot_env_applied(self, func):
            return func

        @asynccontextmanager
        async def app_context(self, manifest):
            try:
                yield
            finally:
                # Guaranteed teardown the stop-signal handler must let run.
                exited["value"] = True

        async def run_backend(self, args) -> None:
            entered.set()
            await asyncio.Event().wait()  # block until the cancellation lands

    app = _SigApp()
    monkeypatch.setattr(backend.instance, "build_app", lambda: app)

    task = asyncio.create_task(backend.run_backend(["worker"]))
    await entered.wait()

    # Deliver the signal through the chain the launcher subscribed to.
    assert "backend-main-task-cancel" in signal_chain.subscribers(sig)
    signal_chain._dispatch(sig)

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert exited["value"] is True


@pytest.mark.parametrize(
    ("first", "then"),
    [(signal.SIGTERM, signal.SIGTERM), (signal.SIGINT, signal.SIGTERM), (signal.SIGTERM, signal.SIGINT)],
    ids=["SIGTERM-SIGTERM", "SIGINT-SIGTERM", "SIGTERM-SIGINT"],
)
async def test_the_first_stop_signal_begins_the_workers_leaving_and_a_later_one_begins_nothing(
    fake_app: _FakeApp, first: signal.Signals, then: signal.Signals
) -> None:
    # Run as its own task, as ``main`` runs it: the launcher's main-task cancel then lands on a
    # finished launch, not on this test.
    await asyncio.create_task(backend.run_backend(["worker"]))

    for sig in STOP_SIGNALS:
        assert signal_chain.subscribers(sig) == ("backend-main-task-cancel", "backend-stop-began")
    signal_chain._dispatch(first)
    signal_chain._dispatch(then)

    assert fake_app.stops_began == [(f"{first.name} received", True), (f"{then.name} received", False)]
    assert fake_app.bus.departing == f"{first.name} received"
    await fake_app.bus.leaving_announced()


def test_main_catches_the_stop_signal_cancellation_as_clean_exit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # ``main`` converts the deliberate stop-signal cancellation into a clean exit (exit code 0),
    # not a crash, and its line names no one signal: either stop signal cancels the main task.
    # Whether the stop finished is the stop's own line to say, so this one claims no teardown.
    monkeypatch.delenv("TAI_MANIFEST_PATH", raising=False)

    def fake_loop_run(coro):
        coro.close()
        raise asyncio.CancelledError

    monkeypatch.setattr(backend, "run_backend", lambda args: _noop_coro())
    # The process logging setup would replace the root handlers the capture rides on.
    monkeypatch.setattr(backend, "setup_logging", lambda settings: None)

    import uvloop

    monkeypatch.setattr(uvloop, "run", fake_loop_run)

    with caplog.at_level(logging.INFO, logger=backend.logger.name):
        result = CliRunner().invoke(backend.main, ["worker"])

    assert result.exit_code == 0, result.output
    exits = [record.getMessage() for record in caplog.records if record.name == backend.logger.name]
    assert exits[-1] == "stop signal received — backend worker exiting"


async def _noop_coro() -> None:
    return None


class _ReadyLifecycle(_FakeLifecycle):
    """Adds the two members a backend base reaches through the ``tai42_app``
    handle: the boot-readiness latch and the post-apply fleet-op hook."""

    async def wait_until_ready(self) -> None:
        return None

    def on_fleet_op_applied(self, func):
        return func


class _DrainingWorker(BackendRuntime):
    """A runtime whose body returns only once its drain was requested, so a
    severed body and a drained one are told apart."""

    name = "worker"
    mode = ExecutionMode.on_loop
    consumes_work = True
    liveness = ProgressLiveness()
    latest: ClassVar[_DrainingWorker | None] = None

    def __init__(self) -> None:
        self.running = asyncio.Event()
        self.stopped = asyncio.Event()
        self.drained = False

    @classmethod
    def from_args(cls, args) -> _DrainingWorker:
        cls.latest = cls()
        return cls.latest

    async def run_on_loop(self) -> None:
        self.running.set()
        await self.stopped.wait()
        self.drained = True

    def request_drain(self) -> None:
        self.stopped.set()


class _DrainingBackend(ManagedBackend):
    label = "chain-test"
    runtimes: ClassVar[Mapping[str, type[BackendRuntime]]] = {"worker": _DrainingWorker}


@pytest.mark.parametrize("sig", STOP_SIGNALS, ids=lambda sig: sig.name)
async def test_run_backend_stop_signal_drains_the_backend_and_still_runs_teardown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sig: signal.Signals
) -> None:
    """One stop signal, two subscribers — the composed-chain regression.

    The backend base wires its own drain onto each stop signal while the launcher's
    main-task cancellation is already there. With a raw ``add_signal_handler``
    the second install destroys the first, so exactly one of these two
    assertions fails: either in-flight work is severed, or ``app_context``
    teardown is skipped. Both must hold.
    """
    _DrainingWorker.latest = None
    monkeypatch.setenv("TAI_BACKEND_LIVENESS_PATH", str(tmp_path / "liveness"))

    class _DrainApp(_StopObservingApp):
        def __init__(self) -> None:
            super().__init__()
            self.config = SimpleNamespace(config_manager=_FakeConfigManager())
            self.lifecycle = _ReadyLifecycle()
            self.torn_down = False

        def _on_boot_env_applied(self, func):
            return func

        @asynccontextmanager
        async def app_context(self, manifest):
            try:
                yield
            finally:
                self.torn_down = True

        async def run_backend(self, args) -> None:
            await _DrainingBackend().launch(args)

    app = _DrainApp()
    monkeypatch.setattr(backend.instance, "build_app", lambda: app)

    with tai42_app.bound(app):
        task = asyncio.create_task(backend.run_backend(["worker"]))
        worker = await _await_worker()

        # BOTH are on the signal at once. A raw ``add_signal_handler`` on either
        # side would leave exactly one here — silently, since asyncio reports no
        # collision — and one of the two assertions below would then fail.
        assert signal_chain.subscribers(sig) == (
            "backend-main-task-cancel",
            "backend-stop-began",
            "chain-test-worker-drain",
        )
        signal_chain._dispatch(sig)

        with pytest.raises(asyncio.CancelledError):
            await task

    assert worker.drained is True
    assert app.torn_down is True
    # The base's wiring is scoped to the run body and left; the launcher's
    # subscription survived that registration AND that departure, which raw
    # ``add_signal_handler``/``remove_signal_handler`` could not manage.
    assert signal_chain.subscribers(sig) == ("backend-main-task-cancel", "backend-stop-began")
    assert app.stops_began == [(f"{sig.name} received", True)]


async def _await_worker() -> _DrainingWorker:
    for _ in range(1000):
        worker = _DrainingWorker.latest
        if worker is not None and worker.running.is_set():
            return worker
        await asyncio.sleep(0)
    raise AssertionError("the backend runtime never started")
