"""Boot-time worker-bus invariants — refuse to start a deployment the worker bus cannot serve.

The worker bus is what keeps sibling workers from serving stale config after a
reload, so three shapes require ``TAI_BUS_REDIS_URL``:

* more than one server worker (siblings would diverge on a reload),
* a task backend registered in the manifest (the backend-runtime and server
  processes must converge), and
* any non-``file`` config mode (an external provider serves shared config to
  every instance, which must converge on a reload, and one instance cannot see
  how many siblings share it).

A single-worker, file-mode process with no backend is the supported busless shape
and runs on :meth:`WorkerBus.local`.

The shared-config and workers checks read only boot-fixed env / args, so they run
at the CLI BEFORE the config manager is constructed — a busless shared-config boot
then refuses naming ``TAI_BUS_REDIS_URL`` rather than failing first on the external
provider's connection. The backend check needs the manifest, so it also runs at the ``app_context`` seam that
both ``tai serve`` and ``tai backend`` cross, and re-runs there on every reload.

A bus-enabled deployment's fleet apply window (``TAI_BUS_APPLY_TIMEOUT``) must cover the
retire's drain floor — three drains of the declared drain budget
(:func:`~tai42_skeleton.app.epoch_retire.retire_drain_floor_seconds`) — so a worker draining
real work answers inside the window. The window and the budget are checked against the
resolved config at boot, at every reload, and at mutation time.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from tai42_skeleton.app.bus_settings import bus_settings
from tai42_skeleton.app.epoch_retire import retire_drain_floor_seconds
from tai42_skeleton.config.config_mode import ConfigMode, config_mode

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from tai42_skeleton.app.lifecycle.drain import DrainBudgetRegistry
    from tai42_skeleton.manifest import Manifest

_BUS_VAR = "TAI_BUS_REDIS_URL"
_APPLY_WINDOW_VAR = "TAI_BUS_APPLY_TIMEOUT"
_DRAIN_BUDGET_VAR = "TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS"


class ConfigInvariantError(RuntimeError):
    """A resolved config breaks an invariant that relates the worker bus to the rest of the config.

    A ``RuntimeError`` so a boot-time or reload-time refusal fails loudly; a mutate-time door
    maps it to its own actionable error through :func:`translate_config_invariants`.
    """


class BackendNeedsBusError(ConfigInvariantError):
    """A resolved config registers a task backend while no worker bus is configured.

    The backend-runtime and server processes must converge on config reloads, which
    needs the bus. This is the one backend-needs-bus predicate, evaluated against the
    resolved config at boot, at every reload, and at mutation time — so the same
    invariant rejects a manifest/env change that ADDS a backend with no bus and one
    that REMOVES the bus while a backend still needs it.
    """


class ApplyWindowBelowDrainError(ConfigInvariantError):
    """A bus-enabled config's fleet apply window is below the retire's drain floor.

    A reload's retire may legally spend three drains of the declared drain budget; a window
    shorter than that would report a worker still draining real work as ``timed_out``.
    """


@contextmanager
def translate_config_invariants(into: Callable[[str], Exception]) -> Iterator[None]:
    """Re-raise a ConfigInvariantError as ``into(str(exc))`` — the mutate-time doors' 400 or compose error.

    The invariants are ``RuntimeError`` so a boot-time refusal crashes loudly; a mutate-time
    door maps one to its own loud, actionable error naming the settings to change rather than
    letting it escape as a 500.
    """
    try:
        yield
    except ConfigInvariantError as exc:
        raise into(str(exc)) from exc


def _bus_configured() -> bool:
    return bus_settings().enabled


def check_backend_needs_bus(*, backend_module: str | None, bus_configured: bool) -> None:
    """Raise when a resolved config registers a task backend with no bus configured.

    The reusable core of the backend-needs-bus invariant: ``backend_module`` is the
    resolved manifest's backend module (empty / ``None`` when none is registered) and
    ``bus_configured`` is whether the effective env configures the bus. The mutation
    pipeline evaluates both against the POST-change resolved config, so this one
    predicate covers both directions — a change that adds a backend without a bus and
    a change that drops the bus while a backend remains.
    """
    if backend_module and not bus_configured:
        raise BackendNeedsBusError(
            f"Refusing a config that registers a task backend ({backend_module!r}) with no worker bus: "
            f"the backend-runtime and server processes must converge on config reloads. Set {_BUS_VAR}."
        )


def require_bus_for_workers(workers: int) -> None:
    """Refuse a multi-worker server with no bus.

    Sibling workers would serve stale state after a reload with no channel to converge on.
    """
    if workers > 1 and not _bus_configured():
        raise RuntimeError(
            f"Refusing to start {workers} workers without the worker bus: sibling workers would serve "
            f"stale config after a reload, with no channel to converge on. Set {_BUS_VAR} to enable the "
            "bus, or run a single worker."
        )


def require_bus_for_shared_config() -> None:
    """Refuse a shared-config boot with no bus.

    Any non-``file`` config mode serves shared config from an external provider, so instances must
    converge on a reload and one instance cannot see how many siblings share the config.
    """
    if config_mode() != ConfigMode.file and not _bus_configured():
        raise RuntimeError(
            f"Refusing to start in {config_mode()!r} config mode without the worker bus: an external "
            f"config provider serves shared config that every instance must converge on after a reload, "
            f"and the instance count is undetectable from inside one instance. Set {_BUS_VAR}."
        )


def require_bus_for_backend(manifest: Manifest) -> None:
    """Refuse a boot / reload that registers a task backend with no bus.

    The backend-runtime and server processes must converge on config reloads.
    The boot-time and reload-time face of :func:`check_backend_needs_bus`, reading
    the process-wide bus configuration — so the mutate-time pipeline check and this
    one agree on the same predicate.
    """
    check_backend_needs_bus(backend_module=manifest.backend_module, bus_configured=_bus_configured())


def check_apply_window_covers_drain(*, bus_enabled: bool, apply_timeout: float, drain_budget: float) -> None:
    """Raise when a bus-enabled config's fleet apply window is below the retire's drain floor.

    The reusable core of the apply-window invariant: ``apply_timeout`` is the resolved
    ``TAI_BUS_APPLY_TIMEOUT`` and ``drain_budget`` the declared drain budget, both read from
    the config being checked. A window equal to the floor passes. A busless config runs on
    :meth:`WorkerBus.local`, which uses no apply window, so it is never refused here.
    """
    if not bus_enabled:
        return
    floor = retire_drain_floor_seconds(drain_budget)
    if apply_timeout < floor:
        raise ApplyWindowBelowDrainError(
            f"Refusing a config whose fleet apply window is below the retire's drain floor: "
            f"{_APPLY_WINDOW_VAR}={apply_timeout:g} s, but a reload's retire may legally spend {floor:g} s "
            f"(three drains of the declared drain budget {_DRAIN_BUDGET_VAR}={drain_budget:g} s). "
            f"Set {_APPLY_WINDOW_VAR} to at least {floor:g} s plus the reload's own build and post-apply time, "
            "or lower the drain budget."
        )


def declared_drain_budgets() -> DrainBudgetRegistry:
    """The process app's drain-budget registry; its budget is read from the env current at each call."""
    from tai42_skeleton.app import instance

    return instance.build_app().drain_budgets


def require_apply_window_covers_drain() -> None:
    """Refuse a boot / reload whose fleet apply window is below the retire's drain floor.

    The boot-time and reload-time face of :func:`check_apply_window_covers_drain`, reading
    the process-wide bus configuration and the app's declared drain budget — so the
    mutate-time pipeline check and this one agree on the same predicate.
    """
    bus = bus_settings()
    if not bus.enabled:
        return
    check_apply_window_covers_drain(
        bus_enabled=True, apply_timeout=bus.apply_timeout, drain_budget=declared_drain_budgets().budget()
    )
