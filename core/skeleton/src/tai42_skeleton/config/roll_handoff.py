"""A bounded change's fleet recycle roll, handed to the bus member it acts for and carried out there.

A change run under a bound (:func:`~tai42_kit.utils.run_bound.in_bounded_run`: a dispatched
job's budget, an armed turn budget) cannot wait out the roll's drains: one target's announced
drain alone can outlast the bound. So the change hands its roll to the member it acts for — the
backend worker that forked its job, or the worker it runs in — through the bus
:class:`~tai42_skeleton.app.bus.ops.CarryRoll` op (:func:`hand_off_roll`), and answers with the
roll deferred to that member. The member answers the op at once and carries the roll out as its
own tracked task (:func:`carry_handed_off_roll`), outside every bound: it waits each target's
drain plus the step, and, when the roll converged and the writer needs its own restart, departs itself last. What the
change holds across its roll passes to the member as the op's named roll holds
(:mod:`~tai42_skeleton.config.roll_holds`), which the member keeps until the roll ended.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence, Set
from typing import TYPE_CHECKING, NoReturn

from tai42_skeleton.app.bus.models import OpOutcome
from tai42_skeleton.app.bus.ops import CarryRoll, carry_roll_reply
from tai42_skeleton.config.recycle_policy import UNREACHABLE_REPORT, recycle_decision
from tai42_skeleton.config.recycle_roll import Orchestrate, roll_decided

if TYPE_CHECKING:
    from tai42_skeleton.app.bus import FleetResult, WorkerBus, WorkerIdentity, WorkerKind
    from tai42_skeleton.app.writer_exit import WriterExitScope

logger = logging.getLogger(__name__)


class RollHandOffUnknownError(RuntimeError):
    """The roll may have been sent to the bus member ``member`` names, but its answer is not known.

    Whether the member started the roll is unknown, so the roll is the member's to carry out
    and judge, never judged by the change that handed it off. Every other hand-off failure means
    the roll did not start.
    """

    def __init__(self, message: str, *, member: str) -> None:
        """Build the error from its ``message`` and the ``member`` the roll may have been sent to."""
        super().__init__(message)
        self.member = member


async def hand_off_roll(
    bus: WorkerBus,
    report: FleetResult,
    writer: WorkerIdentity,
    pinned: Mapping[str, tuple[WorkerKind, int]],
    *,
    change: str,
    writer_exits: bool,
    addressed: Set[str] | None,
    holds: Sequence[str] = (),
) -> str:
    """Hand the roll ``change`` decided to the bus member this process acts for; return that member's name.

    ``holds`` names the roll holds (:mod:`~tai42_skeleton.config.roll_holds`) the member takes
    before it answers and keeps until the roll ended.

    A forked job child (a non-member) sends the member the :class:`CarryRoll` op over the bus; a
    member process gives the op to its own app, on its serving loop, since a bus op never reaches
    its publisher. Raises when the hand-off fails, naming the member, the change, and whether
    the reload reached the bus (:func:`_reload_reach`); what the change persisted is not the
    roll's to state, so the error claims nothing of it. When the member is leaving, or, in a
    member process, its own app could not take the op (it is not built, or a hold the op names
    cannot be taken), the error says the roll did not start. When its answer does not tell — the
    op's ``failed`` row over the bus (a bus op also fails on a post-apply handler that raised
    after the member took the roll), the bus failed while the op was out (its publish reported the
    bus unreachable, or raised), no answer came, or the answer is not a carry reply — it raises
    :class:`RollHandOffUnknownError` naming the member, whose text says whether the member started
    the roll is unknown.
    """
    op = CarryRoll(
        op="carry_roll",
        change=change,
        report=report,
        pinned=dict(pinned),
        writer=writer,
        addressed=tuple(sorted(addressed)) if addressed is not None else None,
        writer_exits=writer_exits,
        holds=tuple(holds),
    )
    member = bus.member_identity.name
    if bus.identity.member:
        payload = await _carry_in_this_member(op, member)
    else:
        payload = await _send_to_member(bus, op, member)
    try:
        reply = carry_roll_reply(payload)
    except TypeError as error:
        _raise_unknown(member, op, f"its answer is malformed: {error}")
    if reply is None:
        _raise_unknown(member, op, f"its answer names no carried roll: {payload!r}")
    if reply.kind == "already_leaving":
        _raise_member_leaving(member, op, reply.reason)
    logger.info("the fleet recycle for %s is handed to %r, which carries it out on its own", change, member)
    return member


async def _carry_in_this_member(op: CarryRoll, member: str) -> object:
    """Give ``op`` to this member process's own app on its serving loop; return the app's answer."""
    from tai42_skeleton.app.instance import built_app

    app = built_app()
    if app is None:
        _raise_not_handed(member, op, "its app is not built")
    try:
        return await app.carry_roll(op)
    except Exception as error:
        _raise_not_handed(member, op, f"its app did not take the op: {type(error).__name__}: {error}")


async def _send_to_member(bus: WorkerBus, op: CarryRoll, member: str) -> object:
    """Send ``op`` over the bus to ``member``; return its ``applied`` payload, raising on any other answer.

    A publish that raises (a Redis error reply, say) may have sent the op before it did, so it
    reads as an answer not known, chained to what the publish raised.
    """
    try:
        result = await bus.publish(op, targets=[member], local=None)
    except Exception as error:
        _raise_unknown(member, op, f"the bus failed: {type(error).__name__}: {error}", cause=error)
    if not result.reachable:
        # The transport failed somewhere in the publish: the op may have gone out before it did.
        _raise_unknown(member, op, f"the bus is unreachable: {result.error}")
    row = next((r for r in result.results if r.name == member), None)
    if row is None:
        _raise_unknown(member, op, "it did not answer")
    if row.outcome is OpOutcome.failed:
        # A bus op fails on its post-apply handlers too, after the member took the roll.
        _raise_unknown(member, op, f"the op failed there: {row.error or row.detail or row.outcome.value}")
    if row.outcome is OpOutcome.recycling:
        # A member's row reads ``recycling`` only once its life began leaving, after which it carries nothing.
        _raise_member_leaving(member, op, row.detail or row.outcome.value)
    if row.outcome is not OpOutcome.applied:
        # No terminal answer came (missing, departed, timed out, resyncing, stale): the member
        # may have taken the op, or take it still.
        _raise_unknown(member, op, f"no answer came: {row.detail or row.outcome.value}")
    return row.payload


def _reload_reach(op: CarryRoll) -> str:
    """Whether the reload whose roll ``op`` hands off reached the bus, read from its report.

    The roll layer knows nothing of what the change behind the reload persisted: the door that
    made the change states that.
    """
    return "the reload broadcast reached the bus" if op.report.reachable else UNREACHABLE_REPORT


def _raise_not_handed(member: str, op: CarryRoll, why: str) -> NoReturn:
    """Raise that ``member`` refused the roll ``op`` hands off, and why: the roll did not start."""
    raise RuntimeError(
        f"fleet recycle: the roll for {op.change} could not be handed to {member!r}, "
        f"the bus member the change acts for — "
        f"{why}. The roll did not start; {_reload_reach(op)}"
    )


def _raise_member_leaving(member: str, op: CarryRoll, why: str) -> NoReturn:
    """Raise that ``member`` is leaving for ``why`` and so carries no roll ``op`` hands off: the roll did not start.

    One text for the one state, whether the member answered the op ``already_leaving`` or its
    row read ``recycling``.
    """
    _raise_not_handed(
        member,
        op,
        f"it is leaving ({why}) and carries nothing; a member that replaces it rolls on its join what that join's "
        "report states, and with no replacement nothing rolls",
    )


def _raise_unknown(member: str, op: CarryRoll, why: str, *, cause: Exception | None = None) -> NoReturn:
    """Raise :class:`RollHandOffUnknownError`: the roll ``op`` hands off may have been sent to ``member``.

    Its answer is not known, so whether the member started the roll is unknown. ``cause``, when
    given, is the error the unknown answer comes from, chained as the raised error's cause.
    """
    error = RollHandOffUnknownError(
        f"fleet recycle: the roll for {op.change} may have been sent to {member!r}, "
        "the bus member the change acts for, "
        f"but its answer is not known — {why}. Whether {member!r} started the roll is unknown; {_reload_reach(op)}. "
        "Read its log and the fleet census",
        member=member,
    )
    if cause is None:
        raise error
    raise error from cause


async def carry_handed_off_roll(
    bus: WorkerBus, op: CarryRoll, exits: WriterExitScope, *, orchestrate: Orchestrate
) -> None:
    """Carry out the roll ``op`` hands this member, holding its exit in ``exits``, and depart last when owed.

    The decision is made again from the op's inputs and the roll runs as the change's own would
    (:func:`~tai42_skeleton.config.recycle_roll.roll_decided`), owing the writer's exit into
    ``exits``, which holds this member's exit from the op's answer on, so a recycle op from
    another worker's roll leaves the member to this roll. Settling ``exits`` once the roll ended
    departs the member when the converged roll owes its restart, else releases the hold. A roll
    that stopped is logged at ERROR by the roll and departs nothing; one cancelled (the worker's
    shutdown) is logged at ERROR naming the change and re-raised.
    """
    decision = recycle_decision(
        op.report, op.pinned, op.writer, addressed=set(op.addressed) if op.addressed is not None else None
    )
    logger.info("carrying the fleet recycle for %s handed to this worker", op.change)
    try:
        await roll_decided(
            bus,
            op.report,
            decision,
            op.pinned,
            change=op.change,
            orchestrate=orchestrate,
            writer_exits=op.writer_exits,
            exits=exits,
        )
    except asyncio.CancelledError:
        logger.exception(
            "the fleet recycle for %s handed to this worker was cancelled mid-roll (the worker is stopping) — "
            "the targets it had not reached keep their state",
            op.change,
        )
        raise
    await exits.settle()
