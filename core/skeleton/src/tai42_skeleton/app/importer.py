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
import os
import pkgutil
import sys


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


def import_or_reload_package(root_pkg_name: str | None) -> list[str]:
    if not root_pkg_name:
        return []

    importlib.invalidate_caches()

    # A manifest-named package that cannot be found is corrupt configuration:
    # the discovery failure propagates and aborts startup loudly rather than
    # booting a server silently missing its modules.
    managed = _discover_all_modules(root_pkg_name)

    # Remove all managed modules from sys.modules
    for name in managed:
        sys.modules.pop(name, None)

    # Re-import all in a stable order; imports will handle dependencies automatically
    order = _stable_cycle_fallback(managed)
    reloaded = []
    for name in order:
        try:
            importlib.import_module(name)
            reloaded.append(name)
        except ImportError as e:
            # A manifest-named module that fails to import is corrupt
            # configuration — abort startup loudly, naming the module.
            raise ImportError(f"Failed to import module {name}: {e}") from e
    return reloaded
