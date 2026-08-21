"""The one fleet-broadcast primitive for runtime-op publishers.

Every operation that changes runtime state on this worker AND must reach the rest
of the fleet applies its change locally, then broadcasts it over the app's worker
bus (``instance.app.bus``). :func:`broadcast` is that shared step: it enforces the
three invariants every publisher shares — targets are validated against the presence
census BEFORE any local side effect, the expected-confirmation membership of an
untargeted op is snapshotted BEFORE that side effect too (a local apply must not be a
blind spot in which a worker can fade off the census unreported), and the worker
applies locally only when it is itself a target — then awaits the confirmed broadcast
and returns the per-worker fleet report.

Failure discipline splits the callers into two disciplines, selected by
``publish_on_local_failure``:

* Pure registry/query ops (``reload_mcp``, ``deregister_mcp``, ``reload_tool``,
  ``remove_tool``, ``reload_failed_mcps``, ``list_failed_mcps``) leave it ``False``:
  a failed local apply raises before anything is broadcast, so the siblings stay
  genuinely untouched.
* Convergence and store-backed publishers (both ``reload_config`` doors, the
  connector catalog write) pass ``True``: their persist/convergence intent has
  already committed, so a failed local apply still broadcasts (siblings converge
  on the persisted state) and then re-raises with the fleet report attached — a
  stranded fleet never hides behind a local error.

A store-backed publisher whose apply can fail EITHER before it mutates the store
(nothing to converge — a not-found, a rejected path) OR once destructive mutation
may have begun (siblings must converge) names the pre-mutation exception types in
``pre_mutation_errors``: those keep the abort-before-publish discipline even under
``publish_on_local_failure=True``, so a spurious fan-out never rides a failure that
changed nothing, while a mid-mutation failure still publishes.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Protocol

from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import FleetResult, LocalApplyResult, OpOutcome, UnknownFleetTargetsError
from tai42_skeleton.app.recycle import SELF_DEFERRED
from tai42_skeleton.operations.errors import BadRequestError, OperationFailed

if TYPE_CHECKING:
    from tai42_skeleton.config.service import ApplyResult, ProfileApplyOutcome

logger = logging.getLogger(__name__)


@contextmanager
def translate_orphan_env_write() -> Iterator[None]:
    """Map a combined env+manifest :class:`~tai42_skeleton.config.service.OrphanEnvWriteError`
    to a typed :class:`~tai42_skeleton.operations.errors.OperationFailed` (500).

    The one shared translation every door that crosses the combined env+manifest seam wraps
    around its call: an oauth connector leaving the manifest (installer uninstall/update, a
    hand ``manifest replace`` / backup restore), ``set_mcp_secret_env``, or any marks-write
    that lands while the manifest persist exhausts its retries. The error is a partial
    failure — the env write STANDS as an inert, re-runnable orphan — so it is LOUD (a 500),
    never folded into a client-input 400. Imported lazily to avoid a config↔operations import
    cycle (``config.service`` imports this module)."""
    from tai42_skeleton.config.service import OrphanEnvWriteError

    try:
        yield
    except OrphanEnvWriteError as exc:
        raise OperationFailed(str(exc)) from exc


def fleet_fanout(fleet: FleetResult) -> dict[str, Any]:
    """Summarize a pipeline broadcast for a mutation response's ``fanout`` field.

    A single-worker deployment reaches no sibling, so the fan-out collapses to a
    human note; a reachable multi-worker broadcast returns the per-worker report, and
    an unreachable bus returns its error shape. The publisher's own worker is excluded
    when deciding local-only, so a lone worker never reports a fan-out.
    """
    if not fleet.reachable:
        return {"mode": "unreachable", **fleet.model_dump(mode="json")}
    remotes = [result for result in fleet.results if result.name != instance.app.bus.identity.name]
    if not remotes:
        return {"mode": "local-only", "note": "no worker bus configured; only this worker reloaded"}
    return {"mode": "fleet", **fleet.model_dump(mode="json")}


def apply_response(result: ApplyResult) -> dict[str, Any]:
    """The standard mutation-op response: the local reload result merged with the
    fan-out summary — the one shape every ConfigService writer returns from an
    :class:`~tai42_skeleton.config.service.ApplyResult`."""
    return {**result.local, "fanout": result.fanout}


def profile_apply_response(outcome: ProfileApplyOutcome) -> dict[str, Any]:
    """The DEDICATED profile-apply response — NOT :func:`apply_response`.

    ``{hot, recycle, fresh, refused, fanout}``: ``hot`` are the hot-class diff key NAMES;
    ``recycle`` is one ``{name, kind, status, generation_before}`` line per recycled /
    timed-out sibling plus, when the applier itself must self-exit, the applier's own
    deferred line ``{name: <self>, kind: "serve", status: SELF_DEFERRED,
    generation_before: <self generation>}`` (the one shared ``"self-deferred"`` literal).
    ``fresh`` is the per-kind list of NEW READY lives observed since the pre-apply
    snapshot (``{name, kind, generation}``) — capacity evidence, never a claimed
    successor and never a post-recycle generation. ``refused`` is ``[]`` on success BY
    CONSTRUCTION — any refusal aborts the pipeline upfront, before a response is ever
    built. ``fanout`` is the reload broadcast's fleet-fanout summary. NAMES-ONLY: the
    report enumerates key names + worker identities, never env VALUES."""
    entries: list[dict[str, Any]] = []
    fresh: list[dict[str, Any]] = []
    recycle = outcome.recycle
    if recycle is not None:
        for row in recycle.rows:
            entries.append(
                {"name": row.name, "kind": row.kind, "status": row.status, "generation_before": row.generation_before}
            )
        fresh = [{"name": life.name, "kind": life.kind, "generation": life.generation} for life in recycle.fresh]
    if outcome.serve_affecting:
        # The applier's own recycle is a post-response self-exit it cannot confirm — a
        # serve worker carrying its own current generation, reported deferred with the
        # one shared literal.
        entries.append(
            {
                "name": outcome.self_identity.name,
                "kind": "serve",
                "status": SELF_DEFERRED,
                "generation_before": outcome.self_identity.generation,
            }
        )
    return {
        "hot": list(outcome.hot),
        "recycle": entries,
        "fresh": fresh,
        "refused": [],
        "fanout": fleet_fanout(outcome.fleet),
    }


class FleetBroadcastError(RuntimeError):
    """Raised after a mutation has PERSISTED when the post-persist propagation does
    not fully complete — the local reload raised, the fleet broadcast raised, or both.
    The fleet report is attached (``reachable=False`` when the broadcast itself
    raised) so the caller can surface it and knows the change already landed on disk;
    the specific failure is the ``cause`` and the report's ``error``."""

    def __init__(self, op_name: str, report: FleetResult, cause: BaseException) -> None:
        super().__init__(f"{op_name}: change persisted but fleet propagation failed — {cause}")
        self.report = report


def log_non_convergence(report: FleetResult) -> None:
    """Loudly ERROR-log a reachable-but-non-converged fleet report: an unconfirmed
    worker is a visible failure, never a silently stale sibling. A bus-unreachable
    report is already logged inside :meth:`WorkerBus.publish`, and a fully converged
    report logs nothing — so this is a no-op unless the bus was reached yet some
    worker did not confirm ``applied``. Shared by every publisher (``broadcast`` and
    the store-backed helpers that publish directly) so the message stays identical.

    Each unconfirmed entry carries the worker name, its outcome, and the verdict detail
    — the detail names the superseding generation for a worker replaced mid-apply."""
    if not (report.reachable and not report.ok):
        return
    unconfirmed = [(r.name, r.outcome.value, r.detail) for r in report.results if r.outcome != OpOutcome.applied]
    logger.error("worker bus: op %r did not fully converge — unconfirmed workers: %s", report.op, unconfirmed)


class CensusPublisher(Protocol):
    """The op-start census seam :func:`snapshot_membership` reads — the one method of
    :class:`~tai42_skeleton.app.bus.WorkerBus` it needs, so a publisher typed against a
    narrower bus protocol satisfies it structurally."""

    async def expected_at_start(self) -> dict[str, int]: ...


async def snapshot_membership(bus: CensusPublisher, op_name: str) -> dict[str, int] | None:
    """Pin the expected-confirmation membership to op START, before the caller's local
    side effect can age it out — the shared discipline for every UNTARGETED publisher.

    Whole-fleet only, by construction: an untargeted op's expected set IS the census,
    and :meth:`~tai42_skeleton.app.bus.WorkerBus.publish` takes that census after the
    caller has already applied locally. A TARGETED op needs no snapshot — its expected
    set is the caller's explicit list, so a named target whose presence fades mid-apply
    is already carried into the report as ``departed``.

    Best-effort, and ``None`` when the census does not answer: ``publish`` takes its own
    census and degrades a dead bus to an honest unreachable report, so a blip here must
    NOT abort an op that would otherwise survive it. Loud, because the op then runs on
    exactly the post-apply membership this snapshot exists to correct.

    Called by every untargeted publisher, so the ordering rule and its failure
    discipline have one home: :func:`broadcast` takes the snapshot itself (it owns the
    local apply), while a publisher whose local apply is separate and
    compensation-rolled-back upstream — the config-service reload doors and the
    store-backed preset fan-outs — takes it at its OWN pre-apply point and passes it
    in. Nothing enforces that from here; a new untargeted publisher that skips it
    silently reports against its post-apply membership."""
    try:
        return await bus.expected_at_start()
    except Exception:
        logger.warning(
            "worker bus: op-start census for %r failed — falling back to the publish-time census "
            "(a worker whose presence fades during the local apply may go unreported)",
            op_name,
            exc_info=True,
        )
        return None


async def broadcast(
    op: dict[str, Any],
    targets: list[str] | None,
    apply: Callable[[], Awaitable[Any]],
    *,
    publish_on_local_failure: bool = False,
    pre_mutation_errors: tuple[type[BaseException], ...] = (),
) -> dict[str, Any]:
    """Validate targets, pin the expected membership, apply locally per the
    self-targeting rule, then broadcast.

    ``op`` is the wire op dict (``{"op": <name>, ...}``); ``targets`` is ``None`` for
    the whole fleet or the explicit worker list; ``apply`` runs this worker's own
    apply and returns its result (rides the self entry as the payload). Returns the
    :class:`~tai42_skeleton.app.bus.FleetResult` as a JSON-ready dict.

    ``pre_mutation_errors`` names the local-apply exception types that ALWAYS keep the
    abort-before-publish discipline — a failure raised before any destructive mutation,
    which leaves siblings genuinely untouched — even under ``publish_on_local_failure``.
    A failure of any other type under ``publish_on_local_failure`` still broadcasts. When
    ``publish_on_local_failure`` is ``False`` this is moot: every local failure aborts.

    Both pre-apply steps exist for the same reason: everything the report is judged
    against must be read before the local apply can change it. See
    :func:`snapshot_membership`.
    """
    bus = instance.app.bus
    self_targeted = targets is None or bus.identity.name in targets
    # Validate the whole target set against the census BEFORE any local side effect,
    # so a typo'd worker name is a loud error and never a silent narrowing. A name
    # absent from the census is a caller mistake, surfaced as a 400 (never a bare 500).
    if targets is not None:
        try:
            await bus.validate_targets(targets)
        except UnknownFleetTargetsError as exc:
            raise BadRequestError(str(exc)) from exc

    # Read the membership the report will be judged against BEFORE the local apply below
    # can age it out.
    expected_at_start = await snapshot_membership(bus, str(op.get("op"))) if targets is None else None

    local: LocalApplyResult | None = None
    local_failure: Exception | None = None
    if self_targeted:
        try:
            result = await apply()
        except Exception as exc:
            if not publish_on_local_failure or isinstance(exc, pre_mutation_errors):
                # Abort-before-publish: nothing is broadcast, siblings untouched — the
                # apply either opts out of post-failure publish, or failed before any
                # destructive mutation could begin (``pre_mutation_errors``).
                raise
            local_failure = exc
            local = LocalApplyResult(outcome=OpOutcome.failed, error=f"{type(exc).__name__}: {exc}")
        else:
            local = LocalApplyResult(outcome=OpOutcome.applied, payload=result)

    report = await bus.publish(op, targets, local, expected_at_start=expected_at_start)
    # The report is embedded in the response, but an unconfirmed worker is also a
    # loud, visible failure — never a silently stale sibling.
    log_non_convergence(report)
    if local_failure is not None:
        raise FleetBroadcastError(report.op, report, local_failure) from local_failure
    return report.model_dump(mode="json")
