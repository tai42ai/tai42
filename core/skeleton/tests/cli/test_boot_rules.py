"""Worker-bus boot rules.

Three shapes refuse to start without the bus:

* more than one server worker (siblings would serve stale config after a reload),
* a task backend registered in the manifest (server + backend-runtime must
  converge), and
* any non-``file`` config mode (an external provider serves shared config).

Every refusal names ``TAI_BUS_REDIS_URL`` so the operator knows the fix, and the
shared-config check runs BEFORE any config-manager construction so a busless
shared-config boot fails on the bus var rather than first on the provider's
connection. The single-worker, file-mode, no-backend, no-bus shape is supported and
runs on ``WorkerBus.local``.

A bus-enabled config whose fleet apply window (``TAI_BUS_APPLY_TIMEOUT``) is below the
retire's drain floor (three drains of the declared drain budget) refuses at boot and on
every reload, naming both settings and the floor.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app import boot_rules
from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app.lifecycle import TaiMCPLifecycleMixin, lifespan
from tai42_skeleton.app.server import ServingCore
from tai42_skeleton.cli import mcp_app
from tai42_skeleton.manifest import Manifest as ConcreteManifest

if TYPE_CHECKING:
    from tai42_skeleton.manifest import Manifest


@pytest.fixture(autouse=True)
def _reset_settings_after() -> Iterator[None]:
    yield
    reset_all_settings()


def _busless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    reset_all_settings()


def _with_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_BUS_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()


class _Manifest:
    def __init__(self, backend_module: str) -> None:
        self.backend_module = backend_module


# -- workers rule -------------------------------------------------------------


def test_multi_worker_busless_refuses_naming_the_bus_var() -> None:
    with pytest.raises(boot_rules.WorkersNeedBusError, match="TAI_BUS_REDIS_URL") as exc:
        boot_rules.check_workers_need_bus(workers=4, bus_configured=False)
    assert "4 workers" in str(exc.value)
    assert isinstance(exc.value, boot_rules.ConfigInvariantError)


def test_multi_worker_with_bus_is_allowed() -> None:
    boot_rules.check_workers_need_bus(workers=4, bus_configured=True)


def test_single_worker_busless_is_allowed() -> None:
    boot_rules.check_workers_need_bus(workers=1, bus_configured=False)


def test_the_bus_face_refuses_the_stamped_worker_count_with_no_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    _busless(monkeypatch)
    monkeypatch.setenv("TAI_SERVE_WORKERS", "3")
    with pytest.raises(RuntimeError, match=r"3 workers with no worker bus.*TAI_BUS_REDIS_URL"):
        boot_rules.require_bus_for_workers()


def test_the_bus_face_allows_the_stamped_worker_count_with_the_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    monkeypatch.setenv("TAI_SERVE_WORKERS", "3")
    boot_rules.require_bus_for_workers()


def test_the_bus_face_allows_an_unstamped_busless_process(monkeypatch: pytest.MonkeyPatch) -> None:
    _busless(monkeypatch)
    monkeypatch.delenv("TAI_SERVE_WORKERS", raising=False)
    boot_rules.require_bus_for_workers()


# -- shared stores for more than one server worker -----------------------------
#
# The sub-MCP registration store and the hooks registry are per-process when in-memory, so a
# server running more than one worker needs both shared. The worker count is the one
# ``tai serve`` stamps into ``TAI_SERVE_WORKERS`` for its workers; the store settings are the
# ones the store factories read, after the stored env is applied.


def _unset(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    # Set first so the monkeypatch records the original value and restores it, even when a
    # stored-env apply in the test writes the name into ``os.environ`` afterwards.
    for name in names:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


def _no_sub_mcp_store(monkeypatch: pytest.MonkeyPatch) -> None:
    _unset(monkeypatch, "SUB_MCP_REDIS_URL", "TAI_DEFAULT_REDIS_URL")
    reset_all_settings()


def _no_hooks_store(monkeypatch: pytest.MonkeyPatch) -> None:
    _unset(monkeypatch, "HOOKS_REDIS_URL", "TAI_DEFAULT_REDIS_URL")
    reset_all_settings()


def _serve_workers(monkeypatch: pytest.MonkeyPatch, workers: int) -> None:
    monkeypatch.setenv("TAI_SERVE_WORKERS", str(workers))


def test_the_serve_worker_count_is_the_stamped_one(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_workers(monkeypatch, 4)
    assert boot_rules.serve_workers() == 4


def test_a_process_without_the_stamp_counts_as_one_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    _unset(monkeypatch, "TAI_SERVE_WORKERS")
    assert boot_rules.serve_workers() == 1


@pytest.mark.parametrize("value", ["two", "0", "-1", ""])
def test_a_malformed_serve_worker_count_raises_naming_the_setting(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("TAI_SERVE_WORKERS", value)
    with pytest.raises(RuntimeError, match="TAI_SERVE_WORKERS"):
        boot_rules.serve_workers()


def test_multi_worker_without_sub_mcp_store_refuses_naming_the_setting() -> None:
    with pytest.raises(boot_rules.WorkersNeedSubMcpStoreError, match="SUB_MCP_REDIS_URL") as exc:
        boot_rules.check_workers_need_sub_mcp_store(workers=2, sub_mcp_store_shared=False)
    assert "2 workers" in str(exc.value)
    assert "TAI_DEFAULT_REDIS_URL" in str(exc.value)


def test_multi_worker_with_a_shared_sub_mcp_store_is_allowed() -> None:
    boot_rules.check_workers_need_sub_mcp_store(workers=4, sub_mcp_store_shared=True)


def test_single_worker_without_sub_mcp_store_is_allowed() -> None:
    boot_rules.check_workers_need_sub_mcp_store(workers=1, sub_mcp_store_shared=False)


def test_multi_worker_without_hooks_store_refuses_naming_the_setting() -> None:
    with pytest.raises(boot_rules.WorkersNeedHooksStoreError, match="HOOKS_REDIS_URL") as exc:
        boot_rules.check_workers_need_hooks_store(workers=2, hooks_store_shared=False)
    assert "2 workers" in str(exc.value)
    assert "TAI_DEFAULT_REDIS_URL" in str(exc.value)


def test_multi_worker_with_a_shared_hooks_store_is_allowed() -> None:
    boot_rules.check_workers_need_hooks_store(workers=4, hooks_store_shared=True)


def test_single_worker_without_hooks_store_is_allowed() -> None:
    boot_rules.check_workers_need_hooks_store(workers=1, hooks_store_shared=False)


def test_the_sub_mcp_face_refuses_the_stamped_worker_count_over_an_in_memory_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve_workers(monkeypatch, 3)
    _no_sub_mcp_store(monkeypatch)
    with pytest.raises(boot_rules.WorkersNeedSubMcpStoreError, match="3 workers"):
        boot_rules.require_sub_mcp_store_for_workers()


def test_the_sub_mcp_face_allows_sub_mcp_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_workers(monkeypatch, 4)
    _no_sub_mcp_store(monkeypatch)
    monkeypatch.setenv("SUB_MCP_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()
    boot_rules.require_sub_mcp_store_for_workers()


def test_the_sub_mcp_face_allows_the_default_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    # The sub-MCP connection falls back to TAI_DEFAULT_REDIS_URL, which makes the store shared.
    _serve_workers(monkeypatch, 4)
    _no_sub_mcp_store(monkeypatch)
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()
    boot_rules.require_sub_mcp_store_for_workers()


def test_the_hooks_face_refuses_the_stamped_worker_count_over_an_in_memory_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve_workers(monkeypatch, 3)
    _no_hooks_store(monkeypatch)
    with pytest.raises(boot_rules.WorkersNeedHooksStoreError, match="3 workers"):
        boot_rules.require_hooks_store_for_workers()


def test_the_hooks_face_allows_hooks_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_workers(monkeypatch, 4)
    _no_hooks_store(monkeypatch)
    monkeypatch.setenv("HOOKS_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()
    boot_rules.require_hooks_store_for_workers()


def test_the_hooks_face_allows_the_default_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    # The hooks connection falls back to TAI_DEFAULT_REDIS_URL, which makes the registry shared.
    _serve_workers(monkeypatch, 4)
    _no_hooks_store(monkeypatch)
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()
    boot_rules.require_hooks_store_for_workers()


def test_the_store_faces_allow_an_unstamped_process_over_in_memory_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    _unset(monkeypatch, "TAI_SERVE_WORKERS")
    _no_sub_mcp_store(monkeypatch)
    _no_hooks_store(monkeypatch)
    boot_rules.require_sub_mcp_store_for_workers()
    boot_rules.require_hooks_store_for_workers()


def test_run_mcp_app_stamps_the_worker_count_and_leaves_the_workers_rules_to_the_workers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The master cannot read the stored env the workers apply, so it hands the count to
    # them and launches; each worker refuses at its own boot if it has no bus or its stores
    # are in-memory. The launch wipes the multiproc dir, so it gets this test's own dir
    # rather than the one this process's counters write to.
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path / "prometheus"))
    _busless(monkeypatch)
    _no_sub_mcp_store(monkeypatch)
    _no_hooks_store(monkeypatch)
    _unset(monkeypatch, "TAI_SERVE_WORKERS")
    launched: list[dict] = []
    monkeypatch.setattr(mcp_app, "run_workers", lambda target, **kw: launched.append(kw))
    mcp_app.run_mcp_app(
        manifest_path="unused.yml",
        transport="http",
        host="127.0.0.1",
        port=8000,
        workers=4,
        stateless_http=True,
    )
    assert [kw["workers"] for kw in launched] == [4]
    assert mcp_app.os.environ["TAI_SERVE_WORKERS"] == "4"


def test_run_mcp_app_under_reload_stamps_the_one_process_it_serves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The reload supervisor takes precedence over the workers and serves one process, so
    # that process is not held to the multi-worker store rules.
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path / "prometheus"))
    _unset(monkeypatch, "TAI_SERVE_WORKERS")
    launched: list[dict] = []
    monkeypatch.setattr(mcp_app, "run_workers", lambda target, **kw: launched.append(kw))
    mcp_app.run_mcp_app(
        manifest_path="unused.yml",
        transport="http",
        host="127.0.0.1",
        port=8000,
        workers=2,
        stateless_http=True,
        uvicorn_kwargs={"reload": True},
    )
    assert [kw["reload"] for kw in launched] == [True]
    assert mcp_app.os.environ["TAI_SERVE_WORKERS"] == "1"


# -- backend rule -------------------------------------------------------------


def test_registered_backend_busless_refuses_naming_the_bus_var(monkeypatch: pytest.MonkeyPatch) -> None:
    _busless(monkeypatch)
    with pytest.raises(RuntimeError, match="TAI_BUS_REDIS_URL"):
        boot_rules.require_bus_for_backend(cast("Manifest", _Manifest("myapp.backend")))


def test_registered_backend_with_bus_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    boot_rules.require_bus_for_backend(cast("Manifest", _Manifest("myapp.backend")))


def test_no_backend_busless_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _busless(monkeypatch)
    boot_rules.require_bus_for_backend(cast("Manifest", _Manifest("")))


# -- backend hooks-store rule ---------------------------------------------------
#
# A registered task backend runs in its own process (``tai backend``), and the platform
# events it raises (the interactions reaper, the media reaper, the monitoring health watch)
# are matched against that process's hooks registry. An in-memory registry there holds
# none of the hooks registered through the server, so those events never run a hook.


def test_registered_backend_without_hooks_store_refuses_naming_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    _no_hooks_store(monkeypatch)
    with pytest.raises(boot_rules.BackendNeedsHooksStoreError, match="HOOKS_REDIS_URL") as exc:
        boot_rules.require_hooks_store_for_backend(cast("Manifest", _Manifest("myapp.backend")))
    assert "'myapp.backend'" in str(exc.value)
    assert "TAI_DEFAULT_REDIS_URL" in str(exc.value)


def test_registered_backend_with_hooks_redis_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_hooks_store(monkeypatch)
    monkeypatch.setenv("HOOKS_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()
    boot_rules.require_hooks_store_for_backend(cast("Manifest", _Manifest("myapp.backend")))


def test_registered_backend_with_the_default_redis_backs_the_hooks_store(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_hooks_store(monkeypatch)
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()
    boot_rules.require_hooks_store_for_backend(cast("Manifest", _Manifest("myapp.backend")))


def test_no_backend_without_hooks_store_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_hooks_store(monkeypatch)
    boot_rules.require_hooks_store_for_backend(cast("Manifest", _Manifest("")))


# -- shared-config rule (any non-file mode; fails on the bus var, BEFORE the
#    external provider is ever contacted) ---------------------------------------


def test_shared_config_mode_busless_refuses_on_the_bus_var_not_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_CONFIG_MODE", "external")
    _busless(monkeypatch)
    with pytest.raises(RuntimeError) as exc:
        boot_rules.require_bus_for_shared_config()
    message = str(exc.value)
    # The refusal names the bus var and the active mode, and runs before the config
    # manager is ever constructed — so it never reflects a provider connection error.
    assert "TAI_BUS_REDIS_URL" in message
    assert "external" in message


def test_shared_config_mode_with_bus_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_CONFIG_MODE", "external")
    _with_bus(monkeypatch)
    boot_rules.require_bus_for_shared_config()


def test_file_mode_busless_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_CONFIG_MODE", "file")
    _busless(monkeypatch)
    boot_rules.require_bus_for_shared_config()


def test_run_mcp_app_shared_config_busless_raises_on_the_bus_var(monkeypatch: pytest.MonkeyPatch) -> None:
    # A busless shared-config `tai serve` boot refuses naming TAI_BUS_REDIS_URL — the
    # check precedes config-manager construction, so no provider is contacted first.
    monkeypatch.setenv("TAI_CONFIG_MODE", "external")
    _busless(monkeypatch)
    with pytest.raises(RuntimeError) as exc:
        mcp_app.run_mcp_app(
            manifest_path="unused.yml",
            transport="http",
            host="127.0.0.1",
            port=8000,
            workers=1,
        )
    assert "TAI_BUS_REDIS_URL" in str(exc.value)


# -- the mutate-time translation ------------------------------------------------


class _RefusedError(Exception):
    pass


def test_translate_config_invariants_re_raises_backend_needs_bus_as_the_given_error() -> None:
    with (
        pytest.raises(_RefusedError, match="Set TAI_BUS_REDIS_URL") as exc,
        boot_rules.translate_config_invariants(_RefusedError),
    ):
        boot_rules.check_backend_needs_bus(backend_module="pkg.backend", bus_configured=False)
    assert isinstance(exc.value.__cause__, boot_rules.BackendNeedsBusError)


def test_translate_config_invariants_re_raises_backend_needs_hooks_store_as_the_given_error() -> None:
    with (
        pytest.raises(_RefusedError, match="HOOKS_REDIS_URL") as exc,
        boot_rules.translate_config_invariants(_RefusedError),
    ):
        boot_rules.check_backend_needs_hooks_store(backend_module="pkg.backend", hooks_store_shared=False)
    assert isinstance(exc.value.__cause__, boot_rules.BackendNeedsHooksStoreError)


def test_translate_config_invariants_re_raises_a_short_apply_window_as_the_given_error() -> None:
    with (
        pytest.raises(_RefusedError, match="TAI_BUS_APPLY_TIMEOUT") as exc,
        boot_rules.translate_config_invariants(_RefusedError),
    ):
        boot_rules.check_apply_window_covers_drain(bus_enabled=True, apply_timeout=20.0, drain_budget=10.0)
    assert isinstance(exc.value.__cause__, boot_rules.ApplyWindowBelowDrainError)


def test_translate_config_invariants_lets_other_errors_through() -> None:
    with pytest.raises(KeyError), boot_rules.translate_config_invariants(_RefusedError):
        raise KeyError("other")


def test_translate_config_invariants_is_silent_without_an_error() -> None:
    with boot_rules.translate_config_invariants(_RefusedError):
        boot_rules.check_backend_needs_bus(backend_module="pkg.backend", bus_configured=True)


# -- apply-window rule ----------------------------------------------------------


def test_an_apply_window_below_three_drain_budgets_is_refused_naming_both_settings_and_the_floor() -> None:
    with pytest.raises(boot_rules.ApplyWindowBelowDrainError) as exc:
        boot_rules.check_apply_window_covers_drain(bus_enabled=True, apply_timeout=29.5, drain_budget=10.0)
    message = str(exc.value)
    assert "TAI_BUS_APPLY_TIMEOUT=29.5" in message
    assert "TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS=10" in message
    assert "30" in message
    assert isinstance(exc.value, boot_rules.ConfigInvariantError)
    assert isinstance(exc.value, RuntimeError)


def test_an_apply_window_equal_to_three_drain_budgets_is_allowed() -> None:
    boot_rules.check_apply_window_covers_drain(bus_enabled=True, apply_timeout=30.0, drain_budget=10.0)


def test_an_apply_window_above_three_drain_budgets_is_allowed() -> None:
    boot_rules.check_apply_window_covers_drain(bus_enabled=True, apply_timeout=270.5, drain_budget=90.0)


def test_a_busless_config_uses_no_apply_window_and_is_allowed() -> None:
    boot_rules.check_apply_window_covers_drain(bus_enabled=False, apply_timeout=1.0, drain_budget=90.0)


def _window_and_drain(monkeypatch: pytest.MonkeyPatch, *, window: str, drain: str) -> None:
    monkeypatch.setenv("TAI_BUS_APPLY_TIMEOUT", window)
    monkeypatch.setenv("TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS", drain)
    reset_all_settings()


def test_the_boot_face_allows_a_window_at_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    _window_and_drain(monkeypatch, window="30", drain="10")
    boot_rules.require_apply_window_covers_drain()


def test_the_boot_face_allows_a_short_window_on_the_busless_local_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    _busless(monkeypatch)
    _window_and_drain(monkeypatch, window="20", drain="10")
    boot_rules.require_apply_window_covers_drain()


def test_the_platform_defaults_satisfy_the_apply_window_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    monkeypatch.delenv("TAI_BUS_APPLY_TIMEOUT", raising=False)
    monkeypatch.delenv("TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS", raising=False)
    reset_all_settings()
    boot_rules.require_apply_window_covers_drain()


class _Lifecycle(TaiMCPLifecycleMixin):
    """A lifecycle with no event server: ``start()`` records its manifest instead of re-initialising."""

    def __init__(self) -> None:
        super().__init__()
        self.started_with: object | None = None
        self._building = ServingCore(cast("object", self), args=(), auth=None, kwargs={"name": "boot-rules"})  # type: ignore[arg-type]

    def _mcp_tools(self, config, tools, preset_names):  # abstract in the mixin
        pass

    def start(self, manifest):
        self.started_with = manifest


async def test_app_context_refuses_a_window_below_the_drain_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    _window_and_drain(monkeypatch, window="20", drain="10")
    lifecycle = _Lifecycle()
    with pytest.raises(boot_rules.ApplyWindowBelowDrainError, match="TAI_BUS_APPLY_TIMEOUT=20"):
        async with lifecycle.app_context(ConcreteManifest()):
            pytest.fail("the process booted with an apply window below the drain floor")
    assert lifecycle.started_with is None


def test_a_reload_refuses_a_window_below_the_drain_floor_before_rebuilding(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    _window_and_drain(monkeypatch, window="20", drain="10")
    lifecycle = _Lifecycle()
    with pytest.raises(boot_rules.ApplyWindowBelowDrainError, match="TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS=10"):
        lifecycle._reload_registries(ConcreteManifest())
    # Refused before the registries rebuild, so the build primitive discards it whole.
    assert lifecycle.started_with is None


async def test_app_context_refuses_a_backend_without_hooks_store(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    _no_hooks_store(monkeypatch)
    lifecycle = _Lifecycle()
    with pytest.raises(boot_rules.BackendNeedsHooksStoreError, match="HOOKS_REDIS_URL"):
        async with lifecycle.app_context(ConcreteManifest(backend_module="myapp.backend")):
            pytest.fail("the process booted a task backend over an in-memory hooks registry")
    assert lifecycle.started_with is None


def test_a_reload_refuses_a_backend_without_hooks_store_before_rebuilding(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    _no_hooks_store(monkeypatch)
    lifecycle = _Lifecycle()
    with pytest.raises(boot_rules.BackendNeedsHooksStoreError, match="HOOKS_REDIS_URL"):
        lifecycle._reload_registries(ConcreteManifest(backend_module="myapp.backend"))
    assert lifecycle.started_with is None


# -- the workers rules at the worker's boot and on every reload -----------------


class _BootRulesPassedError(Exception):
    """Raised in place of the supervision check, the boot step that follows every boot rule."""


class _StoredEnv:
    """A config manager whose stored env is ``env``."""

    def __init__(self, env: dict[str, str]) -> None:
        self._env = env

    def read_env(self) -> dict[str, str]:
        return dict(self._env)


def _booting_lifecycle(monkeypatch: pytest.MonkeyPatch, stored_env: dict[str, str]) -> _Lifecycle:
    """A lifecycle over ``stored_env`` whose boot stops with :class:`_BootRulesPassedError` after its boot rules."""

    def booted() -> None:
        raise _BootRulesPassedError

    monkeypatch.setattr(lifespan, "require_supervision_declared", booted)
    # The boot env bridge drops every key the last apply loaded that ``stored_env`` lacks, so it
    # starts from no loaded keys, and each stored key is restored after the test.
    monkeypatch.setattr(epoch_mod, "_loaded_env_keys", set())
    for name in stored_env:
        if name in os.environ:
            monkeypatch.setenv(name, os.environ[name])
        else:
            _unset(monkeypatch, name)
    lifecycle = _Lifecycle()
    lifecycle._config_manager = cast("object", _StoredEnv(stored_env))  # type: ignore[assignment]
    return lifecycle


def _shared_stores_only_in(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    # The container env carries neither store; the stored env carries both.
    _with_bus(monkeypatch)
    _no_sub_mcp_store(monkeypatch)
    _no_hooks_store(monkeypatch)
    return {"SUB_MCP_REDIS_URL": "redis://localhost:6379/1", "HOOKS_REDIS_URL": "redis://localhost:6379/2"}


async def test_a_worker_boots_with_its_shared_stores_only_in_the_stored_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_workers(monkeypatch, 2)
    lifecycle = _booting_lifecycle(monkeypatch, _shared_stores_only_in(monkeypatch))
    lifecycle._apply_stored_env_at_boot()
    with pytest.raises(_BootRulesPassedError):
        async with lifecycle.app_context(ConcreteManifest()):
            pytest.fail("the boot stops after its boot rules")


@pytest.mark.parametrize(
    ("emptied", "error"),
    [
        ("SUB_MCP_REDIS_URL", boot_rules.WorkersNeedSubMcpStoreError),
        ("HOOKS_REDIS_URL", boot_rules.WorkersNeedHooksStoreError),
    ],
)
async def test_a_worker_refuses_a_stored_empty_store_url_over_the_container_value(
    monkeypatch: pytest.MonkeyPatch, emptied: str, error: type[Exception]
) -> None:
    # The container env shares both stores; the stored env, which wins at the worker's boot,
    # empties one of them, so that store comes up in-memory in every worker.
    _with_bus(monkeypatch)
    _serve_workers(monkeypatch, 2)
    _unset(monkeypatch, "TAI_DEFAULT_REDIS_URL")
    monkeypatch.setenv("SUB_MCP_REDIS_URL", "redis://localhost:6379/1")
    monkeypatch.setenv("HOOKS_REDIS_URL", "redis://localhost:6379/2")
    reset_all_settings()
    lifecycle = _booting_lifecycle(monkeypatch, {emptied: ""})
    lifecycle._apply_stored_env_at_boot()
    with pytest.raises(error, match=f"{emptied}.*2 workers|2 workers.*{emptied}"):
        async with lifecycle.app_context(ConcreteManifest()):
            pytest.fail("the worker booted over an in-memory store")
    assert lifecycle.started_with is None


async def test_a_single_worker_boots_over_in_memory_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_workers(monkeypatch, 1)
    _with_bus(monkeypatch)
    _no_sub_mcp_store(monkeypatch)
    _no_hooks_store(monkeypatch)
    lifecycle = _booting_lifecycle(monkeypatch, {})
    lifecycle._apply_stored_env_at_boot()
    with pytest.raises(_BootRulesPassedError):
        async with lifecycle.app_context(ConcreteManifest()):
            pytest.fail("the boot stops after its boot rules")


@pytest.mark.parametrize(
    ("no_store", "error", "setting"),
    [
        (_no_sub_mcp_store, boot_rules.WorkersNeedSubMcpStoreError, "SUB_MCP_REDIS_URL"),
        (_no_hooks_store, boot_rules.WorkersNeedHooksStoreError, "HOOKS_REDIS_URL"),
    ],
)
def test_a_reload_refuses_an_in_memory_store_on_a_multi_worker_server_before_rebuilding(
    monkeypatch: pytest.MonkeyPatch,
    no_store: Callable[[pytest.MonkeyPatch], None],
    error: type[Exception],
    setting: str,
) -> None:
    _with_bus(monkeypatch)
    _serve_workers(monkeypatch, 2)
    monkeypatch.setenv("SUB_MCP_REDIS_URL", "redis://localhost:6379/1")
    monkeypatch.setenv("HOOKS_REDIS_URL", "redis://localhost:6379/2")
    no_store(monkeypatch)
    lifecycle = _Lifecycle()
    with pytest.raises(error, match=setting):
        lifecycle._reload_registries(ConcreteManifest())
    assert lifecycle.started_with is None


def _shared_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    _unset(monkeypatch, "TAI_DEFAULT_REDIS_URL")
    monkeypatch.setenv("SUB_MCP_REDIS_URL", "redis://localhost:6379/1")
    monkeypatch.setenv("HOOKS_REDIS_URL", "redis://localhost:6379/2")
    reset_all_settings()


async def test_a_worker_boots_with_its_bus_only_in_the_stored_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_workers(monkeypatch, 2)
    _busless(monkeypatch)
    _shared_stores(monkeypatch)
    lifecycle = _booting_lifecycle(monkeypatch, {"TAI_BUS_REDIS_URL": "redis://localhost:6379/0"})
    lifecycle._apply_stored_env_at_boot()
    with pytest.raises(_BootRulesPassedError):
        async with lifecycle.app_context(ConcreteManifest()):
            pytest.fail("the boot stops after its boot rules")


async def test_a_worker_refuses_a_stored_empty_bus_url_over_the_container_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The container env configures the bus; the stored env, which wins at the worker's boot,
    # empties it, so every worker would come up on its own local bus.
    _serve_workers(monkeypatch, 2)
    _with_bus(monkeypatch)
    _shared_stores(monkeypatch)
    lifecycle = _booting_lifecycle(monkeypatch, {"TAI_BUS_REDIS_URL": ""})
    lifecycle._apply_stored_env_at_boot()
    with pytest.raises(RuntimeError, match=r"2 workers with no worker bus.*TAI_BUS_REDIS_URL"):
        async with lifecycle.app_context(ConcreteManifest()):
            pytest.fail("the worker booted with no bus")
    assert lifecycle.started_with is None


def test_a_reload_refuses_a_busless_multi_worker_server_before_rebuilding(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_workers(monkeypatch, 2)
    _busless(monkeypatch)
    _shared_stores(monkeypatch)
    lifecycle = _Lifecycle()
    with pytest.raises(RuntimeError, match=r"2 workers with no worker bus.*TAI_BUS_REDIS_URL"):
        lifecycle._reload_registries(ConcreteManifest())
    assert lifecycle.started_with is None
