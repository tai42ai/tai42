"""A bounded change's fleet recycle roll, handed to the bus member it acts for and carried out there.

A change run under a bound (:func:`~tai42_kit.utils.run_bound.run_bound`: a dispatched job's
budget, an armed turn budget) cannot wait out the roll's drains: one target's announced drain
alone can outlast the bound. So the change hands its roll to the member it acts for — the
backend worker that forked its job, or the worker it runs in — through the bus
:class:`~tai42_skeleton.app.bus.ops.CarryRoll` op (:func:`hand_off_roll`), and answers with the
roll deferred to that member. The member answers the op once it took the roll holds, and holds
the roll until the run released it: the release is owed to the run's bound, which sends it at
its exit, once the run ended, its answer sent or the run cut
(:class:`~tai42_skeleton.app.bus.ops.ReleaseRoll` over the bus from a forked job child, onto the
serving loop in a member process), unless the member provably took nothing. The member
then carries the roll out as its own tracked task (:func:`carry_handed_off_roll`), outside every
bound: it waits each target's drain plus the step, and, when the roll converged and the writer
needs its own restart, departs itself last. What the change holds across its roll passes to the
member as the op's named roll holds (:mod:`~tai42_skeleton.config.roll_holds`), which the member
keeps until the roll ended.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence, Set
from typing import TYPE_CHECKING, NoReturn

from tai42_skeleton.app.bus.models import OpOutcome
from tai42_skeleton.app.bus.ops import CarryRoll, CarryRollReply, ReleaseRoll, carry_roll_reply, release_roll_reply
from tai42_skeleton.config.recycle_policy import UNREACHABLE_REPORT, recycle_decision
from tai42_skeleton.config.recycle_roll import Orchestrate, roll_decided

if TYPE_CHECKING:
    from tai42_kit.utils.run_bound import RunBound

    from tai42_skeleton.app.bus import FleetResult, WorkerBus, WorkerIdentity, WorkerKind
    from tai42_skeleton.app.server import TaiMCP
    from tai42_skeleton.app.writer_exit import WriterExitScope

logger = logging.getLogger(__name__)


class RollHandOffUnknownError(RuntimeError):
    """The roll may have been sent to the bus member ``member`` names, but its answer is not known.

    Whether the member started the roll is unknown, so the roll is the member's to carry out
    and judge, never judged by the change that handed it off. A refusal
    (:class:`RollNotHandedError`) means the roll did not start.
    """

    def __init__(self, message: str, *, member: str) -> None:
        """Build the error from its ``message`` and the ``member`` the roll may have been sent to."""
        super().__init__(message)
        self.member = member


class RollNotHandedError(RuntimeError):
    """The bus member refused the roll, or could not take it: it holds nothing, and the roll did not start."""


async def hand_off_roll(
    bus: WorkerBus,
    report: FleetResult,
    writer: WorkerIdentity,
    pinned: Mapping[str, tuple[WorkerKind, int]],
    *,
    bound: RunBound,
    change: str,
    writer_exits: bool,
    addressed: Set[str] | None,
    holds: Sequence[str] = (),
) -> str:
    """Hand the roll ``change`` decided to the bus member this process acts for; return that member's name.

    ``bound`` is the bound the change runs under. ``holds`` names the roll holds
    (:mod:`~tai42_skeleton.config.roll_holds`) the member takes before it answers and keeps
    until the roll ended.

    The op names the roll's identity and the seconds left of ``bound``, the latest the run
    answers. A forked job child (a non-member) sends the member the :class:`CarryRoll` op over
    the bus; a member process gives the op to its own app, on its serving loop, since a bus op
    never reaches its publisher. The roll's release is owed to ``bound`` before the op goes out
    (:class:`_OwedRelease`), so a bound that already settled raises then, with no op sent; the
    bound's exit sends the release once the run ended, its answer sent or the run cut
    (:func:`_release_over_bus`, :func:`_release_in_this_member`), unless the member provably took
    nothing: its app could not take the op, or it is leaving.

    Raises when the hand-off fails, naming the member, the change, and whether the reload
    reached the bus (:func:`_reload_reach`); what the change persisted is not the roll's to
    state, so the error claims nothing of it. When the member is leaving, or, in a member
    process, its own app could not take the op (it is not built, or a hold the op names cannot
    be taken), it raises :class:`RollNotHandedError`, whose text says the roll did not start,
    and no release is sent. A cancellation of the run propagates as it is, and the release is
    sent at the bound's exit all the same: the member may hold the roll. When its answer
    does not tell — the op's ``failed`` row over the bus (a bus op also fails on a post-apply
    handler that raised after the member took the roll), the bus failed while the op was out
    (its publish reported the bus unreachable, or raised), no answer came, or the answer is not
    a carry reply — it raises :class:`RollHandOffUnknownError` naming the member, whose text says
    whether the member started the roll is unknown, and the release is sent at the bound's exit
    all the same: a member that took the op starts the roll on it, and one that never took it
    answers it ``failed``, logged at ERROR.
    """
    op = CarryRoll(
        op="carry_roll",
        change=change,
        roll=uuid.uuid4().hex,
        answers_within=bound.seconds_left(),
        report=report,
        pinned=dict(pinned),
        writer=writer,
        addressed=tuple(sorted(addressed)) if addressed is not None else None,
        writer_exits=writer_exits,
        holds=tuple(holds),
    )
    member = bus.member_identity.name
    app = _member_app(op, member) if bus.identity.member else None
    owed = _OwedRelease(
        _release_in_this_member(app, op) if app is not None else _release_over_bus(bus, member, op, bound)
    )
    bound.once_ended(owed)
    try:
        if app is not None:
            payload = await _carry_in_this_member(app, op, member)
        else:
            payload = await _send_to_member(bus, op, member)
        reply = _carry_reply(payload, member, op)
        if reply.kind == "already_leaving":
            _raise_member_leaving(member, op, reply.reason)
    except RollNotHandedError:
        owed.due = False
        raise
    logger.info(
        "the fleet recycle for %s is handed to %r, which carries it out on its own once this run ended",
        change,
        member,
    )
    return member


class _OwedRelease:
    """The release of a handed-off roll, owed to the run's bound before the op goes out, sent while due.

    It is ``due`` from the moment it is owed; the hand-off withdraws it on a refusal it can
    prove, and the bound's exit then sends nothing.
    """

    def __init__(self, send: Callable[[], Awaitable[None]]) -> None:
        """Hold the release ``send``, due."""
        self._send = send
        self.due = True

    async def __call__(self) -> None:
        """Send the release when it is due."""
        if self.due:
            await self._send()


def _carry_reply(payload: object, member: str, op: CarryRoll) -> CarryRollReply:
    """Read ``member``'s answer ``payload`` to ``op`` as a carry reply; raise that the answer is unknown otherwise."""
    try:
        reply = carry_roll_reply(payload)
    except TypeError as error:
        _raise_unknown(member, op, f"its answer is malformed: {error}")
    if reply is None:
        _raise_unknown(member, op, f"its answer names no carried roll: {payload!r}")
    return reply


def _member_app(op: CarryRoll, member: str) -> TaiMCP:
    """This member process's own app, which takes the roll ``op`` hands off; raises when it is not built."""
    from tai42_skeleton.app.instance import built_app

    app = built_app()
    if app is None:
        _raise_not_handed(member, op, "its app is not built")
    return app


async def _carry_in_this_member(app: TaiMCP, op: CarryRoll, member: str) -> object:
    """Give ``op`` to this member process's own ``app`` on its serving loop; return the app's answer."""
    try:
        return await app.carry_roll(op)
    except Exception as error:
        _raise_not_handed(member, op, f"its app did not take the op: {type(error).__name__}: {error}")


def _release_in_this_member(app: TaiMCP, op: CarryRoll) -> Callable[[], Awaitable[None]]:
    """The release of the roll ``op`` handed to this member process's own ``app``, owed to the run's bound.

    It schedules the release onto the serving loop and waits for nothing
    (:meth:`~tai42_skeleton.app.lifecycle.carried_roll.CarriedRollMixin.release_carried_roll`).
    A serving loop no longer there (the worker is stopping, which cancels the held roll with its
    own ERROR line) is logged at ERROR, never raised into the run's answer.
    """

    async def release() -> None:
        try:
            app.release_carried_roll(op.roll, op.change)
        except RuntimeError:
            logger.exception(
                "fleet recycle: the run that handed this worker the roll %s for %s ended, but the release could "
                "not reach the serving loop",
                op.roll,
                op.change,
            )

    return release


def _release_over_bus(bus: WorkerBus, member: str, op: CarryRoll, bound: RunBound) -> Callable[[], Awaitable[None]]:
    """The release of the roll ``op`` handed to ``member`` over the bus, owed to the run's ``bound``.

    It sends ``member`` the :class:`ReleaseRoll` op and waits for its answer within what is left
    of ``bound``: a bounded run never waits past its bound. The answer is logged at INFO when the
    member started the roll on it or had started it by the run's bound; any other end — the
    member refused it, gave no answer, or the bus failed, or the wait reached the bound — is one
    ERROR line naming the member, the change, the roll and why. It never raises into the run's
    answer: the change landed, and its answer, when one was sent, says the member carries the
    roll, or may, which stays true, since a member holding the roll starts it by the run's bound
    at the latest.
    """

    async def release() -> None:
        why = await _release_answer(bus, member, ReleaseRoll(op="release_roll", roll=op.roll, change=op.change), bound)
        if why is not None:
            logger.error(
                "fleet recycle: the release of the roll %s for %s could not be confirmed by %r — %s. %r, when it "
                "holds the roll, starts it by the run's bound at the latest",
                op.roll,
                op.change,
                member,
                why,
                member,
            )

    return release


async def _release_answer(bus: WorkerBus, member: str, op: ReleaseRoll, bound: RunBound) -> str | None:
    """Send ``op`` to ``member`` within what is left of ``bound``; ``None`` once it confirmed, else why not."""
    window = asyncio.timeout(bound.seconds_left())
    try:
        async with window:
            result = await bus.publish(op, targets=[member], local=None)
    except Exception as error:
        if window.expired():
            return "the wait for its answer reached the run's bound"
        return f"the bus failed: {type(error).__name__}: {error}"
    if not result.reachable:
        return f"the bus is unreachable: {result.error}"
    row = next((r for r in result.results if r.name == member), None)
    if row is None:
        return "it did not answer"
    if row.outcome is not OpOutcome.applied:
        return f"the op did not apply there: {row.error or row.detail or row.outcome.value}"
    try:
        reply = release_roll_reply(row.payload)
    except TypeError as error:
        return f"its answer is malformed: {error}"
    if reply is None:
        return f"its answer names no release: {row.payload!r}"
    if reply.kind == "started":
        logger.info("the fleet recycle for %s is released to %r, which starts it now", op.change, member)
    else:
        logger.info("the fleet recycle for %s had started on %r by the run's bound", op.change, member)
    return None


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
    """Raise :class:`RollNotHandedError`: ``member`` refused the roll ``op`` hands off, and why; it did not start."""
    raise RollNotHandedError(
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
