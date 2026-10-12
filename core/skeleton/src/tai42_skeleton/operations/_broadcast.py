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
  connector catalog write, a completed reconnect's ``reprobe_failed_mcps``) pass
  ``True``: their persist/convergence intent has already committed, so a failed
  local apply still broadcasts (siblings converge on the persisted state) and then
  re-raises with the fleet report attached — a stranded fleet never hides behind a
  local error.

A store-backed publisher whose apply can fail EITHER before it mutates the store
(nothing to converge — a not-found, a rejected path) OR once destructive mutation
may have begun (siblings must converge) names the pre-mutation exception types in
``pre_mutation_errors``: those keep the abort-before-publish discipline even under
``publish_on_local_failure=True``, so a spurious fan-out never rides a failure that
changed nothing, while a mid-mutation failure still publishes.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Protocol

from tai42_contract.app.responses import AwaitingWorker

from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import FleetResult, LocalApplyResult, OpOutcome, UnknownFleetTargetsError
from tai42_skeleton.app.recycle import RECYCLED, SELF_DEFERRED
from tai42_skeleton.operations.errors import BadRequestError, OperationFailedError

if TYPE_CHECKING:
    from tai42_skeleton.app.bus import WorkerIdentity
    from tai42_skeleton.app.recycle import RecycleReport
    from tai42_skeleton.config.service import ApplyResult, ProfileApplyOutcome

logger = logging.getLogger(__name__)


@contextmanager
def translate_orphan_env_write() -> Iterator[None]:
    """Map a combined env+manifest orphan-env-write failure to a typed 500 operation error.

    Maps :class:`~tai42_skeleton.config.service.OrphanEnvWriteError` to
    :class:`~tai42_skeleton.operations.errors.OperationFailedError` (500).

    The one shared translation every door that crosses the combined env+manifest seam
    wraps around its call: an oauth connector leaving the manifest (installer
    uninstall/update, a hand ``manifest replace`` / backup restore),
    ``set_mcp_secret_env``, or any marks-write that lands while the manifest persist
    exhausts its retries. The error is a partial failure — the env write STANDS as an
    inert, re-runnable orphan — so it is LOUD (a 500), never folded into a client-input
    400. Imported lazily to avoid a config↔operations import cycle (``config.service``
    imports this module).
    """
    from tai42_skeleton.config.service import OrphanEnvWriteError

    try:
        yield
    except OrphanEnvWriteError as exc:
        raise OperationFailedError(str(exc)) from exc


def fleet_fanout(fleet: FleetResult) -> dict[str, Any]:
    """Summarize a pipeline broadcast for a mutation response's ``fanout`` field.

    A broadcast that reaches no sibling collapses to a human note stating why: a
    busless deployment's local publish (``local_only``) has no worker bus, while a
    publish over the bus that found no other worker on the census says that instead.
    A reachable multi-worker broadcast returns the per-worker report, and an
    unreachable bus returns its error shape. The publisher's own worker is excluded
    when deciding local-only, so a lone worker never reports a fan-out.
    """
    if not fleet.reachable:
        return {"mode": "unreachable", **fleet.model_dump(mode="json")}
    if fleet.local_only:
        return {"mode": "local-only", "note": "no worker bus configured; only this worker reloaded"}
    remotes = [result for result in fleet.results if result.name != instance.app.bus.identity.name]
    if not remotes:
        return {"mode": "local-only", "note": "no other worker is on the worker bus; only this worker reloaded"}
    return {"mode": "fleet", **fleet.model_dump(mode="json")}


def apply_response(result: ApplyResult) -> dict[str, Any]:
    """The standard mutation-op response: this worker's reload result, the fan-out summary, the roll, the verdict.

    The one shape every ConfigService writer returns from an
    :class:`~tai42_skeleton.config.service.ApplyResult`: this worker's local reload result
    (``status``, ``env_keys``), ``fanout``, ``recycle`` — the fleet recycle roll the change ran
    (:func:`recycle_roll`, ``None`` when it carried no recycle-class key) — and
    ``awaiting_respawn``, the operation's verdict (``result.awaiting_respawn``, computed by the
    pipeline with :func:`awaiting_respawn_after`). The local reload's own ``awaiting_respawn``
    is a fact the verdict judges; it never reaches the top level. One reply on every edge: the
    writer's post-reply self-exit runs on the HTTP and the projected tool edge alike.
    """
    local = {key: value for key, value in result.local.items() if key != "awaiting_respawn"}
    return {
        **local,
        "fanout": result.fanout,
        "recycle": result_recycle_roll(result),
        "awaiting_respawn": [worker.model_dump() for worker in result.awaiting_respawn],
    }


def result_recycle_roll(result: ApplyResult) -> dict[str, Any] | None:
    """The fleet recycle roll a pipeline run carried (:func:`recycle_roll`), or ``None`` when it rolled none."""
    if result.recycle is None or result.self_identity is None:
        return None
    return recycle_roll(result.recycle, result.self_identity, self_exit_armed=result.self_exit_armed)


def awaiting_respawn_after(
    fleet: FleetResult,
    recycle: RecycleReport | None,
    writer: WorkerIdentity | None,
    *,
    self_exit_armed: bool,
) -> list[AwaitingWorker]:
    """The workers still awaiting a respawn after an operation, sorted by name.

    Each reload row's payload states a fact — the recycle-class keys whose value differs from the
    one that worker bound them under, at boot or when the plugin that declares them was loaded
    (``awaiting_respawn``). This judges it for the operation that
    started the reload, the one that knows whether a respawn follows: one ``{name, keys}`` per row
    that names keys, EXCLUDING a worker the roll confirmed ``recycled`` (its old life is gone) and
    the writer when its own post-response exit is armed. A ``timed-out`` / ``failed`` row, a
    target the roll never reached, and every row of a reload that rolls nothing stay listed.
    """
    recycled = set() if recycle is None else {row.name for row in recycle.rows if row.status == RECYCLED}
    exiting = writer.name if writer is not None and self_exit_armed else None
    verdict: list[AwaitingWorker] = []
    for row in fleet.results:
        keys = _awaiting_keys(row.payload)
        if keys and row.name not in recycled and row.name != exiting:
            verdict.append(AwaitingWorker(name=row.name, keys=keys))
    return sorted(verdict, key=lambda worker: worker.name)


def _awaiting_keys(payload: Any) -> list[str]:
    """The recycle-class keys a reload payload states as moved off their bound value (none for any other payload)."""
    if not isinstance(payload, Mapping):
        return []
    keys = payload.get("awaiting_respawn")
    return list(keys) if isinstance(keys, list) else []


def log_awaiting_respawn(verdict: list[AwaitingWorker]) -> None:
    """Loudly ERROR-log an operation's workers still awaiting a respawn, in one line, naming each and its keys.

    Nothing in the operation respawns them, so their recycle-class keys take effect only when the
    deployment's supervisor restarts them. A no-op for an empty verdict. Shared by every operation
    that starts a reload — the pipeline after its roll, the bare reload doors, the reconnect resync —
    beside :func:`log_non_convergence`, so the message stays identical.
    """
    if not verdict:
        return
    named = ", ".join(f"{worker.name} ({', '.join(worker.keys)})" for worker in verdict)
    logger.error(
        "worker(s) awaiting a respawn — recycle-class key(s) changed in the stored env since they were bound and "
        "nothing in this operation respawns them: %s — restart each through the deployment's supervisor "
        "(bare: restart the server)",
        named,
    )


def writer_exit_response(payload: Any, *, armed: bool) -> Any:
    """Wrap an env-change door's ``payload``, arming the writer's own post-reply graceful self-exit when ``armed``.

    ``armed`` is the operation's ``self_exit_armed`` (a serve-affecting change whose roll
    converged). Returns an ``OperationResponse`` whose background task is the graceful exit of the
    bus member this process acts for — itself, or, inside a forked job child, the member worker
    that forked it — the writer the roll excluded and reported ``self-deferred``. The HTTP edge runs
    it after the body flushes and the projected tool edge once the call returned; the worker's
    graceful stop lets the in-flight reply (or job) finish first, so the reply's ``self-deferred``
    line holds on every edge.
    """
    from starlette.background import BackgroundTask

    from tai42_skeleton.app.graceful_exit import request_graceful_exit
    from tai42_skeleton.operations.adapter import OperationResponse

    if not armed:
        return OperationResponse(payload)
    return OperationResponse(payload, BackgroundTask(request_graceful_exit, instance.app.bus.member_identity.pid))


def recycle_roll(recycle: RecycleReport, writer: WorkerIdentity, *, self_exit_armed: bool) -> dict[str, Any]:
    """The report of one fleet recycle roll: ``{recycle, fresh, recycle_stopped}``.

    ``recycle`` is one ``{name, kind, status, generation_before, detail}`` line per target
    the roll reached — ``recycled``, or, for the target the roll stopped at, ``timed-out``
    or ``failed`` with the ``detail`` naming why — plus, when the writer's own self-exit
    is armed (a serve-affecting change whose roll converged), the writer's own deferred line
    ``{name: <self>, kind: <self kind>, status: SELF_DEFERRED, generation_before: <self
    generation>, detail: None}`` (the one shared ``"self-deferred"`` literal). ``fresh`` is
    the per-kind list of NEW READY lives observed since the change began (``{name, kind,
    generation}``) — capacity evidence, never a claimed successor and never a post-recycle
    generation. ``recycle_stopped`` is ``None`` when the roll ran to its end, else where it
    stopped and why ``{kind, name, detail}`` — ``name`` is ``None`` when the bus could not
    be read with no target in hand, so a body whose rows all read ``recycled`` never reads
    as a complete roll it was not. NAMES-ONLY: key names + worker identities, never env VALUES.
    """
    entries: list[dict[str, Any]] = [
        {
            "name": row.name,
            "kind": row.kind,
            "status": row.status,
            "generation_before": row.generation_before,
            "detail": row.detail,
        }
        for row in recycle.rows
    ]
    if self_exit_armed:
        # The writer's own recycle is a post-reply self-exit it cannot confirm — the writer
        # carrying its own kind and current generation, reported deferred with the one shared
        # literal.
        entries.append(
            {
                "name": writer.name,
                "kind": writer.kind.value,
                "status": SELF_DEFERRED,
                "generation_before": writer.generation,
                "detail": None,
            }
        )
    return {
        "recycle": entries,
        "fresh": [{"name": life.name, "kind": life.kind, "generation": life.generation} for life in recycle.fresh],
        "recycle_stopped": None if recycle.stopped is None else recycle.stopped.model_dump(),
    }


def writer_self_exit_armed(roll: Mapping[str, Any] | None) -> bool:
    """Whether a roll report (:func:`recycle_roll`) carries the writer's own deferred line — its self-exit is armed."""
    return roll is not None and any(entry["status"] == SELF_DEFERRED for entry in roll["recycle"])


def profile_apply_response(outcome: ProfileApplyOutcome) -> dict[str, Any]:
    """The DEDICATED profile-apply response — NOT :func:`apply_response`.

    ``{hot, recycle, fresh, refused, recycle_stopped, awaiting_respawn, fanout}``: ``hot`` are the
    hot-class diff key NAMES;
    ``recycle`` is one ``{name, kind, status, generation_before, detail}`` line per target
    the roll reached — ``recycled``, or, for the target the roll stopped at, ``timed-out``
    or ``failed`` with the ``detail`` naming why — plus, when the applier's own self-exit
    is armed (a serve-affecting diff whose roll converged), the applier's own deferred line
    ``{name: <self>, kind: <self kind>, status: SELF_DEFERRED, generation_before: <self
    generation>, detail: None}`` (the one shared ``"self-deferred"`` literal).
    ``fresh`` is the per-kind list of NEW READY lives observed since the pre-apply
    snapshot (``{name, kind, generation}``) — capacity evidence, never a claimed
    successor and never a post-recycle generation. ``refused`` is ``[]`` on success BY
    CONSTRUCTION — any refusal aborts the pipeline upfront, before a response is ever
    built. ``recycle_stopped`` is ``None`` when the roll ran to its end, else where it
    stopped and why ``{kind, name, detail}`` — ``name`` is ``None`` when the bus could not
    be read with no target in hand, so a body whose rows all read ``recycled`` never
    reads as a complete roll it was not. ``awaiting_respawn`` is the operation's verdict
    (``outcome.awaiting_respawn``).
    ``fanout`` is the reload broadcast's fleet-fanout summary. NAMES-ONLY: the
    report enumerates key names + worker identities, never env VALUES.
    """
    roll = (
        recycle_roll(outcome.recycle, outcome.self_identity, self_exit_armed=outcome.self_exit_armed)
        if outcome.recycle is not None
        else {"recycle": [], "fresh": [], "recycle_stopped": None}
    )
    return {
        "hot": list(outcome.hot),
        "recycle": roll["recycle"],
        "fresh": roll["fresh"],
        "refused": [],
        "recycle_stopped": roll["recycle_stopped"],
        "awaiting_respawn": [worker.model_dump() for worker in outcome.awaiting_respawn],
        "fanout": fleet_fanout(outcome.fleet),
    }


class FleetBroadcastError(RuntimeError):
    """Raised after a mutation has PERSISTED when the post-persist propagation does not fully complete.

    The local reload raised, the fleet broadcast raised, or both. The fleet report is
    attached (``reachable=False`` when the broadcast itself raised) so the caller can
    surface it and knows the change already landed on disk; the specific failure is the
    ``cause`` and the report's ``error``. ``unrolled_recycle_keys`` are the recycle-class keys the
    persisted change carried whose roll never ran (the failure came before it), so a caller that
    recovers can hand them to the reload that continues the change.
    """

    def __init__(
        self, op_name: str, report: FleetResult, cause: BaseException, *, unrolled_recycle_keys: tuple[str, ...] = ()
    ) -> None:
        super().__init__(f"{op_name}: change persisted but fleet propagation failed — {cause}")
        self.report = report
        self.unrolled_recycle_keys = unrolled_recycle_keys


def log_non_convergence(report: FleetResult) -> None:
    """Loudly ERROR-log a reachable-but-non-converged fleet report.

    An unconfirmed worker is a visible failure, never a silently stale sibling. A
    bus-unreachable report is already logged inside :meth:`WorkerBus.publish`, and a
    fully converged report logs nothing — so this is a no-op unless the bus was reached
    yet some worker did not confirm ``applied``. Shared by every publisher (``broadcast``
    and the store-backed helpers that publish directly) so the message stays identical.

    Each unconfirmed entry carries the worker name, its outcome, and the verdict detail
    — the detail names the superseding generation for a worker replaced mid-apply.
    """
    if not (report.reachable and not report.ok):
        return
    unconfirmed = [(r.name, r.outcome.value, r.detail) for r in report.results if r.outcome != OpOutcome.applied]
    logger.error("worker bus: op %r did not fully converge — unconfirmed workers: %s", report.op, unconfirmed)


class CensusPublisher(Protocol):
    """The op-start census seam :func:`snapshot_membership` reads.

    The one method of :class:`~tai42_skeleton.app.bus.WorkerBus` it needs, so a
    publisher typed against a narrower bus protocol satisfies it structurally.
    """

    async def expected_at_start(self) -> dict[str, int]: ...


async def snapshot_membership(bus: CensusPublisher, op_name: str) -> dict[str, int] | None:
    """Pin the expected-confirmation membership to op START, before the local apply can age it out.

    The shared discipline for every UNTARGETED publisher.

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
    silently reports against its post-apply membership.
    """
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
    """Validate targets, pin the expected membership, apply locally per the self-targeting rule, then broadcast.

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
    # A non-member (a forked job child) is never a fleet target — it has no serving surface of
    # its own — so it applies nothing locally and its member converges as a sibling.
    self_targeted = bus.identity.member and (targets is None or bus.identity.name in targets)
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
