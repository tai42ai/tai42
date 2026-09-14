"""Background tool-run operations — submit, get-by-id, and per-tool list.

The synchronous ``POST /api/run-tool`` door holds one request open for the whole
tool call; a dropped connection loses the result even though the tool finished
server-side. These operations detach the run from the request:

* ``submit_run`` — takes ``tool_name`` + ``arguments`` (the same field shape the
  sync door's parser enforces), returns ``{"run_id": ...}`` at once (the route
  answers ``202``) and executes the tool as an in-process background task through
  the SAME ``tai42_app.tools.run_tool`` seam the sync door uses — with
  ``offload_sync`` set, so a blocking sync tool runs on a worker thread and cannot
  starve the supervisor's liveness refresh. A "run any tool by name" door, so it
  is a tier-1 meta-executor (never projected to the MCP surface, like
  ``run_tool``).
* ``get_run`` — the run record ``{run_id, tool_name, status, started_at,
  finished_at?, result?, error?}``; an unknown/expired id is a loud 404.
  ``status ∈ running | succeeded | failed | lost``.
* ``list_tool_runs`` — the recent runs for one tool (id, tool name, status,
  timestamps only — never ``result``/``error``), newest first, from a per-tool
  ZSET trimmed to ``ToolRunsSettings.recent_runs_limit``.

Per-identity isolation: a run records the OWNING identity of its submitter (always
the caller's OWN id — each key is its own island, never sharing its owner's or a
sibling owned key's slice) and is indexed under a per-identity
``recent:{user_id}:{tool_name}`` window in addition to the shared per-tool window. A
restricted caller reads and prunes only its own per-identity window (complete within
its own bound, never truncated by other identities' volume) and may GET only a run it
owns — another identity's run id is a loud ``403`` (never a ``404``: the run exists,
it is simply not the caller's). An unrestricted caller keeps the full view over the
shared window.

A supervisor wraps each run: it refreshes a per-run liveness key while the tool
runs, writes the terminal record when the tool returns or raises (``succeeded``
+ result, or ``failed`` + the caught error string — the error becomes visible
record data, never swallowed), and in ``finally`` cancels the liveness refresher.
``lost`` is computed-and-persisted one way: the FIRST read of a record still
``running`` whose liveness key has expired writes ``status: lost`` (a dead
process never wrote its terminal record, so it cannot later flip to succeeded).
"""

from __future__ import annotations

import sys

# Test-double seam symbols bound at the package level BEFORE the door submodules
# import, so a ``setattr(operations.tool_runs, "<sym>", …)`` takes effect: every
# caller submodule reads these THROUGH this package object at call time.
from tai42_kit.clients import client_ctx

from tai42_skeleton.access_control.user import request_identity
from tai42_skeleton.operations._submitted_tool_authz import authorize_submitted_tool
from tai42_skeleton.routers.tool_runs_settings import tool_runs_settings

from .models import _now

# Per-worker count of in-flight background runs, enforced against
# ``max_concurrent_runs``. HOMED here as the single mutable binding: the submit door
# reserves a slot and the supervisors' done-callbacks release one, all touching this
# package attribute through the package object, so a test's ``setattr(ops,
# "_ACTIVE_RUNS", 0)`` resets the one counter both paths read. Exact on the single
# event loop (no interleaving between the capacity check and the increment).
_ACTIVE_RUNS: int = 0

# Imported after the seam bindings above so every seam symbol and ``_ACTIVE_RUNS``
# is a package attribute before a door/supervisor submodule reads it through the
# package object.
from . import doors, reconcile, supervisor  # noqa: E402
from .doors import get_run, list_tool_runs, submit_run  # noqa: E402
from .reconcile import _reconcile_lost_with_liveness, _spawn_crash_resume  # noqa: E402
from .store import ToolRunStore  # noqa: E402
from .supervisor import (  # noqa: E402
    _SUPERVISORS,
    _drain_supervisors,
    _refresh_liveness_loop,
    drain_supervisors,
    run_recorded,
)

# A reload can rebuild THIS package around a still-cached door submodule whose module-top
# ``_pkg`` alias then points at the retired generation. Re-point each door submodule's alias
# at THIS package object so a door always reads the seam symbols (and ``_ACTIVE_RUNS``) from
# the generation that re-exports it — the generation a test patches at the package alias.
for _door_submodule in (doors, reconcile, supervisor):
    _door_submodule.__dict__["_pkg"] = sys.modules[__name__]

__all__ = [
    "_SUPERVISORS",
    "ToolRunStore",
    "_drain_supervisors",
    "_now",
    "_reconcile_lost_with_liveness",
    "_refresh_liveness_loop",
    "_spawn_crash_resume",
    "authorize_submitted_tool",
    "client_ctx",
    "drain_supervisors",
    "get_run",
    "list_tool_runs",
    "request_identity",
    "run_recorded",
    "submit_run",
    "tool_runs_settings",
]
