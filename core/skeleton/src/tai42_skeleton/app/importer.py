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
from collections.abc import Mapping

# The core distribution's top-level import package. Its modules are NEVER widened to
# a whole-package reload: a core router declares its routes in the manifest leaf
# itself (so a leaf-only re-import re-fires them), and widening would pop+reimport the
# entire skeleton.
_CORE_TOP_LEVEL = "tai42_skeleton"


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


def _reload_roots(module: str, dist_map: Mapping[str, list[str]] | None) -> set[str]:
    """The package name(s) to pop+reimport for a manifest ``module``.

    A plugin's manifest leaf may register its HTTP routes in a SIBLING module via an
    import side-effect (``tai42_channel_twilio.register`` does ``import
    tai42_channel_twilio.inbound``, whose ``@custom_route`` decorators live in the
    sibling). Popping+reimporting only the leaf leaves the sibling cached in
    ``sys.modules`` on a reload, so its decorators never re-fire and every one of the
    plugin's routes drops from the rebuilt epoch. Widening the reload to every
    top-level package the leaf's DISTRIBUTION provides re-fires all its siblings.

    Widening is derived from ``dist_map`` (top-level package -> owning distribution(s),
    a ``packages_distributions`` snapshot). It is DELIBERATELY narrow:

    * a ``tai42_skeleton`` (core) module is never widened — its routes live in the
      manifest leaf itself and widening would reload the whole skeleton;
    * an operator-authored module that maps to no installed distribution (no
      ``dist_map`` entry) is never widened — leaf-only preserves its reload semantics;
    * with no ``dist_map`` at all (a non-plugin caller) the leaf alone is reloaded.

    Both fall-back paths return just the leaf, so existing behavior is unchanged
    except for a real installed plugin distribution.
    """
    if dist_map is None:
        return {module}
    top_level = module.partition(".")[0]
    if top_level == _CORE_TOP_LEVEL:
        return {module}
    owning = dist_map.get(top_level)
    if not owning:
        return {module}
    owners = set(owning)
    # Reload every top-level package the owning distribution(s) provide (a dist may
    # ship more than one), so a sibling in ANY of them re-fires — minus core, which is
    # never widened even when a plugin dist also contributes to its namespace.
    packages = {pkg for pkg, dists in dist_map.items() if pkg != _CORE_TOP_LEVEL and not owners.isdisjoint(dists)}
    packages.add(top_level)
    return packages


def import_or_reload_package(root_pkg_name: str | None, dist_map: Mapping[str, list[str]] | None = None) -> list[str]:
    if not root_pkg_name:
        return []

    importlib.invalidate_caches()

    # Resolve the reload scope: a plain leaf (core / operator / no dist_map) reloads
    # only itself; a real plugin distribution's leaf widens to its whole top-level
    # package set so its route-registering siblings re-fire (see ``_reload_roots``).
    # A manifest-named package that cannot be found is corrupt configuration: the
    # discovery failure propagates and aborts startup loudly rather than booting a
    # server silently missing its modules.
    managed: set[str] = set()
    for root in _reload_roots(root_pkg_name, dist_map):
        managed |= _discover_all_modules(root)

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
