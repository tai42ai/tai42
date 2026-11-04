"""rq's scheduler as a thread of the worker process, for the ``gevent`` pool.

rq's ``RQScheduler`` moves ``enqueue_at`` / ``enqueue_in`` jobs out of the scheduled-job registry; rq
forks it as a child process. A process ``gevent.monkey.patch_all`` has patched runs its worker loop as a
greenlet on the OS thread that also runs the host event loop, so a fork from there copies the hub, the
host loop's tasks and its sockets into the child, which then runs them. This scheduler runs in the
worker process instead: a ``threading.Thread`` (a greenlet under the patch) that keeps the Redis client
it built in this process, is supervised by rq's own ``is_alive()`` check and is stopped by flag and
join. The recurring-schedule runtime (``tai backend beat``, the rq-scheduler package) is a different
program.
"""

from __future__ import annotations

import threading

from rq.scheduler import RQScheduler, run


class ThreadedScheduler(RQScheduler):
    """rq's scheduler, its loop run on a daemon thread of this process rather than a forked child.

    The thread keeps rq's ``_process`` name, so rq's supervision works unchanged: the worker's
    maintenance pass restarts a thread that has died (``acquire_locks(auto_start=True)`` calls
    :meth:`start`), and rq's ``run`` logs the loop's start, its stop and any exception it raises.
    """

    def _install_signal_handlers(self) -> None:
        """The worker process owns its signals; the stop arrives through :meth:`request_stop`."""

    def start(self) -> threading.Thread:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Run the scheduler loop on a daemon thread of this process, keeping the connection already built.

        rq drops the connection before it forks because a client cannot cross the fork; here the
        loop runs in this process, so it keeps the client. The thread is a daemon so a loop stuck in
        Redis I/O cannot hold the process open past the worker's end; the worker's normal end is a
        joined stop (``CustomRQGeventWorker.stop_scheduler``).
        """
        self._status = self.Status.STARTED
        self._process = threading.Thread(target=run, args=(self,), name="Scheduler", daemon=True)
        self._process.start()
        return self._process
