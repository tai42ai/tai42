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

More than one server worker also requires a shared sub-MCP registration store
(``SUB_MCP_REDIS_URL``, or the shared ``TAI_DEFAULT_REDIS_URL`` it falls back to): the
in-memory store is per-process, so a sub-MCP app registered through one worker would
not be served by its siblings. It likewise requires a shared hooks registry
(``HOOKS_REDIS_URL``, or ``TAI_DEFAULT_REDIS_URL``): the in-memory registry is
per-process, so a hook registered through one worker would be unseen by its siblings,
which would accept that hook's events and never run it.

A task backend registered in the manifest requires that shared hooks registry too: the
backend runtime (``tai backend``) is a separate process that raises platform events of
its own (the interactions expiry reaper, the media reaper, the monitoring health watch
all run in it), and it matches them against its own registry. An in-memory one there
holds none of the hooks registered through the server, so those events would run no
hook.

The shared-config check runs at the CLI BEFORE the config manager is constructed — a
busless shared-config boot then refuses naming ``TAI_BUS_REDIS_URL`` rather than failing
first on the external provider's connection — and again at the ``app_context`` seam. The
backend checks (bus and hooks registry) need the manifest, so they run in ``tai backend``
right after its manifest read and at the ``app_context`` seam that both ``tai serve`` and
``tai backend`` cross, re-run there on every reload, and are evaluated at mutation time
against the post-change config.

The workers-need-bus check and the two workers-need-a-shared-store checks read the bus and
store settings the bus and the store factories read, which the stored env (the config-dir
``.env`` or the external provider's store) overrides and a config write can change. So
they run at the ``app_context`` seam after the boot env bridge has applied the stored env,
again on every reload, and at mutation time against the post-change env, like the backend
checks. They count the server's workers from ``TAI_SERVE_WORKERS``, which ``tai serve``
stamps for the processes it launches; a process that carries no stamp (an embedded ASGI
host, ``tai backend``) counts as one worker, since it cannot see its siblings.

A bus-enabled deployment's fleet apply window (``TAI_BUS_APPLY_TIMEOUT``) must cover the
retire's drain floor — three drains of the declared drain budget
(:func:`~tai42_skeleton.app.epoch_retire.retire_drain_floor_seconds`) — so a worker draining
real work answers inside the window. The window and the budget are checked against the
resolved config at boot, at every reload, and at mutation time.

A process stays on the worker bus its deployment gives it. When withdrawing the stored copies
of keys another worker's deployment sets moves a value that locates the bus off the bus this
process is a member of (:attr:`~tai42_skeleton.app.bus_settings.BusSettings.fleet`: the bus
Redis URL or ``TAI_BUS_NAMESPACE``), the process reaches that bus only through a copy that
does not count, and it can neither stay on that bus nor run without it: the boot env bridge
refuses to start, and a join, a join announcement or a reconnect ends the process
(:func:`check_bus_stays_deployment_given`).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING

from tai42_kit.settings import fold_env_name

from tai42_skeleton.app.bus_settings import bus_settings, fleet_env_names
from tai42_skeleton.app.epoch_retire import retire_drain_floor_seconds
from tai42_skeleton.config.config_mode import ConfigMode, config_mode
from tai42_skeleton.hooks.settings import HooksSettings
from tai42_skeleton.sub_mcp.settings import sub_mcp_settings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from tai42_skeleton.app.bus_settings import BusSettings
    from tai42_skeleton.app.lifecycle.drain import DrainBudgetRegistry
    from tai42_skeleton.manifest import Manifest

_BUS_VAR = "TAI_BUS_REDIS_URL"
_APPLY_WINDOW_VAR = "TAI_BUS_APPLY_TIMEOUT"
_DRAIN_BUDGET_VAR = "TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS"
_SUB_MCP_STORE_VAR = "SUB_MCP_REDIS_URL"
_HOOKS_STORE_VAR = "HOOKS_REDIS_URL"
_DEFAULT_REDIS_VAR = "TAI_DEFAULT_REDIS_URL"

# The server's worker count, stamped by ``tai serve`` into the env its workers inherit.
SERVE_WORKERS_VAR = "TAI_SERVE_WORKERS"


class ConfigInvariantError(RuntimeError):
    """A resolved config breaks an invariant that relates the worker bus or a shared store to the rest of the config.

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


class BackendNeedsHooksStoreError(ConfigInvariantError):
    """A resolved config registers a task backend while the hooks registry is in-memory.

    The backend runtime is a separate process that matches the platform events it raises
    against its own hooks registry, so the registry must be shared. Evaluated against the
    resolved config at boot, at every reload, and at mutation time, so the same invariant
    rejects a change that ADDS a backend over an in-memory registry and one that REMOVES the
    shared registry while a backend still needs it.
    """


class WorkersNeedBusError(ConfigInvariantError):
    """A resolved config runs more than one server worker while no worker bus is configured.

    Sibling workers would serve stale config after a reload, with no channel to converge on.
    Evaluated against the resolved config at boot, at every reload, and at mutation time.
    """


class WorkersNeedSubMcpStoreError(ConfigInvariantError):
    """A resolved config runs more than one server worker over an in-memory sub-MCP registration store.

    The in-memory store is per-process: a registration made through one worker is invisible
    to its siblings, which neither list nor serve it, and no worker rehydrates another's
    routes. Evaluated against the resolved config at boot, at every reload, and at mutation
    time.
    """


class WorkersNeedHooksStoreError(ConfigInvariantError):
    """A resolved config runs more than one server worker over an in-memory hooks registry.

    The in-memory registry is per-process: a hook registered through one worker is invisible
    to its siblings, which neither list it nor run it for the events they accept on its
    topic. Evaluated against the resolved config at boot, at every reload, and at mutation
    time.
    """


class ApplyWindowBelowDrainError(ConfigInvariantError):
    """A bus-enabled config's fleet apply window is below the retire's drain floor.

    A reload's retire may legally spend three drains of the declared drain budget; a window
    shorter than that would report a worker still draining real work as ``timed_out``.
    """


class BusNotDeploymentGivenError(ConfigInvariantError):
    """A withdraw of stored keys another worker's deployment sets moved the bus this process is a member of.

    The process reaches its bus only through a stored copy that does not count, so it can
    neither stay on that bus nor run without it. Raised by the boot env bridge, which then
    refuses to start, and at every door that learns the fleet's deployment names, which then
    ends the process.
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


def _hooks_store_shared() -> bool:
    return not HooksSettings().in_memory


def serve_workers() -> int:
    """The number of server workers this process runs among, as ``tai serve`` stamped it.

    ``tai serve`` stamps ``TAI_SERVE_WORKERS`` before it launches its workers. A process
    without the stamp counts as one worker. A stamp that is not a positive integer raises.
    """
    raw = os.environ.get(SERVE_WORKERS_VAR)
    if raw is None:
        return 1
    try:
        workers = int(raw)
    except ValueError:
        workers = 0
    if workers < 1:
        raise RuntimeError(f"{SERVE_WORKERS_VAR}={raw!r} is not a positive worker count.")
    return workers


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


def check_backend_needs_hooks_store(*, backend_module: str | None, hooks_store_shared: bool) -> None:
    """Raise when a resolved config registers a task backend over an in-memory hooks registry.

    The reusable core of the backend-needs-hooks-store invariant: ``backend_module`` is the
    resolved manifest's backend module (empty / ``None`` when none is registered) and
    ``hooks_store_shared`` is whether the effective env configures the hooks Redis. The
    mutation pipeline evaluates both against the POST-change resolved config.
    """
    if backend_module and not hooks_store_shared:
        raise BackendNeedsHooksStoreError(
            f"Refusing a config that registers a task backend ({backend_module!r}) over an in-memory "
            "hooks registry: the backend runtime is a separate process that matches the events it raises "
            "against its own registry, which holds none of the hooks registered through the server, so "
            f"those events would run no hook. Set {_HOOKS_STORE_VAR} (or {_DEFAULT_REDIS_VAR})."
        )


def check_workers_need_bus(*, workers: int, bus_configured: bool) -> None:
    """Raise when a resolved config runs more than one server worker with no bus configured.

    The reusable core of the workers-need-bus invariant: ``workers`` is the server's worker
    count and ``bus_configured`` is whether the effective env configures the bus. The mutation
    pipeline evaluates it against the POST-change env.
    """
    if workers > 1 and not bus_configured:
        raise WorkersNeedBusError(
            f"Refusing a config that serves {workers} workers with no worker bus: sibling workers would "
            f"serve stale config after a reload, with no channel to converge on. Set {_BUS_VAR} to enable "
            "the bus, or run a single worker."
        )


def check_workers_need_sub_mcp_store(*, workers: int, sub_mcp_store_shared: bool) -> None:
    """Raise when a resolved config runs more than one server worker over an in-memory sub-MCP store.

    The reusable core of the workers-need-sub-MCP-store invariant: ``workers`` is the server's
    worker count and ``sub_mcp_store_shared`` is whether the effective env configures the
    sub-MCP Redis. The mutation pipeline evaluates it against the POST-change env.
    """
    if workers > 1 and not sub_mcp_store_shared:
        raise WorkersNeedSubMcpStoreError(
            f"Refusing a config that serves {workers} workers over an in-memory sub-MCP registration "
            "store: the store is per-process, so a sub-MCP app registered through one worker is not "
            f"served by its siblings. Set {_SUB_MCP_STORE_VAR} (or {_DEFAULT_REDIS_VAR}), or run a "
            "single worker."
        )


def check_workers_need_hooks_store(*, workers: int, hooks_store_shared: bool) -> None:
    """Raise when a resolved config runs more than one server worker over an in-memory hooks registry.

    The reusable core of the workers-need-hooks-store invariant: ``workers`` is the server's
    worker count and ``hooks_store_shared`` is whether the effective env configures the hooks
    Redis. The mutation pipeline evaluates it against the POST-change env.
    """
    if workers > 1 and not hooks_store_shared:
        raise WorkersNeedHooksStoreError(
            f"Refusing a config that serves {workers} workers over an in-memory hooks registry: the "
            "registry is per-process, so a hook registered through one worker is not seen by its "
            f"siblings, which accept its events and never run it. Set {_HOOKS_STORE_VAR} (or "
            f"{_DEFAULT_REDIS_VAR}), or run a single worker."
        )


def require_bus_for_workers() -> None:
    """Refuse a boot / reload of a multi-worker server with no bus.

    The boot-time and reload-time face of :func:`check_workers_need_bus`, reading the stamped
    worker count and the process-wide bus configuration under the applied env — so the
    mutate-time pipeline check and this one agree on the same predicate.
    """
    check_workers_need_bus(workers=serve_workers(), bus_configured=_bus_configured())


def require_sub_mcp_store_for_workers() -> None:
    """Refuse a boot / reload of a multi-worker server whose sub-MCP registration store is in-memory.

    The boot-time and reload-time face of :func:`check_workers_need_sub_mcp_store`, reading
    the stamped worker count and the process's sub-MCP settings under the applied env — so
    the mutate-time pipeline check and this one agree on the same predicate.
    """
    check_workers_need_sub_mcp_store(workers=serve_workers(), sub_mcp_store_shared=not sub_mcp_settings().in_memory)


def require_hooks_store_for_workers() -> None:
    """Refuse a boot / reload of a multi-worker server whose hooks registry is in-memory.

    The boot-time and reload-time face of :func:`check_workers_need_hooks_store`, reading the
    stamped worker count and the process's hooks settings under the applied env — so the
    mutate-time pipeline check and this one agree on the same predicate.
    """
    check_workers_need_hooks_store(workers=serve_workers(), hooks_store_shared=_hooks_store_shared())


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


def require_hooks_store_for_backend(manifest: Manifest) -> None:
    """Refuse a boot / reload that registers a task backend over an in-memory hooks registry.

    The boot-time and reload-time face of :func:`check_backend_needs_hooks_store`, reading the
    process's hooks settings — so the mutate-time pipeline check and this one agree on the
    same predicate.
    """
    check_backend_needs_hooks_store(backend_module=manifest.backend_module, hooks_store_shared=_hooks_store_shared())


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


def check_bus_stays_deployment_given(
    *, member: BusSettings, before: BusSettings, after: BusSettings, withdrawn: Sequence[str]
) -> None:
    """Raise when a withdraw moved the bus settings off the bus a process is a member of.

    ``member`` is the bus settings the process's bus was built under; ``before`` and ``after``
    are the effective bus settings just before and just after the withdraw of ``withdrawn``.
    Only the values that locate the fleet's census
    (:attr:`~tai42_skeleton.app.bus_settings.BusSettings.fleet`) count, and only a move the
    withdraw itself made: a withdraw that moves the heartbeat or a timeout, one that leaves the
    location where it was (a namespace saved since the bus was built still waits for the
    respawn), and one that returns the location to the bus the process runs on all leave the
    process on its bus. The refusal names the withdrawn keys that locate the bus, never a value.
    """
    if before.fleet == after.fleet or member.fleet == after.fleet:
        return
    locating = fleet_env_names()
    keys = sorted(key for key in withdrawn if fold_env_name(key) in locating)
    raise BusNotDeploymentGivenError(
        f"Refusing to run on a worker bus this process reaches only through a stored copy of {', '.join(keys)}: "
        "another worker's deployment sets them and the stored copy does not count. "
        "Set them in this process's environment too, or in no worker's."
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
