"""Pop-and-reimport of a manifest-named package.

FORK INVARIANT (canonical statement in ``tai42_kit.fork_gate``): while this runs, no
child may be FORKED and no in-process job may RUN. Each ``import_module`` below holds
that module's ``importlib`` per-module lock for the span of its body, and ``importlib``
registers no ``os.register_at_fork`` handler — a child forked here inherits the held lock
with an owner thread that does not exist post-fork and blocks forever on the same import;
an in-process job imports against the half-torn ``sys.modules`` this leaves behind.

Two RELOAD doors reach this, and each holds ``tai42_kit.fork_gate``'s exclusive side for
its whole body: ``reload_gate.run(..., reimports=True)`` (every ``reload_config``) and
``ConfigService.apply_replace_env``, which drives ``build_and_swap_epoch`` directly and
takes ``fork_gate.exclusive_async``. The other side is held by a forking backend
(``tai42_backend_rq``'s worker) around each child spawn.

BOOT is exempt and reaches this un-gated. That is safe by sequencing, not by luck: the rq
work loop awaits ``lifecycle.wait_until_ready()`` before its first dequeue, so the boot
import pass has finished before any child can be spawned.

A future forking consumer must take that same job span. Two gaps are known and accepted:

* the celery backend does not, and mostly does not need to — its prefork children are
  re-forked from ``on_fleet_op_applied``, which runs strictly AFTER the reload body
  returns, so its turnover is sequenced rather than concurrent. That covers only the
  turnover celery drives; billiard's supervisor may replace a dead child on its own
  schedule, which is not sequenced against a reload. Celery has not shown this failure
  because its children are long-lived and rarely re-forked, not because the window is
  closed.
* a host-registered ``@tai42_app.admin.tool_reloader`` runs through ``run_tool_reload``,
  which is NOT reload-gated. The built-in preset reloader imports nothing, but a consumer
  reloader that re-imports would reach this un-gated and would need the exclusive side of
  its own accord.
"""

import importlib
import importlib.util
import logging
import os
import pkgutil
import sys
from collections.abc import Callable, Iterable, Mapping

from tai42_skeleton.app.mount_map import MountBinding, bind_module, current_mount_binding

logger = logging.getLogger(__name__)


def _discover_all_modules(root_pkg_name: str) -> set[str]:
    spec = importlib.util.find_spec(root_pkg_name)
    if not spec:
        raise ImportError(f"Cannot find module {root_pkg_name}")

    names = {root_pkg_name}
    search_locations = spec.submodule_search_locations
    if not search_locations:
        return names

    # Enumerate submodule names WITHOUT importing them: ``pkgutil.iter_modules``
    # only lists a directory's modules, whereas ``walk_packages`` imports each
    # subpackage to obtain its ``__path__`` and recurse. Subpackage search paths
    # are instead built from the filesystem, so discovery imports nothing and the
    # caller's pop+reimport step is the sole import of each module (an ``__init__``
    # side effect runs exactly once per start()).
    stack: list[tuple[list[str], str]] = [(list(search_locations), root_pkg_name)]
    while stack:
        paths, prefix = stack.pop()
        for module_info in pkgutil.iter_modules(paths):
            full_name = f"{prefix}.{module_info.name}"
            names.add(full_name)
            if module_info.ispkg:
                stack.append(([os.path.join(p, module_info.name) for p in paths], full_name))
    return names


def _stable_cycle_fallback(nodes: set[str]) -> list[str]:
    return sorted(nodes, key=lambda n: (n.count("."), n))


def _import_module_under_binding(
    name: str,
    mount_map: Mapping[str, MountBinding],
    route_savepoint: Callable[[], int] | None,
    route_rollback: Callable[[MountBinding, int], None] | None,
) -> None:
    """Execute module ``name`` under ITS OWN mount binding when the map declares one
    and it is not already the active binding, else under whatever context the walk
    already carries. A route submodule reached through a foreign role's package walk
    thus resolves ``mount_base()`` and registers its declared rows against ITS item's
    binding; a sibling with no binding keeps the leaf's, and a module whose own binding
    is already active is not re-bound (its rows must land in the live context so its
    completeness check still sees them).

    A submodule the walk newly binds is savepoint-guarded when ``route_savepoint``/
    ``route_rollback`` are supplied: a mid-import ``custom_route`` fault or a bind-time
    completeness fault rolls the submodule's committed rows back before the fault
    propagates, so a failed foreign-walk import leaves no half-registered state — the
    same guarantee the own-role import gives itself."""
    binding = mount_map.get(name)
    if binding is not None and binding != current_mount_binding():
        savepoint = route_savepoint() if route_savepoint is not None else None
        try:
            with bind_module(binding):
                importlib.import_module(name)
        except BaseException:
            if route_rollback is not None and savepoint is not None:
                route_rollback(binding, savepoint)
            raise
    else:
        importlib.import_module(name)


def import_or_reload_package(
    root_pkg_name: str | None,
    extra_modules: Iterable[str] = (),
    *,
    mount_map: Mapping[str, MountBinding] | None = None,
    route_savepoint: Callable[[], int] | None = None,
    route_rollback: Callable[[MountBinding, int], None] | None = None,
) -> list[str]:
    if not root_pkg_name:
        return []
    module_bindings: Mapping[str, MountBinding] = mount_map or {}

    importlib.invalidate_caches()

    # A manifest-named package that cannot be found is corrupt configuration:
    # the discovery failure propagates and aborts startup loudly rather than
    # booting a server silently missing its modules.
    managed = _discover_all_modules(root_pkg_name)

    # ``extra_modules`` are the route-registering SIBLING module(s) of a bound plugin:
    # its manifest leaf imports them for the ``@custom_route`` side-effect, so re-importing
    # the leaf alone leaves them cached and their decorators never re-fire. Pop+reimport
    # them alongside the leaf — under the caller's active binding — so their routes
    # re-register into the staged epoch. A stale extra (a plugin update renamed its
    # module) that no longer resolves is dropped: the leaf's own import pulls the current
    # module, and the epoch-build audit fails loudly if a route still went missing.
    extras: set[str] = set()
    for name in extra_modules:
        if name in managed:
            continue
        # ``find_spec`` short-circuits to ``sys.modules[name].__spec__`` when the module
        # is still cached, so a sibling whose file the update removed resolves against its
        # stale in-memory spec and dodges the staleness check. Drop the module from the
        # cache first — the ``invalidate_caches`` above has already refreshed the finders —
        # so resolution runs against the filesystem. Popping a dead module is correct
        # hygiene, and a resolving extra is popped again below as a managed member.
        sys.modules.pop(name, None)
        if importlib.util.find_spec(name) is None:
            # A stale route-sibling of ``root_pkg_name``: notable but expected across a
            # plugin update, so log the drop rather than pass it silently.
            logger.info("dropping stale route-sibling extra %s of %s: no longer resolves", name, root_pkg_name)
            continue
        extras.add(name)
    managed |= extras

    # Remove all managed modules from sys.modules
    for name in managed:
        sys.modules.pop(name, None)

    # Re-import all in a stable order; imports will handle dependencies automatically
    order = _stable_cycle_fallback(managed)
    reloaded = []
    for name in order:
        try:
            _import_module_under_binding(name, module_bindings, route_savepoint, route_rollback)
            reloaded.append(name)
        except ImportError as e:
            # A manifest-named module that fails to import is corrupt
            # configuration — abort startup loudly, naming the module.
            raise ImportError(f"Failed to import module {name}: {e}") from e
    return reloaded
