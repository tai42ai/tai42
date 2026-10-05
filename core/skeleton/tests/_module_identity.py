"""Keep the states package's submodules canonical so the feature gate never reads an orphan.

The states feature gate resolves its check THROUGH the parent package at call time —
``from tai42_skeleton.states import service as _pkg; _pkg.states_store_configured()`` — so a config
reload's rebound package and a test's package-alias double both drive it. A handful of suites
exercise the app's reload path by popping a module out of ``sys.modules`` and re-importing it (which
rebinds the parent package's attribute to a FRESH module object), sometimes restoring the
``sys.modules`` entry to the original by hand afterwards. That can leave ``tai42_skeleton.states``'s
``service`` attribute — or the ``sys.modules`` entry — pointing at the orphaned re-import, whose
``states_store_configured`` was bound under another test's patch. The orphan then answers the gate
for an unrelated later test.

Restoring the state package's own submodules (``service``, ``db``, ``store``) to the canonical module
objects captured at import — in both ``sys.modules`` and the parent-package attribute — removes any
such orphan before each test. It is scoped to these three names, which are always submodules (never
re-exported callables), so it cannot disturb a re-exported name that merely shares a submodule's
spelling in some other package.
"""

from __future__ import annotations

import sys
from types import ModuleType

import tai42_skeleton.states as _states
import tai42_skeleton.states.db as _states_db
import tai42_skeleton.states.service as _states_service
import tai42_skeleton.states.store as _states_store

# The canonical state-package submodules, captured at import (session start, before any test runs).
_CANONICAL: dict[str, ModuleType] = {
    "db": _states_db,
    "service": _states_service,
    "store": _states_store,
}


def restore_states_module_identity() -> None:
    """Point ``tai42_skeleton.states.{db,service,store}`` (sys.modules entry and parent attribute)
    back at the canonical module objects. A no-op when they already match (the normal case)."""
    for name, module in _CANONICAL.items():
        full = f"tai42_skeleton.states.{name}"
        if sys.modules.get(full) is not module:
            sys.modules[full] = module
        if getattr(_states, name, None) is not module:
            setattr(_states, name, module)
