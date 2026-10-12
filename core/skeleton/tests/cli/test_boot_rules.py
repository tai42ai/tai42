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

from collections.abc import Iterator
from typing import TYPE_CHECKING, cast

import pytest
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app import boot_rules
from tai42_skeleton.app.lifecycle import TaiMCPLifecycleMixin
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


def test_multi_worker_busless_refuses_naming_the_bus_var(monkeypatch: pytest.MonkeyPatch) -> None:
    _busless(monkeypatch)
    with pytest.raises(RuntimeError, match="TAI_BUS_REDIS_URL") as exc:
        boot_rules.require_bus_for_workers(4)
    assert "4 workers" in str(exc.value)


def test_multi_worker_with_bus_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    boot_rules.require_bus_for_workers(4)


def test_single_worker_busless_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _busless(monkeypatch)
    boot_rules.require_bus_for_workers(1)


def test_run_mcp_app_serve_dash_w4_busless_raises_naming_the_bus_var(monkeypatch: pytest.MonkeyPatch) -> None:
    _busless(monkeypatch)
    with pytest.raises(RuntimeError, match="TAI_BUS_REDIS_URL"):
        mcp_app.run_mcp_app(
            manifest_path="unused.yml",
            transport="http",
            host="127.0.0.1",
            port=8000,
            workers=4,
            stateless_http=True,
        )


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

    def _mcp_tools(self, config, tools):  # abstract in the mixin
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
