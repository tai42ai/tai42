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
from collections.abc import Awaitable, Callable, Iterator, Mapping, Set
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

from tai42_contract.app.responses import AwaitingWorker

from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import FleetResult, LocalApplyResult, OpOutcome, UnknownFleetTargetsError
from tai42_skeleton.app.bus.ops import FleetOp
from tai42_skeleton.app.recycle import RECYCLED, SELF_DEFERRED
from tai42_skeleton.operations.errors import BadRequestError, OperationFailedError

if TYPE_CHECKING:
    from tai42_skeleton.app.bus import WorkerBus, WorkerIdentity
    from tai42_skeleton.app.recycle import RecycleReport
    from tai42_skeleton.config.recycle_policy import RecycleDecision
    from tai42_skeleton.config.recycle_roll import RollOutcome
    from tai42_skeleton.config.service import ApplyResult, FleetReloadOutcome, ProfileApplyOutcome

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
    (:func:`recycle_roll`, ``None`` when the roll had no worker to visit: every worker's reload
    confirmed no recycle-class key, or the only ones naming one were refused at their epoch build or are
    the writer whose own restart cannot follow as its exit) — and
    ``awaiting_respawn``, the operation's verdict (``result.awaiting_respawn``, computed by the
    pipeline with :func:`awaiting_respawn_after`). The local reload's own ``awaiting_respawn``
    is a fact the verdict judges; it never reaches the top level. ``span_release_error`` is the
    store's error when its exclusive write span failed to release after the persist, ``None``
    when it released. ``roll_deferred`` is set when the roll is not judged in this reply, and
    ``roll_deferred_to`` names the bus member carrying it out on its own when the change
    handed its roll off (``None`` otherwise). One reply on every edge: the writer's post-reply
    self-exit runs on the HTTP and the projected tool edge alike
    (:mod:`~tai42_skeleton.app.writer_exit`).
    """
    local = {key: value for key, value in result.local.items() if key != "awaiting_respawn"}
    return {
        **local,
        "fanout": result.fanout,
        "recycle": result_recycle_roll(result),
        "awaiting_respawn": [worker.model_dump() for worker in result.awaiting_respawn],
        "roll_deferred": result.roll_deferred,
        "roll_deferred_to": result.roll_deferred_to,
        "span_release_error": result.span_release_error,
    }


def fleet_reload_response(outcome: FleetReloadOutcome) -> dict[str, Any]:
    """A fleet soft restart's response: the per-worker reload report, the roll it ran, the verdict.

    The fleet report as JSON (each worker's payload stays the fact the decision read: an applied
    worker's ``{status: "ok", env_keys, awaiting_respawn}``, a ``failed`` worker whose reload was refused
    at its epoch build ``{status: "discarded", awaiting_respawn}``, none for any other failed
    reload), ``recycle`` — the roll (:func:`recycle_roll`, ``None`` when the roll had no worker to
    visit: every addressed worker confirmed no recycle-class key, or the only ones naming one were refused
    at their epoch build) —
    ``awaiting_respawn``, the operation's verdict, and
    ``roll_deferred`` / ``roll_deferred_to``, set and naming the bus member when the roll is
    deferred to it (:func:`roll_outcome_body`).
    """
    return {**outcome.fleet.model_dump(mode="json"), **roll_outcome_body(outcome.roll)}


def result_recycle_roll(result: ApplyResult) -> dict[str, Any] | None:
    """The fleet recycle roll a pipeline run carried (:func:`recycle_roll`), or ``None`` when it rolled none."""
    if result.decision is None:
        return None
    return ran_recycle_roll(result.recycle, result.decision, self_exit_armed=result.self_exit_armed)


def ran_recycle_roll(
    recycle: RecycleReport | None, decision: RecycleDecision, *, self_exit_armed: bool
) -> dict[str, Any] | None:
    """The report of the roll a decision ran (:func:`recycle_roll`), ``None`` when it ran none."""
    return None if recycle is None else recycle_roll(recycle, decision, self_exit_armed=self_exit_armed)


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
    started the reload, the one that knows whether a respawn follows. A worker's latest statement
    counts: its roll row's keys when the roll asked or recycled it, else its reload row's. One
    ``{name, keys}`` per worker whose statement names keys, EXCLUDING a worker the roll confirmed
    ``recycled`` (its old life is gone), a target whose exit is deferred to its own in-flight change
    (``self-deferred``: that change's exit restarts it), a worker confirmed clean once ready, and the
    writer when its own post-response exit is armed. A ``timed-out`` / ``failed`` row, a ``withheld``
    one and a discarded build's reload row stating keys (the worker's reload was refused at its
    epoch build, so the roll made no restart for them), a target the roll never reached, and every
    row when no roll ran stay listed.
    """
    stated = {row.name: _awaiting_keys(row.payload) for row in fleet.results}
    left: set[str] = set()
    if recycle is not None:
        stated.update({row.name: list(row.keys) for row in recycle.rows})
        left = {row.name for row in recycle.rows if row.status in (RECYCLED, SELF_DEFERRED)} | set(
            recycle.confirmed_clean
        )
    exiting = writer.name if writer is not None and self_exit_armed else None
    verdict = [
        AwaitingWorker(name=name, keys=keys)
        for name, keys in stated.items()
        if keys and name not in left and name != exiting
    ]
    return sorted(verdict, key=lambda worker: worker.name)


def _awaiting_keys(payload: Any) -> list[str]:
    """The recycle-class keys a reload payload states as moved off their bound value (none for any other payload)."""
    if not isinstance(payload, Mapping):
        return []
    keys = payload.get("awaiting_respawn")
    return list(keys) if isinstance(keys, list) else []


def log_awaiting_respawn(verdict: list[AwaitingWorker], *, withheld: Set[str]) -> None:
    """Loudly ERROR-log an operation's workers still awaiting a respawn, naming each and its keys.

    Nothing in the operation respawns them. A worker only awaiting its respawn takes its
    recycle-class keys when the deployment's supervisor restarts it, and the line says to restart
    it. A worker in ``withheld`` had its reload refused at its epoch build (the build discarded
    before its swap): the stored config did not build on it, it keeps serving its current config,
    and a restart may fail the same way, so its own line says to fix the stored config, or the
    cause the build error names, before any restart. One line per group, a no-op for an empty
    verdict. Every operation that starts a reload logs its verdict here, through
    :func:`~tai42_skeleton.config.recycle_roll.decide_and_roll`, so the messages stay identical.
    """
    awaiting = [worker for worker in verdict if worker.name not in withheld]
    if awaiting:
        logger.error(
            "worker(s) awaiting a respawn — recycle-class key(s) changed in the stored env since they were bound "
            "and nothing in this operation respawns them: %s — restart each through the deployment's supervisor "
            "(bare: restart the server)",
            _named(awaiting),
        )
    refused = [worker for worker in verdict if worker.name in withheld]
    if refused:
        logger.error(
            "worker(s) whose reload was refused at its epoch build — the stored config did not build on them, so each "
            "keeps serving its current config, its recycle-class key(s) changed in the stored env since they were "
            "bound: %s — fix the stored config, or the cause the build error names, before restarting any of them",
            _named(refused),
        )


def _named(workers: list[AwaitingWorker]) -> str:
    """Each worker of a verdict line with its keys: ``name (KEY, KEY)``, comma-joined."""
    return ", ".join(f"{worker.name} ({', '.join(worker.keys)})" for worker in workers)


def recycle_roll(recycle: RecycleReport, decision: RecycleDecision, *, self_exit_armed: bool) -> dict[str, Any]:
    """The report of one fleet recycle roll: ``{recycle, skipped, fresh, recycle_stopped}``.

    ``recycle`` is one ``{name, kind, status, generation_before, keys, detail}`` line per target
    the roll reached — ``recycled``; ``self-deferred`` for a target whose exit is deferred to its
    own in-flight change; ``withheld`` for a target asked once ready whose reload was refused at its
    epoch build yet reports keys, left running; or, for the target the roll stopped at, ``timed-out``
    or ``failed`` — plus, when the writer's own self-exit is armed (the writer needs its restart and the roll
    converged), the writer's own deferred line with its kind, current generation and the
    one shared ``"self-deferred"`` literal. ``keys`` are the recycle-class keys that worker stated
    as moved off their bound value, at the reload or when asked once ready; a worker restarted
    because its state stayed unknown has none, and its ``detail`` names what left it unknown (its
    reload outcome, no report row, a malformed reply, an unreachable bus, and its answer when asked
    once ready), joined with ``"; "`` to why the roll stopped at it when it did. ``skipped`` are
    the workers whose reload confirmed no recycle-class key off its bound value, at the reload or
    when asked once ready, left running; a worker whose restart is withheld is not among them.
    ``fresh`` is the per-kind list of NEW READY lives observed since the change began
    (``{name, kind, generation}``) — capacity evidence, never a claimed successor and never a
    post-recycle generation. ``recycle_stopped`` is ``None`` when the roll ran to its end,
    else where it stopped and why ``{kind, name, detail}`` — ``name`` is ``None`` when the bus
    could not be read with no target in hand, so a body whose rows all read ``recycled`` never
    reads as a complete roll it was not. NAMES-ONLY: key names + worker identities, never env VALUES.
    """
    entries: list[dict[str, Any]] = [
        {
            "name": row.name,
            "kind": row.kind,
            "status": row.status,
            "generation_before": row.generation_before,
            "keys": list(row.keys),
            "detail": row.detail,
        }
        for row in recycle.rows
    ]
    writer_reason = decision.writer_reason
    if self_exit_armed and writer_reason is not None:
        # The writer's own recycle is a post-reply self-exit it cannot confirm — the writer
        # carrying its own kind and current generation, reported deferred with the one shared
        # literal.
        writer = decision.writer
        entries.append(
            {
                "name": writer.name,
                "kind": writer.kind.value,
                "status": SELF_DEFERRED,
                "generation_before": writer.generation,
                "keys": list(writer_reason.keys),
                "detail": writer_reason.unknown,
            }
        )
    return {
        "recycle": entries,
        "skipped": sorted(decision.clean | set(recycle.confirmed_clean)),
        "fresh": [{"name": life.name, "kind": life.kind, "generation": life.generation} for life in recycle.fresh],
        "recycle_stopped": None if recycle.stopped is None else recycle.stopped.model_dump(),
    }


def roll_outcome_body(outcome: RollOutcome | None) -> dict[str, Any]:
    """A recycle decision's roll as reply fields: ``recycle`` (:func:`recycle_roll`, ``None`` if none ran), the verdict.

    The fields a reply that carries a :class:`~tai42_skeleton.config.recycle_roll.RollOutcome` on its
    own (a fleet soft restart, a marketplace upgrade batch, a failed marketplace operation's error
    body) merges in, named as on :func:`apply_response`: ``roll_deferred`` is set and
    ``roll_deferred_to`` names the bus member when the roll is deferred to it — handed off, or
    one that may have been sent with its answer not known — to carry out and judge on its own.
    ``outcome`` is ``None`` when the operation reloaded nothing, so it owed no decision: no roll,
    no worker awaiting a respawn.
    """
    if outcome is None:
        return {"recycle": None, "awaiting_respawn": [], "roll_deferred": False, "roll_deferred_to": None}
    return {
        "recycle": ran_recycle_roll(outcome.recycle, outcome.decision, self_exit_armed=outcome.self_exit_armed),
        "awaiting_respawn": [worker.model_dump() for worker in outcome.awaiting_respawn],
        "roll_deferred": outcome.deferred,
        "roll_deferred_to": outcome.deferred_to,
    }


def profile_apply_response(outcome: ProfileApplyOutcome) -> dict[str, Any]:
    """The DEDICATED profile-apply response — NOT :func:`apply_response`.

    ``{hot, recycle, skipped, fresh, refused, recycle_stopped, awaiting_respawn, roll_deferred, roll_deferred_to,
    span_release_error, fanout}``:
    ``hot`` are the hot-class diff key NAMES; ``recycle``, ``skipped``, ``fresh`` and
    ``recycle_stopped`` are the roll the apply ran (:func:`recycle_roll`); when the roll had no
    worker to visit (every worker's reload confirmed no recycle-class key, or the only ones naming
    one were refused at their epoch build or are the writer whose own restart cannot follow as its exit)
    ``recycle`` and ``fresh`` are empty, ``recycle_stopped`` ``None`` and ``skipped``
    every worker that confirmed clean. ``refused`` is ``[]`` on success BY
    CONSTRUCTION — any refusal aborts the pipeline upfront, before a response is ever
    built. ``awaiting_respawn`` is the operation's verdict
    (``outcome.awaiting_respawn``). ``roll_deferred`` is set when the roll is not judged in this
    reply, and ``roll_deferred_to`` names the bus member carrying it out on its own when the
    apply handed its roll off (``None`` otherwise). ``span_release_error`` is the store's error
    when its exclusive write span failed to release after the env persisted, ``None`` when it
    released.
    ``fanout`` is the reload broadcast's fleet-fanout summary. NAMES-ONLY: the
    report enumerates key names + worker identities, never env VALUES.
    """
    ran = (
        None
        if outcome.decision is None
        else ran_recycle_roll(outcome.recycle, outcome.decision, self_exit_armed=outcome.self_exit_armed)
    )
    # No roll ran: every worker the reload reached confirmed clean, so all of them were skipped.
    skipped = [] if outcome.decision is None else sorted(outcome.decision.clean)
    roll = ran or {"recycle": [], "skipped": skipped, "fresh": [], "recycle_stopped": None}
    return {
        "hot": list(outcome.hot),
        "recycle": roll["recycle"],
        "skipped": roll["skipped"],
        "fresh": roll["fresh"],
        "refused": [],
        "recycle_stopped": roll["recycle_stopped"],
        "awaiting_respawn": [worker.model_dump() for worker in outcome.awaiting_respawn],
        "roll_deferred": outcome.roll_deferred,
        "roll_deferred_to": outcome.roll_deferred_to,
        "span_release_error": outcome.span_release_error,
        "fanout": fleet_fanout(outcome.fleet),
    }


class FleetBroadcastError(RuntimeError):
    """Raised after a mutation has PERSISTED when the post-persist propagation does not fully complete.

    The local reload raised, the fleet broadcast raised, or both; or, once the reload was
    out, the recycle roll the change owes raised (a roll a bounded run could not hand to its
    member among them). The fleet report is attached (``reachable=False`` when the broadcast
    itself raised) so the caller can surface it and knows the change already landed on disk;
    the specific failure is the ``cause`` and the report's ``error``. ``span_release_error`` is the
    store's error when the change's exclusive write span failed to release after the persist
    (``None`` when it released), so a caller that recovers keeps it on the result it answers with.
    ``local`` is the raising worker's own entry for that op (``None`` when it ran none). For a
    reload it is the statement the recycle decision still reads, when it answers for the worker's
    env, if the broadcast could not reach the bus and so the report holds no row for it; no other
    op's entry is read by a recycle decision.

    ``roll`` is the recycle decision's roll the raising door ran on the report before it raised
    (``None`` when the door ran none, e.g. a reload inside a deferring scope, whose scope rolls
    once at its end), or the outcome a roll that failed left
    (:class:`~tai42_skeleton.config.recycle_roll.RollFailedError`). Two readers render it as roll
    fields (:func:`roll_outcome_body`): the unbounded HTTP error handler's body
    (:func:`~tai42_skeleton.app.error_envelope.fleet_broadcast_error_handler`) and a backup
    import's section report. A run-tool call, an MCP tool call and a job answer with the error's
    text alone. A roll that armed the writer's own exit owes it into the edge's writer-exit scope,
    which runs it after the error reply as after any other (:mod:`~tai42_skeleton.app.writer_exit`).
    """

    roll: RollOutcome | None = None

    def __init__(
        self,
        op_name: str,
        report: FleetResult,
        cause: BaseException,
        *,
        span_release_error: str | None = None,
        local: LocalApplyResult | None = None,
    ) -> None:
        super().__init__(f"{op_name}: change persisted but fleet propagation failed — {cause}")
        self.report = report
        self.span_release_error = span_release_error
        self.local = local


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


class PublishedOp(NamedTuple):
    """A published fleet op: its fleet ``report`` and this worker's own entry ``local``.

    ``local`` is ``None`` when this worker applied nothing (a non-member, or a target list that
    excludes it). A report the bus could not complete holds no row for this worker, so a reader
    that judges this worker's own apply reads ``local``.
    """

    report: FleetResult
    local: LocalApplyResult | None


class CensusPublisher(Protocol):
    """The op-start census seam :func:`snapshot_membership` reads.

    The one method of :class:`~tai42_skeleton.app.bus.WorkerBus` it needs, so a
    publisher typed against a narrower bus protocol satisfies it structurally.
    """

    async def expected_at_start(self) -> dict[str, int]: ...


async def op_start_census[T](read: Callable[[], Awaitable[T]], op_name: str, *, without: str) -> T | None:
    """An operation's census read at its start: what ``read`` returns, ``None`` when it raises.

    The one rule every op-start census read follows. The read informs the op and never decides
    it: ``publish`` takes its own census and surfaces a census it cannot take in the op's
    report or error, and a roll reads the census itself and stops on an outage. So a read that
    fails here never aborts the op, whatever it raised: an outage, or a bus fault such as a
    presence row the census cannot parse, which the op's later reads meet again while it lasts.
    Loud: the failure is logged at WARNING with its traceback, naming ``op_name`` and
    ``without``, what the op goes on without.
    """
    try:
        return await read()
    except Exception:
        logger.warning("worker bus: op-start census for %r failed — %s", op_name, without, exc_info=True)
        return None


async def snapshot_membership(bus: CensusPublisher, op_name: str) -> dict[str, int] | None:
    """Pin the expected-confirmation membership to op START, before the local apply can age it out.

    The membership snapshot an UNTARGETED publisher takes when the membership is all it needs from
    its op-start census.

    Whole-fleet only, by construction: an untargeted op's expected set IS the census,
    and :meth:`~tai42_skeleton.app.bus.WorkerBus.publish` takes that census after the
    caller has already applied locally. A TARGETED op needs no snapshot — its expected
    set is the caller's explicit list, so a named target stays expected at a known life
    whether or not its presence row is still there when ``publish`` reads the census.

    ``None`` when the census does not answer (:func:`op_start_census`): the op then runs on
    exactly the post-apply membership this snapshot exists to correct.

    The snapshot is :meth:`~tai42_skeleton.app.bus.WorkerBus.expected_at_start`, which owns the
    ordering rule (read before the local side effect); this function reads it under the op-start
    failure rule (:func:`op_start_census`). :func:`broadcast` takes it itself (it owns the local
    apply), and a publisher whose local apply is separate and compensation-rolled-back upstream —
    the store-backed preset fan-outs — takes it at its OWN pre-apply point and passes it in. A
    publisher that needs more than the membership from its op-start census reads the census once
    and takes the snapshot from those rows
    (:meth:`~tai42_skeleton.app.bus.WorkerBus.expected_from_census`), as the config service's
    whole-fleet reloads do. Nothing enforces either from here; a new untargeted publisher that
    takes neither silently reports against its post-apply membership.
    """
    return await op_start_census(
        bus.expected_at_start,
        op_name,
        without="falling back to the publish-time census (a worker whose presence fades during the local apply "
        "may go unreported)",
    )


async def broadcast(
    op: FleetOp,
    targets: list[str] | None,
    apply: Callable[[], Awaitable[Any]],
    *,
    publish_on_local_failure: bool = False,
    pre_mutation_errors: tuple[type[BaseException], ...] = (),
) -> dict[str, Any]:
    """Validate targets, pin the expected membership, apply locally per the self-targeting rule, then broadcast.

    ``op`` is the typed fleet op; ``targets`` is ``None`` for
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
    await validate_fleet_targets(bus, targets)
    # Read the membership the report will be judged against BEFORE the local apply below
    # can age it out.
    expected_at_start = await snapshot_membership(bus, op.op) if targets is None else None
    published = await apply_and_publish(
        bus,
        op,
        targets,
        apply,
        expected_at_start=expected_at_start,
        publish_on_local_failure=publish_on_local_failure,
        pre_mutation_errors=pre_mutation_errors,
    )
    return published.report.model_dump(mode="json")


async def validate_fleet_targets(bus: WorkerBus, targets: list[str] | None) -> None:
    """Check ``targets`` against the census BEFORE any local side effect, a name absent from it a 400.

    A typo'd worker name is a loud caller mistake (:class:`BadRequestError`), never a silent
    narrowing and never a bare 500. ``None`` (the whole fleet) checks nothing.
    """
    if targets is None:
        return
    try:
        await bus.validate_targets(targets)
    except UnknownFleetTargetsError as exc:
        raise BadRequestError(str(exc)) from exc


async def apply_and_publish(
    bus: WorkerBus,
    op: FleetOp,
    targets: list[str] | None,
    apply: Callable[[], Awaitable[Any]],
    *,
    expected_at_start: dict[str, int] | None,
    publish_on_local_failure: bool,
    pre_mutation_errors: tuple[type[BaseException], ...] = (),
    failure_payload: Callable[[Exception], Any] | None = None,
) -> PublishedOp:
    """Apply locally when this worker is a target, then publish ``op``; return the report and this worker's entry.

    A non-member (a forked job child) is never a fleet target — it has no serving surface of
    its own — so it applies nothing locally and its member converges as a sibling. A failed
    local apply aborts before anything is published unless ``publish_on_local_failure`` is set
    and the failure is not one of ``pre_mutation_errors``; then it still publishes and raises
    :class:`FleetBroadcastError` with the report attached; ``failure_payload``, when given, is
    the payload that ``failed`` self entry states, read from the failure. ``expected_at_start`` is the
    membership the caller read before calling (:func:`snapshot_membership`), ``None`` for a
    targeted op. An unconfirmed worker is logged loudly (:func:`log_non_convergence`).
    """
    self_targeted = bus.identity.member and (targets is None or bus.identity.name in targets)
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
            local = LocalApplyResult(
                outcome=OpOutcome.failed,
                payload=None if failure_payload is None else failure_payload(exc),
                error=f"{type(exc).__name__}: {exc}",
            )
        else:
            local = LocalApplyResult(outcome=OpOutcome.applied, payload=result)

    report = await bus.publish(op, targets, local, expected_at_start=expected_at_start)
    # The report is embedded in the response, but an unconfirmed worker is also a
    # loud, visible failure — never a silently stale sibling.
    log_non_convergence(report)
    if local_failure is not None:
        raise FleetBroadcastError(report.op, report, local_failure, local=local) from local_failure
    return PublishedOp(report, local)
