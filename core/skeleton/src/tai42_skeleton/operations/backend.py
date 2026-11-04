"""Backend identity + the fleet census and the fleet soft-restart.

``backend_info`` reports whether a task :class:`~tai42_contract.backend.Backend`
provider is registered (identity only). The other two operations are FLEET
operations over the app's worker bus, and need no backend at all:

* ``list_workers`` — the live worker fleet from the bus presence census (every
  subscribed process, HTTP server and backend runtime alike). No try/except: a
  presence-store read that cannot read must surface as a ``500``, never an empty
  ``[]``.
* ``fleet_reload_config`` — soft-restart the fleet. The serving worker applies its
  own reload FIRST, then broadcasts the reload; the response embeds the per-worker
  fleet report. A failed local apply does NOT abort
  the broadcast — the door's whole purpose is converging siblings onto persisted
  state — so it publishes anyway and re-raises with the report attached.

``fleet_reload_config`` is named apart from the local-reload ``reload_config`` op
(a different door, ``/api/config/reload``): the fleet soft-restart and the
in-process config reload share no surface.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from tai42_contract.app import tai42_app

from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import LastOp, WorkerKind, WorkerState, presence_fresh
from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.operations import BadRequestError, operation
from tai42_skeleton.operations._broadcast import fleet_reload_response
from tai42_skeleton.operations.response_models.backend import BackendInfo
from tai42_skeleton.operations.response_models.config import FleetReloadResponse


class ReloadConfig(BaseModel):
    """A fleet reload-config request.

    An optional ``targets`` list restricts the soft-restart to named workers (all workers when
    omitted).
    """

    targets: list[str] | None = None


class WorkerListingRow(BaseModel):
    """One worker on the fleet listing: the census row plus a server-computed ``stale`` flag.

    ``stale`` is derived from the SAME bus-owned freshness predicate the ready+fresh expected set
    uses (:func:`~tai42_skeleton.app.bus.presence_fresh` on the row's captured PTTL against the
    serving bus's TTL) — a client never hardcodes a threshold. ``last_op`` is ``null`` until the
    worker has applied one.
    """

    name: str
    kind: WorkerKind
    pid: int
    generation: int
    joined_at: str
    beat_at: str
    state: WorkerState
    stale: bool
    last_op: LastOp | None = None


class WorkerListing(BaseModel):
    """The fleet listing payload for ``GET /api/fleet/workers``.

    One row per live presence key, each with its server-computed ``stale`` flag.
    """

    workers: list[WorkerListingRow]


@operation(summary="Get the backend identity", tags=["backend"], response_model=BackendInfo)
async def backend_info() -> dict:
    """The backend identity — whether one is registered and, if so, its class and module."""
    backend = tai42_app.backends.backend
    if backend is None:
        return {"present": False, "backend": None, "module": None}
    return {"present": True, "backend": type(backend).__name__, "module": type(backend).__module__}


@operation(summary="List the worker fleet", tags=["backend"], response_model=WorkerListing)
async def list_workers() -> WorkerListing:
    """The fleet listing — one row per live worker presence key, each with a server-computed ``stale`` flag."""
    # The census IS the fleet listing now — every process on the bus, via its presence
    # key. No try/except: a presence-store read that cannot read must fail loudly (500),
    # never return an empty fleet. ``stale`` is server-computed from the ONE bus-owned
    # freshness predicate against the row's captured PTTL — never a client threshold.
    bus = instance.app.bus
    ttl = bus.heartbeat_ttl
    workers = [
        WorkerListingRow(
            name=row.name,
            kind=row.kind,
            pid=row.pid,
            generation=row.generation,
            joined_at=row.joined_at,
            beat_at=row.beat_at,
            state=row.state,
            last_op=row.last_op,
            stale=not presence_fresh(row.pttl_ms, ttl),
        )
        for row in await bus.census()
    ]
    return WorkerListing(workers=workers)


@operation(
    name="fleet_reload_config",
    summary="Soft-restart the worker fleet, restarting the drifted workers whose reload built",
    tags=["backend"],
    destructive=True,
    errors=[BadRequestError],
    request_model=ReloadConfig,
    response_model=FleetReloadResponse,
)
async def fleet_reload_config(targets: list[str] | None) -> Any:
    """Soft-restart the worker fleet, optionally restricted to the named ``targets``, then restart the drifted.

    Called over MCP on a worker the reload applies to (no ``targets``, or ``targets`` naming that
    worker), the reload answers on the session that called it and then retires that session with the
    generation it ran on: a later call on the same session fails (streamable HTTP answers it 404, and an
    SSE session's stream closes), so the client starts a new session to go on. When ``targets`` leave
    that worker out, nothing reloads there and the session keeps working.

    An applied worker's payload is ``{status: "ok", env_keys, awaiting_respawn}``; a worker whose
    reload was refused at its epoch build (the build discarded before its swap) replies ``failed``
    with ``{status: "discarded", awaiting_respawn}``, serving its previous epoch with that drift
    standing; any other failed reload, a failed discard among them, carries no payload.
    ``awaiting_respawn`` names the recycle-class keys whose value now differs from the one that
    worker bound (at boot, or when the plugin that declares them was loaded) — the soft restart
    carries every hot key in place, and those keys take effect only on a restart. Once the reports
    are in, every worker the reload addressed that names such a key is restarted one at a time
    through the deployment's supervisor, except a worker whose reload was refused at its epoch
    build: the stored config did not build on it and a restart may fail the same way, so it keeps
    serving its current config and is named in ``awaiting_respawn``. Any worker other than this one
    whose state is unknown (its reload failed otherwise or did not finish, it has no row, its reply
    carries no ``awaiting_respawn`` list, or the bus was unreachable) is asked once it is ready and
    restarted unless its reply confirms it clean or states a refused build (its restart then
    withheld); the workers that confirmed none are left running. This worker is never asked: when
    its own state is unknown, it needs its own restart. ``recycle`` reports that roll (``null`` when
    the roll had no worker to visit: every addressed worker confirmed no recycle-class key, or the
    only ones naming one were refused at their epoch build) and ``awaiting_respawn`` the workers
    still awaiting a respawn after it. When this worker needs its own restart and the roll
    converged, it exits gracefully once the reply is answered and its supervisor starts it again. A
    soft restart run under a bound the roll's waits can outlast (a dispatched job's budget, an armed
    turn budget) runs no roll itself: it hands the roll to the bus member it acts for, which carries
    it out after the reply and departs itself last, once the roll converged, when it needs its own
    restart; ``roll_deferred`` is then ``true``, ``roll_deferred_to`` names that member, ``recycle`` is ``null`` and
    ``awaiting_respawn`` empty, the roll not judged in this reply. Otherwise ``roll_deferred`` is
    ``false`` and ``roll_deferred_to`` ``null``.
    """
    # ``targets`` is validated at the HTTP edge (the route's ``_reload_targets``
    # extractor raises ``BadRequestError`` for a malformed body / non-string-list
    # targets), so the route answers a loud 400 there — the operation receives an
    # already-typed ``targets`` and declares ``BadRequestError`` so the surface
    # documents that 400. The bus then validates the target NAMES against the census.
    #
    # Convergence semantics: the serving worker applies its reload locally first,
    # but a failed local apply must not abort the broadcast — the recovery door
    # exists to heal the exact stale siblings a failed local reload would strand —
    # so it publishes anyway and re-raises with the fleet report attached.
    outcome = await ConfigService.from_app().reload_fleet(targets, change="the fleet soft restart")
    return fleet_reload_response(outcome)
