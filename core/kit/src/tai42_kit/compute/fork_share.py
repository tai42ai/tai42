"""A process's compute pool, shared with the children it forks.

The sharing process holds a socket pair, the rendezvous, and serves its end on a thread of its own
(:class:`~tai42_kit.compute.pool_wire.PoolServer`, whose module describes the wire). A process
forked, at any remove, from it once the share serves keeps the other end alone and is a
:class:`~tai42_kit.compute.pool_wire.ComputePoolClient` of it. The
sharing process runs each child's job in its own pool, asked for at each job, so a stopped or
broken pool is started again. The sockets are inherited or passed, never named: only a process forked,
at any remove, from the sharing process can reach its pool.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable

from tai42_kit.compute.errors import ComputePoolUnavailableError
from tai42_kit.compute.pool import ComputePool
from tai42_kit.compute.pool_wire import PoolServer
from tai42_kit.compute.wire_sockets import wire_sockets

logger = logging.getLogger(__name__)

_NOT_SERVING = "the compute pool's share did not start serving"


class PoolShare:
    """This process's side: serves the rendezvous the processes forked from it reach its pool over."""

    def __init__(self, own_pool: Callable[[], ComputePool]) -> None:
        """Make the rendezvous and start the serving thread; ``own_pool`` returns the pool a job runs in.

        Raises :class:`~tai42_kit.compute.errors.ComputePoolUnavailableError` when the rendezvous
        cannot be made or the thread does not start serving; nothing is left open then.
        """
        try:
            serving_end, child_end = wire_sockets.socketpair()
        except OSError as exc:
            raise ComputePoolUnavailableError(_NOT_SERVING) from exc
        # This process holds the child end for as long as it shares, so serving never ends on the
        # rendezvous closing.
        self._server = PoolServer(serving_end, own_pool)
        serving = threading.Event()
        failed: list[BaseException] = []
        try:
            threading.Thread(target=self._serve, args=(serving, failed), name="compute pool share", daemon=True).start()
        except Exception as exc:
            failed.append(exc)
        else:
            serving.wait()
        if failed:
            self._server.close()
            wire_sockets.close(child_end)
            raise ComputePoolUnavailableError(_NOT_SERVING) from failed[0]
        # From here a child the process forks keeps the child end alone and sends its jobs on it.
        wire_sockets.keep_in_children(child_end)

    def _serve(self, serving: threading.Event, failed: list[BaseException]) -> None:
        # The loop lives on this thread's frame alone: a forked child, where this thread does not
        # run, never finalizes it, so the child never touches the parent's selector.
        try:
            with asyncio.Runner() as runner:
                runner.run(self._server.serve(on_serving=serving.set))
        except BaseException as exc:
            if serving.is_set():
                logger.exception("the compute pool's share stopped serving")
            else:  # it never served: handed to the starting thread, which raises it
                failed.append(exc)
        finally:
            self._server.close()
            serving.set()
