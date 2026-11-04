"""A marketplace operation run under its locks, with its fleet recycle roll run once its changes are made.

An operation changes the plugins directory and the install store under the EXCLUSIVE fleet key.
A worker its roll restarts verifies its plugin environments at boot under the SHARED key
(:func:`~tai42_skeleton.marketplace.environments.boot_verification.verify_environments_at_boot`),
so the roll cannot run while the key is held exclusively, nor before the store row it is to read
is written. :func:`guarded_operation` therefore defers the recycle decision of every reload the
operation's config changes make (:func:`~tai42_skeleton.config.service.deferring_rolls`), and once
the operation's body is done downgrades the fleet hold to shared
(:meth:`~tai42_skeleton.marketplace.locks.FleetHold.downgrade`) and decides and rolls once.
A roll run here ends before both locks are released, so a second operation is still refused
while a restarted worker's verification is let in. A roll the operation hands off, run under a
bound, outlives the operation's locks, which are released when the operation ends; it names the
shared fleet key as its roll hold (:data:`~tai42_skeleton.marketplace.locks.MARKETPLACE_ROLL_HOLD`):
the bus member carrying it takes the key shared before it answers, while the operation still
holds it, and keeps it until the carried roll ended, so the key is never free while that roll
runs and a second operation, which takes the key exclusively, is refused until then.

Whenever config changes landed and their roll does not complete — the downgrade or the roll
raises, or the operation is cancelled — the error the caller reads says so in one line
(:func:`_unrun_roll_note`), on every path the operation can end on, and the error that stopped the
roll is logged whole at ERROR. A roll stopped by an error is raised as the typed
:class:`~tai42_skeleton.marketplace.errors.UnrunRollError`, which every door renders, unless the
operation already failed with a marketplace error, whose reply carries the line as a note; a
cancellation stays a cancellation, the line noted on it. The line names only the stopping error's
type, so its text stays in the log.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from tai42_skeleton.config.recycle_roll import RollFailedError, RollOutcome
from tai42_skeleton.config.service import ApplyResult, ConfigService, DeferredRolls, deferring_rolls
from tai42_skeleton.marketplace import locks
from tai42_skeleton.marketplace.errors import MarketplaceError, UnrunRollError
from tai42_skeleton.operations._broadcast import FleetBroadcastError

logger = logging.getLogger(__name__)


class OperationRoll:
    """The roll a guarded operation ran once its body was done, ``None`` while none ran."""

    def __init__(self) -> None:
        """No roll yet: :func:`guarded_operation` sets it once the body is done."""
        self.roll: RollOutcome | None = None

    def settled(self, result: ApplyResult | None) -> ApplyResult | None:
        """The operation's config result with its roll folded in (:meth:`ApplyResult.rolled`).

        A roll with no result to carry it means the operation lost the result of the change that
        owed the roll, which raises.
        """
        if result is None:
            if self.roll is not None:
                raise RuntimeError("a marketplace operation rolled a fleet recycle but reported no config result")
            return None
        return result.rolled(self.roll)


def _unrun_roll_note(stopped_by: BaseException) -> str:
    """The line saying config changes landed and the roll they owe did not complete, naming what stopped it.

    The one wording every path that leaves a landed change's roll undone gives. It names the
    stopping error by its type alone: its text (a driver's hosts, a bus's addresses) is logged,
    never put in a reply.
    """
    if isinstance(stopped_by, asyncio.CancelledError):
        detail = "the operation was cancelled"
    else:
        detail = type(stopped_by).__name__
    return f"config changes landed, but the fleet recycle roll they owe did not complete: {detail}"


def _log_unrun_roll(stopped_by: BaseException, *, change: str) -> str:
    """Log at ERROR that the roll ``change`` owes did not complete, with ``stopped_by`` whole, and return the line."""
    note = _unrun_roll_note(stopped_by)
    logger.error("the fleet recycle roll %s owes did not complete (%s)", change, note, exc_info=stopped_by)
    return note


async def _run_owed_roll(
    hold: locks.FleetHold,
    svc: Callable[[], ConfigService],
    deferral: DeferredRolls,
    *,
    change: str,
    writer_exits: bool = True,
) -> RollOutcome | None:
    """Downgrade the fleet hold to shared and decide and roll once over the scope's recorded reloads.

    A roll handed off names the shared fleet key as its roll hold, kept by the member carrying it.

    ``writer_exits`` is whether the reply the operation ends on can be followed by the writer's own
    exit (a masked failure's reply cannot).
    A downgrade that raises (the session dropped), or a roll that fails (the bus broke its
    contract: :class:`~tai42_skeleton.config.recycle_roll.RollFailedError`), raises
    :class:`UnrunRollError` from the error that stopped it, which is logged (:func:`_log_unrun_roll`). A
    cancellation mid-roll propagates as a cancellation with the line noted on it.
    """
    try:
        await hold.downgrade()
        return await svc().run_deferred_rolls(
            deferral, change=change, writer_exits=writer_exits, holds=(locks.MARKETPLACE_ROLL_HOLD,)
        )
    except asyncio.CancelledError as cancelled:
        cancelled.add_note(_log_unrun_roll(cancelled, change=change))
        raise
    except RollFailedError as failed:
        raise UnrunRollError(_log_unrun_roll(failed.failure, change=change)) from failed.failure
    except Exception as stopping_error:
        raise UnrunRollError(_log_unrun_roll(stopping_error, change=change)) from stopping_error


@asynccontextmanager
async def guarded_operation(
    fleet: Callable[[], AbstractAsyncContextManager[locks.FleetHold]],
    svc: Callable[[], ConfigService],
    *,
    change: str,
) -> AsyncIterator[OperationRoll]:
    """Run the body under :func:`~tai42_skeleton.marketplace.locks.operation_guard`, then its deferred roll.

    The body's config changes record their reloads in a deferred scope. When the body returns and a
    reload was recorded, the fleet hold is downgraded to shared and the one recycle decision runs
    over the scope (:meth:`ConfigService.run_deferred_rolls`): the workers whose latest reload
    reports drift are restarted, the siblings whose state is unknown are asked once ready and
    restarted unless their reply confirms them clean or states a refused build (their restart then
    withheld), the writer, never asked, needs its own restart when its own state is unknown, and one
    whose reload was refused at its epoch build yet reports drift is left running, its restart
    withheld and named in the verdict, the writer's own restart armed as its post-reply self-exit
    when it needs one and the roll converged. Its outcome is left on the yielded :class:`OperationRoll`. A downgrade or
    roll that raises there — a dropped session among them — raises :class:`UnrunRollError` from that
    error, saying the changes landed and their roll did not complete.

    When the body raises after recording a reload — its forward change, its unwind's revert, or
    both landed — the decision and roll still run, downgraded the same way, and the body's error
    is re-raised. A :class:`MarketplaceError` or a
    :class:`~tai42_skeleton.operations._broadcast.FleetBroadcastError` (a reload that failed after
    its change landed, a plugin that cannot load among them) carries the roll's outcome
    (``roll``), which its error reply renders, arming the writer's exit there as a success reply
    would; any other body error reaches the caller as a masked 500 that can arm nothing, so the
    writer's own restart is not deferred and a writer still bound to a moved key is listed in the
    logged verdict. A roll that itself fails there is noted on the body's error when that is a
    :class:`MarketplaceError`, whose reply renders its notes; any other body error's reply may
    mask its notes (a :class:`~tai42_skeleton.operations._broadcast.FleetBroadcastError` over a
    cause that is not operator-facing renders as a masked 500), so the :class:`UnrunRollError` is
    raised instead, noting the body's failure by its type. A roll cancelled there raises its
    cancellation, noted the same way.

    A body cancelled after recording a reload (a shutdown drain, a cancelled job, a severed
    caller) starts no roll: a fleet roll runs for minutes and a cancellation is owed promptly, so
    the cancellation propagates with the unrun roll noted on it; the next reload of any door
    restarts what still reports drift. ``svc`` reaches the config pipeline fresh, since the
    body's reloads swap app internals; ``change`` names the operation in the roll's logs.
    """
    async with locks.operation_guard(fleet) as hold:
        operation = OperationRoll()
        with deferring_rolls() as deferral:
            try:
                yield operation
            except asyncio.CancelledError as cancelled:
                if deferral.reloads:
                    cancelled.add_note(_log_unrun_roll(cancelled, change=change))
                raise
            except Exception as step_error:
                if deferral.reloads:
                    carries_roll = isinstance(step_error, (MarketplaceError, FleetBroadcastError))
                    try:
                        outcome = await _run_owed_roll(hold, svc, deferral, change=change, writer_exits=carries_roll)
                    except UnrunRollError as unrun:
                        if not isinstance(step_error, MarketplaceError):
                            unrun.add_note(f"the operation failed before its roll ran: {type(step_error).__name__}")
                            raise
                        step_error.add_note(str(unrun))
                    else:
                        if isinstance(step_error, (MarketplaceError, FleetBroadcastError)):
                            step_error.roll = outcome
                raise
        if deferral.reloads:
            operation.roll = await _run_owed_roll(hold, svc, deferral, change=change)
