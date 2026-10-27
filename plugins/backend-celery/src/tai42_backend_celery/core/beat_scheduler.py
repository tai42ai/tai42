"""The celery beat's scheduler: RedBeat's, reporting each tick of the beat loop as this process's progress.

celery's beat service sleeps for the interval each ``tick`` returns, then ticks again, so that
interval is the promise for the next tick. RedBeat's ``tick`` extends its Redis lock first, so a
beat that cannot reach Redis stops reporting. The app names this class as its ``beat_scheduler``,
for the standalone beat and for a beat a worker embeds with ``-B``.
"""

from __future__ import annotations

from typing import Any

from redbeat import RedBeatScheduler
from tai42_contract.backend.runtime import ProgressReporter

# The promise of a tick that returns no positive interval: the beat service then ticks again
# without sleeping, so the next tick is due as soon as this one's Redis commands return.
_SPIN_PROMISE_SECONDS = 1.0

_report: ProgressReporter | None = None


def register(report: ProgressReporter) -> None:
    """Report the beat's progress through ``report``.

    Called from the beat runtime's ``build``, and from the worker runtime's ``build`` for a beat
    the worker embeds (:mod:`~tai42_backend_celery.core.embedded_beat`), never at import. A second call with
    the same reporter changes nothing; one with another reporter raises ``RuntimeError``, because
    one process reports through one reporter.
    """
    global _report
    if _report is not None and _report != report:
        raise RuntimeError("the celery beat already reports its progress through another reporter")
    _report = report


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
