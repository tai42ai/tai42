"""The rolls a bounded change hands this worker: accepted at once, then carried out as this worker's own tasks."""

import asyncio
import functools
import logging

from tai42_skeleton.app.bus import ExitRefusal
from tai42_skeleton.app.bus.ops import CarryRoll, CarryRollReply
from tai42_skeleton.app.lifecycle.state import LifecycleState, run_on_serving_loop
from tai42_skeleton.app.recycle import orchestrate_recycle
from tai42_skeleton.app.root_task import spawn_root_task
from tai42_skeleton.app.writer_exit import WriterExitScope

logger = logging.getLogger(__name__)


class CarriedRollMixin(LifecycleState):
    """Lifecycle mixin owning the rolls handed to this worker (the ``carry_roll`` op) and their tasks."""

    async def carry_roll(self, op: CarryRoll) -> dict[str, str]:
        """Take the roll ``op`` hands this worker from a change it runs itself; return its answer.

        The in-process counterpart of the bus op: a change running in this worker (an in-process
        job, a turn under its budget) gives the op here, from any loop, since a bus op never
        reaches its publisher. The op is accepted on the serving loop, where the carried roll
        runs for the process lifetime (:meth:`accept_carried_roll`), through
        :func:`~tai42_skeleton.app.lifecycle.state.run_on_serving_loop`: a caller cancelled
        meanwhile cancels the acceptance and returns once it has ended there.
        """
        serving = self.serving_loop()
        if serving is None:
            raise RuntimeError(f"fleet recycle: {op.change} hands its roll to this worker, which has no serving loop")
        return await run_on_serving_loop(serving, functools.partial(self.accept_carried_roll, op))

    async def accept_carried_roll(self, op: CarryRoll) -> dict[str, str]:
        """Accept the roll ``op`` hands this worker and start carrying it out; answer once its holds are taken.

        A worker that decided to leave (:attr:`~tai42_skeleton.app.bus.WorkerBus.departing`)
        carries nothing and answers ``{"already_leaving": reason}``. Otherwise it holds its own exit
        for the op's change from here, in one step with that check
        (:meth:`~tai42_skeleton.app.bus.WorkerBus.defer_exit_unless_departing`), so a recycle op
        from another worker's roll leaves it to this roll and no decision to leave is taken
        before this roll ended. It starts the roll as a tracked root task
        (:meth:`_carry_under_holds`). That task first takes the roll holds the op names
        (:func:`~tai42_skeleton.config.roll_holds.enter_roll_holds`); once they are taken this
        answers ``{"carrying": change}``, and the task keeps them until the roll ended. A hold it
        cannot take raises here, the op unapplied and nothing carried; so does a task stopped
        before it took them (the worker's shutdown); this answer cancelled before the holds are
        taken cancels the task. The task is cancelled at shutdown and a failed roll is logged at
        ERROR by its done-callback; whatever way it ends, the hold it did not settle is released.
        """
        bus = self.bus
        hold = bus.defer_exit_unless_departing(op.change)
        if isinstance(hold, ExitRefusal):
            logger.info("worker bus: a handed-off roll for %s on a worker already leaving (%s)", op.change, hold.reason)
            return CarryRollReply(kind="already_leaving", reason=hold.reason).reply()
        exits = WriterExitScope()
        exits.adopt_hold(bus, hold)
        held: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        answered = asyncio.Event()
        task = spawn_root_task(self._carry_under_holds(op, exits, held, answered), name="tai-carried-roll")
        self._carried_roll_tasks.add(task)
        task.add_done_callback(self._carried_roll_tasks.discard)
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
        answered.set()
        return CarryRollReply(kind="carrying", reason=op.change).reply()

    async def _carry_under_holds(
        self, op: CarryRoll, exits: WriterExitScope, held: asyncio.Future[None], answered: asyncio.Event
    ) -> None:
        """Take the roll holds ``op`` names, resolve ``held``, and carry the roll out under them once ``answered``.

        A hold that cannot be taken is set on ``held``, which :meth:`accept_carried_roll` raises
        to the op's sender, and nothing is carried. A task cancelled once the holds were taken but
        before the roll started (the worker's shutdown, or the answer cancelled) is logged at ERROR
        naming the change, and re-raised. The holds are given back once the roll ended, whatever
        way it ends.
        """
        from tai42_skeleton.config.roll_handoff import carry_handed_off_roll
        from tai42_skeleton.config.roll_holds import enter_roll_holds

        try:
            async with enter_roll_holds(op.holds):
                held.set_result(None)
                try:
                    await answered.wait()
                except asyncio.CancelledError:
                    logger.exception(
                        "the fleet recycle for %s handed to this worker was cancelled before it started (the worker "
                        "is stopping, or its answer was cancelled) — no target was rolled",
                        op.change,
                    )
                    raise
                await carry_handed_off_roll(self.bus, op, exits, orchestrate=orchestrate_recycle)
        except Exception as error:
            if held.done():
                raise
            # The holds were not taken: the answer raises why, and nothing is carried.
            held.set_exception(error)

    async def _cancel_carried_rolls(self) -> None:
        """Cancel every carried roll still running and await their end.

        A cancelled carried roll logged its stop itself; one that raised was logged by its
        done-callback, so neither is raised here.
        """
        carried = list(self._carried_roll_tasks)
        for roll in carried:
            roll.cancel()
        await asyncio.gather(*carried, return_exceptions=True)


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
