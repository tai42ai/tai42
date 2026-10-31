"""The celery beat's scheduler: RedBeat's, reporting each tick of the beat loop as this process's progress.

celery's beat service sleeps for the interval each ``tick`` returns, then ticks again, so that
interval is the promise for the next tick. RedBeat's ``tick`` extends its Redis lock first, so a
beat that cannot reach Redis stops reporting. The app names this class as its ``beat_scheduler``,
for the standalone beat and for a beat a worker embeds with ``-B``.

Before its first tick a beat takes RedBeat's lock, which another beat may hold: RedBeat's own
``beat_init`` receiver blocks on it and reports nothing meanwhile. :func:`register` puts a receiver
in its place that tries the same lock once a second without blocking and reports each try, so a
beat waiting for the lock reads alive and takes it within a second of it lapsing.
"""

from __future__ import annotations

import logging
from typing import Any

from celery.signals import beat_init
from redbeat import RedBeatScheduler
from redbeat.schedulers import LUA_EXTEND_TO_SCRIPT, acquire_distributed_beat_lock, get_redis
from tai42_contract.backend.runtime import ProgressReporter

logger = logging.getLogger(__name__)

# The promise of a tick that returns no positive interval: the beat service then ticks again
# without sleeping, so the next tick is due as soon as this one's Redis commands return.
_SPIN_PROMISE_SECONDS = 1.0

# Seconds between two tries for RedBeat's lock while another beat holds it, and so the promise of
# each report meanwhile.
_LOCK_POLL_SECONDS = 1.0

_report: ProgressReporter | None = None


def register(report: ProgressReporter) -> None:
    """Report the beat's progress through ``report``, also while it waits for RedBeat's lock.

    Called from the beat runtime's ``build``, and from the worker runtime's ``build`` for a beat
    the worker embeds (:mod:`~tai42_backend_celery.core.embedded_beat`), never at import. It puts
    :func:`_acquire_lock_reporting` in the place of RedBeat's own ``beat_init`` receiver. A second
    call with the same reporter changes nothing; one with another reporter raises ``RuntimeError``,
    because one process reports through one reporter.
    """
    global _report
    if _report is not None and _report != report:
        raise RuntimeError("the celery beat already reports its progress through another reporter")
    _report = report
    beat_init.disconnect(acquire_distributed_beat_lock)
    # A receiver connected again stays connected once, so a second call adds no second receiver.
    beat_init.connect(_acquire_lock_reporting, weak=False)


def _acquire_lock_reporting(sender: Any = None, **_: Any) -> None:
    """Take RedBeat's lock before the beat's first tick, reporting each try as a pass of the beat's loop.

    Builds the lock RedBeat builds (its key, its timeout, its extend script) and tries it without
    blocking once a second until it is held or the beat is asked to stop. A lock another beat holds
    is logged at WARNING once, with its key. A scheduler with no lock key takes no lock, as in
    RedBeat.
    """
    scheduler = sender.scheduler
    if not scheduler.lock_key:
        return
    if _report is None:
        raise RuntimeError("the celery beat waits for RedBeat's lock with no progress reporter registered")
    redis_client = get_redis(scheduler.app)
    lock = redis_client.lock(scheduler.lock_key, timeout=scheduler.lock_timeout, sleep=scheduler.max_interval)
    # RedBeat's extend script sets the lock's timeout instead of adding to it.
    lock.lua_extend = redis_client.register_script(LUA_EXTEND_TO_SCRIPT)
    warned = False
    while not lock.acquire(blocking=False):
        if not warned:
            logger.warning(
                "beat: RedBeat lock %s is held by another beat; waiting for it and reporting meanwhile",
                scheduler.lock_key,
            )
            warned = True
        _report(next_within=_LOCK_POLL_SECONDS)
        if sender._is_shutdown.wait(_LOCK_POLL_SECONDS):
            return
    logger.info("beat: Acquired lock")
    scheduler.lock = lock


class ProgressReportingScheduler(RedBeatScheduler):
    """RedBeat's scheduler, reporting each tick of the beat loop as this process's progress."""

    def tick(self, *args: Any, **kwargs: Any) -> float:
        """Run RedBeat's tick, then report it with the interval the beat service will sleep."""
        interval = super().tick(*args, **kwargs)
        if _report is None:
            raise RuntimeError(
                "the celery beat scheduler ticked with no progress reporter registered: it runs outside "
                "tai backend beat"
            )
        _report(next_within=interval if interval > 0 else _SPIN_PROMISE_SECONDS)
        return interval
