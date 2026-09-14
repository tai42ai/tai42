"""Worker-bus rebind / removal fan-out and the membership census the mutating
preset doors publish so every serving worker converges on the new binding state."""

from __future__ import annotations

from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import FleetResult, LocalApplyResult, OpOutcome
from tai42_skeleton.operations._broadcast import log_non_convergence, snapshot_membership

# The wire op names these fan-outs publish. Named once because the op-start census
# is read under the same name the publish carries, at a different point in the
# caller — two literals could drift into censusing one op and publishing another.
_RELOAD_OP = "reload_tool"
_REMOVE_OP = "remove_tool"


def _fleet_fanout_ready() -> bool:
    """Whether this worker can fan a preset op out to the fleet: its bus is built AND
    its slot identity is minted.

    ``False`` only during the boot startup handlers — the declared-preset-seed applier
    runs as an ``on_startup`` handler, BEFORE the bus subscription claims this worker's
    slot (the bus is built and its identity minted only after the handlers). Every worker
    runs that applier on its OWN boot, so a boot-time seed create/upgrade needs no fan-out
    — each worker applies it locally. A reload re-runs the applier with the bus already
    subscribed, so it fans out normally, as does every API-door mutation."""
    try:
        _ = instance.app.bus.identity
    except RuntimeError:
        return False
    return True


async def _census_at_start(op_name: str) -> dict[str, int] | None:
    """Pin the expected-confirmation membership BEFORE this worker's store write and
    local rebind — the untargeted-publisher discipline
    :func:`~tai42_skeleton.operations._broadcast.snapshot_membership` owns.

    These publishers apply locally and broadcast afterwards, so ``publish``'s own
    census is taken on the far side of the store write: a sibling whose presence
    faded across it would simply be absent from the expected set, and the op would
    report converged without it. The caller reads this at its true pre-apply point,
    which is why it is a parameter of the fan-out rather than taken inside it.

    ``None`` when the bus cannot yet fan out (the boot-time seed applier, pre-subscribe):
    :func:`_fanout_reload` / :func:`_fanout_remove` collapse to a local-only report."""
    if not _fleet_fanout_ready():
        return None
    return await snapshot_membership(instance.app.bus, op_name)


async def _fanout_reload(name: str, expected_at_start: dict[str, int] | None) -> FleetResult:
    """Broadcast a preset rebind on the worker bus so every worker re-reads the active
    body and rebinds ``name``. The op carries only ``kind`` + ``name``; each worker
    re-reads the store itself. The store write and local rebind have already landed by
    the time this runs (its self entry is a truthful ``applied``), so an unconfirmed
    sibling is surfaced as a loud non-convergence ERROR log, and re-running the
    mutation (or a ``reload_config``) is the recovery.

    The local apply is separate and compensation-rolled-back upstream, so this cannot
    ride ``broadcast()``'s apply-inside model — but it shares its non-convergence
    logging and its op-start census (``expected_at_start``, from
    :func:`_census_at_start`) so a stranded sibling is never silent.

    Returns the per-worker fleet report so the mutation door can embed it in its
    response (:func:`~tai42_skeleton.operations._broadcast.fleet_fanout` shapes it — the
    read-your-writes barrier a deployer needs) exactly as the template writers do. The
    sibling names a rename's second fan-out is judged against are derived from it with
    :func:`_addressed_siblings`; see :func:`_union_census`.

    Collapses to a ``local_only`` report when the bus cannot yet fan out (the boot-time
    seed applier, before this worker's slot is claimed) — the local rebind has already
    landed and every sibling seeds itself on its own boot, so there is no fleet to reach."""
    if not _fleet_fanout_ready():
        return FleetResult(op=_RELOAD_OP, local_only=True)
    report = await instance.app.bus.publish(
        {"op": _RELOAD_OP, "kind": "preset", "name": name},
        None,
        LocalApplyResult(outcome=OpOutcome.applied),
        expected_at_start=expected_at_start,
    )
    log_non_convergence(report)
    return report


def _addressed_siblings(report: FleetResult) -> set[str]:
    """The sibling names a broadcast actually ADDRESSED (self excluded) — the report IS
    that set, expected verdicts and gap rows alike. A rename's second fan-out needs it:
    see :func:`_union_census`."""
    self_name = instance.app.bus.identity.name
    return {result.name for result in report.results if result.name != self_name}


async def _fanout_remove(name: str, expected_at_start: dict[str, int] | None) -> FleetResult:
    """Broadcast a preset removal on the worker bus so every worker tears ``name``
    down. Same already-applied self entry, op-start census, and non-convergence
    logging as :func:`_fanout_reload`, and it likewise returns the per-worker fleet
    report so the delete door can embed it in its response. Collapses to a ``local_only``
    report when the bus cannot yet fan out (the boot-time seed applier, pre-subscribe)."""
    if not _fleet_fanout_ready():
        return FleetResult(op=_REMOVE_OP, local_only=True)
    report = await instance.app.bus.publish(
        {"op": _REMOVE_OP, "kind": "preset", "name": name},
        None,
        LocalApplyResult(outcome=OpOutcome.applied),
        expected_at_start=expected_at_start,
    )
    log_non_convergence(report)
    return report


# A worker the reload reached but no census of ours ever saw. Real generations are
# minted by INCR and start at 1, so this is below every life: the reply gate treats
# any reply as a successor's and the worker gets a computed verdict instead of a
# silent pass. That is the honest reading of "it was addressed, we do not know
# which life".
_LIFE_UNKNOWN = 0


def _union_census(
    at_start: dict[str, int] | None,
    addressed: set[str],
    after_reload: dict[str, int] | None,
) -> dict[str, int]:
    """The membership a rename's REMOVE fan-out is judged against.

    A rename publishes twice, and the second half cannot simply reuse the first
    half's snapshot: a worker that joined after the snapshot was taken is not in
    it, yet the reload broadcast reached it and told it to bind ``new_name``. Left
    unexpected by the remove, that worker keeps the old binding and the op reports
    converged anyway. So the expected set is the UNION of three readings, in
    descending order of authority over the generation:

    * the pre-rename snapshot — the life that was owed the rename from the start,
      and the one :meth:`~tai42_skeleton.app.bus.WorkerBus.publish` re-admits at;
    * a census taken after the reload broadcast — a joiner still live, at its
      current generation;
    * every name the reload REPORT named, which is the only reading that cannot
      miss a worker the reload actually addressed (a joiner that faded during the
      broadcast is in neither census), carried at :data:`_LIFE_UNKNOWN`.
    """
    union: dict[str, int] = dict(after_reload or {})
    union.update(at_start or {})
    for worker in addressed:
        union.setdefault(worker, _LIFE_UNKNOWN)
    return union
