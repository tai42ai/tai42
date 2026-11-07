"""The rolls a bounded change hands this worker: accepted, held until the run released them, then carried out.

A roll handed here is answered once its holds are taken, mid-change; the run that handed it off
releases it at its bound's exit, once it answered its caller, and only then does this worker
start the roll, as its own task. A run in another process can end with no word, so a roll it
handed over the bus starts by that run's bound at the latest, with an ERROR line.
"""

import asyncio
import contextlib
import functools
import logging
from dataclasses import dataclass, field

from tai42_skeleton.app.bus import ExitRefusal
from tai42_skeleton.app.bus.ops import CarryRoll, CarryRollReply, ReleaseRollReply
from tai42_skeleton.app.lifecycle.state import LifecycleState, run_on_serving_loop
from tai42_skeleton.app.recycle import orchestrate_recycle
from tai42_skeleton.app.root_task import spawn_root_task
from tai42_skeleton.app.writer_exit import WriterExitScope

logger = logging.getLogger(__name__)


@dataclass
class HeldRoll:
    """A roll this worker accepted: the change it rolls, its release, and whether it has started."""

    change: str
    released: asyncio.Event = field(default_factory=asyncio.Event)
    started: bool = False


class CarriedRollMixin(LifecycleState):
    """Lifecycle mixin owning the rolls handed to this worker (the ``carry_roll`` op), their release and tasks."""

    async def carry_roll(self, op: CarryRoll) -> dict[str, str]:
        """Take the roll ``op`` hands this worker from a change it runs itself; return its answer.

        The in-process counterpart of the bus op: a change running in this worker (an in-process
        job, a turn under its budget) gives the op here, from any loop, since a bus op never
        reaches its publisher. The op is accepted on the serving loop, where the carried roll
        runs for the process lifetime (:meth:`accept_carried_roll`), through
        :func:`~tai42_skeleton.app.lifecycle.state.run_on_serving_loop`: a caller cancelled
        meanwhile cancels the acceptance and returns once it has ended there. The run that
        handed the roll off is in this process, and its bound's exit releases the roll here
        (:meth:`release_carried_roll`), so the roll is held with no deadline.
        """
        serving = self.serving_loop()
        if serving is None:
            raise RuntimeError(f"fleet recycle: {op.change} hands its roll to this worker, which has no serving loop")
        return await run_on_serving_loop(serving, functools.partial(self.accept_carried_roll, op, answers_within=None))

    async def accept_carried_roll(self, op: CarryRoll, *, answers_within: float | None) -> dict[str, str]:
        """Accept the roll ``op`` hands this worker and hold it until released; answer once its holds are taken.

        A worker that decided to leave (:attr:`~tai42_skeleton.app.bus.WorkerBus.departing`)
        carries nothing and answers ``{"already_leaving": reason}``. Otherwise it holds its own exit
        for the op's change from here, in one step with that check
        (:meth:`~tai42_skeleton.app.bus.WorkerBus.defer_exit_unless_departing`), so a recycle op
        from another worker's roll leaves it to this roll and no decision to leave is taken
        before this roll ended. It keeps the roll as held (:class:`HeldRoll`, under ``op.roll``)
        and starts a tracked root task (:meth:`_carry_under_holds`). That task first takes the
        roll holds the op names (:func:`~tai42_skeleton.config.roll_holds.enter_roll_holds`);
        once they are taken this answers ``{"carrying": change}``, and the task keeps them until
        the roll ended. The task then waits for the run's release
        (:meth:`_release_carried_roll`) — for ``answers_within`` seconds at most, when given — and
        carries the roll out. A hold it cannot take raises here, the op unapplied and nothing
        carried; so does a task stopped before it took them (the worker's shutdown); this answer
        cancelled before the holds are taken cancels the task. The task is cancelled at shutdown
        and a failed roll is logged at ERROR by its done-callback; whatever way it ends, the hold
        it did not settle is released and the roll is no longer held.
        """
        bus = self.bus
        hold = bus.defer_exit_unless_departing(op.change)
        if isinstance(hold, ExitRefusal):
            logger.info("worker bus: a handed-off roll for %s on a worker already leaving (%s)", op.change, hold.reason)
            return CarryRollReply(kind="already_leaving", reason=hold.reason).reply()
        exits = WriterExitScope()
        exits.adopt_hold(bus, hold)
        held: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        roll = HeldRoll(change=op.change)
        self._held_rolls[op.roll] = roll
        task = spawn_root_task(self._carry_under_holds(op, exits, held, roll, answers_within), name="tai-carried-roll")
        self._carried_roll_tasks.add(task)
        task.add_done_callback(self._carried_roll_tasks.discard)
        task.add_done_callback(lambda _: self._held_rolls.pop(op.roll, None))
        task.add_done_callback(self._log_task_exception)
        task.add_done_callback(functools.partial(_stopped_before_holds, held, op.change))
        task.add_done_callback(
            lambda _: exits.abandon(f"the roll for {op.change} handed to this worker ended before its exit ran")
        )
        try:
            # Shielded: the task resolves ``held``; an answer cancelled here cancels the task.
            await asyncio.shield(held)
        except asyncio.CancelledError:
            held.cancel()
            task.cancel()
            raise
        return CarryRollReply(kind="carrying", reason=op.change).reply()

    async def _carry_under_holds(
        self,
        op: CarryRoll,
        exits: WriterExitScope,
        held: asyncio.Future[None],
        roll: HeldRoll,
        answers_within: float | None,
    ) -> None:
        """Take the roll holds ``op`` names, resolve ``held``, and carry the roll out under them once released.

        A hold that cannot be taken is set on ``held``, which :meth:`accept_carried_roll` raises
        to the op's sender, and nothing is carried. Once the holds are taken the task waits for
        ``roll`` to be released; with ``answers_within`` given, a release that has not come by
        then is logged at ERROR naming the change and the bound — the run was cut at its limit or
        died — and the roll starts anyway. A task cancelled once the holds were taken but before
        the roll started (the worker's shutdown, or the answer cancelled) is logged at ERROR
        naming the change, and re-raised. The holds are given back once the roll ended, whatever
        way it ends.
        """
        from tai42_skeleton.config.roll_handoff import carry_handed_off_roll
        from tai42_skeleton.config.roll_holds import enter_roll_holds

        try:
            async with enter_roll_holds(op.holds):
                held.set_result(None)
                try:
                    await _released(roll, answers_within)
                except asyncio.CancelledError:
                    logger.exception(
                        "the fleet recycle for %s handed to this worker was cancelled before it was released (the "
                        "worker is stopping, or its answer was cancelled) — no target was rolled",
                        op.change,
                    )
                    raise
                await carry_handed_off_roll(self.bus, op, exits, orchestrate=orchestrate_recycle)
        except Exception as error:
            if held.done():
                raise
            # The holds were not taken: the answer raises why, and nothing is carried.
            held.set_exception(error)

    def _release_carried_roll(self, roll: str, change: str) -> dict[str, str]:
        """Release the held roll ``roll`` for ``change``: the run that handed it off has answered. Return the answer.

        Runs on the serving loop, for the ``release_roll`` op and for the in-process release
        (:meth:`release_carried_roll`). Answers ``{"started": change}`` when it starts the roll
        now, ``{"already_started": change}`` when the roll had started before, because the run's
        bound passed or an earlier release started it. Raises :class:`LookupError` naming the
        roll and the change when this worker holds no such roll.
        """
        held = self._held_rolls.get(roll)
        if held is None:
            raise LookupError(f"fleet recycle: this worker holds no roll {roll} for {change} to release")
        if held.started:
            logger.info("the run that handed this worker the roll for %s answered after the roll had started", change)
            return ReleaseRollReply(kind="already_started", reason=change).reply()
        held.started = True
        held.released.set()
        logger.info("the run that handed this worker the roll for %s answered: the roll starts", change)
        return ReleaseRollReply(kind="started", reason=change).reply()

    def release_carried_roll(self, roll: str, change: str) -> None:
        """Release the held roll ``roll`` for ``change`` from a run in this process, on the serving loop.

        A notification, not a call: the release is scheduled onto the serving loop from any
        thread and nothing waits for it, so no cancellation of the caller can cut it. Raises
        ``RuntimeError`` when no serving loop is bound, or the bound one is closed. A roll this
        worker no longer holds is logged at ERROR there, since no caller reads the answer.
        """
        serving = self.serving_loop()
        if serving is None:
            raise RuntimeError(f"fleet recycle: the roll for {change} is released on a worker with no serving loop")
        serving.call_soon_threadsafe(self._release_in_this_process, roll, change)

    def _release_in_this_process(self, roll: str, change: str) -> None:
        """Release ``roll`` on the serving loop for a run in this process, logging a roll no longer held."""
        try:
            self._release_carried_roll(roll, change)
        except LookupError:
            logger.exception(
                "fleet recycle: the run that handed this worker the roll for %s answered, but its release found "
                "no roll held here",
                change,
            )

    async def _cancel_carried_rolls(self) -> None:
        """Cancel every carried roll still running and await their end.

        A cancelled carried roll logged its stop itself; one that raised was logged by its
        done-callback, so neither is raised here.
        """
        carried = list(self._carried_roll_tasks)
        for roll in carried:
            roll.cancel()
        await asyncio.gather(*carried, return_exceptions=True)


async def _released(roll: HeldRoll, answers_within: float | None) -> None:
    """Wait for ``roll`` to be released, for ``answers_within`` seconds at most when given.

    A release that has not come by then is logged at ERROR and the roll is marked started, so a
    release coming after reads that it had started.
    """
    if answers_within is None:
        await roll.released.wait()
        return
    # The wait ending at ``answers_within`` is the bus door's deadline, read just below.
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(answers_within):
            await roll.released.wait()
    if roll.released.is_set():
        return
    roll.started = True
    logger.error(
        "the run that handed this worker the roll for %s sent no release by its bound (%gs) — its job was cut "
        "at its limit or died, or its release was lost; carrying the roll now",
        roll.change,
        answers_within,
    )


def _stopped_before_holds(held: asyncio.Future[None], change: str, _task: asyncio.Task[None]) -> None:
    """Fail ``held`` for a carried task that ended without resolving it: it was stopped before it took the holds.

    The task resolves ``held`` itself on every other ending, so one cancelled before its first
    step, or while it took the holds, is the only one left unresolved; the answer waiting on
    ``held`` then raises instead of waiting forever. An answer that was cancelled itself
    resolved ``held`` first, so nothing is set that nobody reads.
    """
    if not held.done():
        held.set_exception(
            RuntimeError(f"fleet recycle: this worker stopped before it took the holds of the roll for {change}")
        )
