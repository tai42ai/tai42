"""A marketplace operation run under its locks, with its fleet recycle roll run once its changes are made.

An operation changes the plugins directory and the install store under the EXCLUSIVE fleet key.
A worker its roll restarts verifies its plugin environments at boot under the SHARED key
(:func:`~tai42_skeleton.marketplace.environments.boot_verification.verify_environments_at_boot`),
so the roll cannot run while the key is held exclusively, nor before the store row it is to read
is written. :func:`guarded_operation` therefore defers every roll the operation's config changes
owe (:func:`~tai42_skeleton.config.service.deferring_rolls`), and once the operation's body is
done downgrades the fleet hold to shared (:meth:`~tai42_skeleton.marketplace.locks.FleetHold.downgrade`)
and runs them as one roll. Both locks stay held until the roll ends, so a second operation is
still refused while a restarted worker's verification is let in.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from tai42_skeleton.config.service import ApplyResult, ConfigService, RollOutcome, deferring_rolls
from tai42_skeleton.marketplace import locks

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


@asynccontextmanager
async def guarded_operation(
    fleet: Callable[[], AbstractAsyncContextManager[locks.FleetHold]],
    svc: Callable[[], ConfigService],
    *,
    change: str,
    writer_exits: bool = True,
) -> AsyncIterator[OperationRoll]:
    """Run the body under :func:`~tai42_skeleton.marketplace.locks.operation_guard`, then its deferred roll.

    The body's config changes defer their rolls. When the body returns and a roll is pending,
    the fleet hold is downgraded to shared and the roll runs (:meth:`ConfigService.run_deferred_rolls`),
    its outcome left on the yielded :class:`OperationRoll`; ``writer_exits`` says whether the
    writer's own recycle follows as its post-reply self-exit. When the body raises after
    deferring a roll — its forward change, its unwind's revert, or both landed — that roll still
    runs, downgraded the same way and with no self-exit (a failed operation arms none), and the
    body's error is re-raised. A roll that itself fails there is logged at ERROR and noted on the
    body's error, which is the one raised. ``svc`` reaches the config pipeline fresh, since the
    body's reloads swap app internals; ``change`` names the operation in the roll's logs.
    """
    async with locks.operation_guard(fleet) as hold:
        operation = OperationRoll()
        with deferring_rolls() as deferral:
            try:
                yield operation
            except Exception as step_error:
                if deferral.rolls:
                    try:
                        await hold.downgrade()
                        await svc().run_deferred_rolls(deferral, writer_exits=False, change=change)
                    except Exception as roll_error:
                        logger.exception("the fleet recycle roll after %s failed also failed", change)
                        step_error.add_note(
                            "the fleet recycle roll after the failure also failed: "
                            f"{type(roll_error).__name__}: {roll_error}"
                        )
                raise
        if deferral.rolls:
            await hold.downgrade()
            operation.roll = await svc().run_deferred_rolls(deferral, writer_exits=writer_exits, change=change)
