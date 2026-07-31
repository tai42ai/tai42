"""The Celery :class:`~tai42_contract.backend.Backend` implementation.

``launch(args)`` starts the Celery runtime for this process — ``worker``,
``beat``, or ``flower``. The worker runs the Celery CLI on a worker thread so
this process's asyncio loop (which reads the app's worker-bus subscription) stays
alive; since Celery skips its signal handlers off the main thread, ``launch``
installs loop-level SIGTERM/SIGINT handlers that request a warm (then cold)
shutdown. ``beat`` and ``flower`` run nothing else on the loop, so they block it
directly and keep Celery's native signal handling. Before starting the worker,
``launch`` registers the prefork-pool turnover (worker-scoped) that re-forks the
pool after every mutating bus op, then gates the worker start on the boot-ready
latch (``wait_until_ready``) so no pool child is forked against a half-built tool
registry.
"""

from __future__ import annotations

import asyncio
import logging
import signal as signal_module
import sys
from collections.abc import Sequence
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.backend import Backend

from tai42_backend_celery.core import prefork

logger = logging.getLogger(__name__)

_LAUNCH_SUBCOMMANDS = frozenset({"worker", "beat", "flower"})

# The Celery application module handed to the Celery CLI (`-A`).
_CELERY_APP_PATH = "tai42_backend_celery.core.app"

# Upper bound on the wait for the app to become boot-ready before the worker
# starts — a loud backstop for a boot that never completes. A worker that times
# out here refuses to start (and fork its pool) against a half-built tool
# registry and fails loudly.
_APP_READY_TIMEOUT_SECONDS = 120


async def _await_app_ready() -> None:
    """Block until the app's boot self-resync has built the tool registry, or fail
    loudly at the timeout.

    A Celery prefork worker forks pool children that inherit the tool registry at
    fork time; a child forked before the boot self-resync finishes inherits a
    half-built registry and fails every job routed to it permanently. Gating the
    whole worker start on the readiness latch means no pool child is forked until
    the registry is built and stable. Awaited on the serving loop, so the bus
    subscription's self-resync (which sets the latch) runs concurrently. A timeout
    is a boot that never completed — raise rather than start against a half-built
    registry.
    """
    try:
        await asyncio.wait_for(tai42_app.lifecycle.wait_until_ready(), timeout=_APP_READY_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise RuntimeError(
            f"celery worker: the app did not become ready within {_APP_READY_TIMEOUT_SECONDS}s "
            "(boot self-resync never completed); refusing to start the worker against a half-built tool registry"
        ) from exc


def _celery_cli_main() -> None:
    """Run the Celery umbrella CLI against ``sys.argv`` (set by ``launch``)."""
    from celery.__main__ import main as celery_main

    celery_main()


def _request_worker_shutdown() -> None:
    """Ask the in-process Celery worker to shut down: warm first (finish in-flight
    tasks), a repeat request escalates to cold. Drives the ``celery.worker.state``
    flags directly, since the worker runs off the main thread where Celery skips
    its own signal handlers."""
    from celery.worker import state

    if state.should_stop is None:
        logger.warning("celery worker: warm shutdown requested")
        state.should_stop = 0
    else:
        logger.warning("celery worker: cold shutdown requested")
        state.should_terminate = 0


class CeleryBackend(Backend):
    """Celery execution backend (see the module docstring for the design)."""

    async def launch(self, args: Sequence[str]) -> None:
        if not args:
            raise ValueError("launch requires a subcommand: worker | beat | flower")
        subcmd, *rest = args
        if subcmd not in _LAUNCH_SUBCOMMANDS:
            raise ValueError(f"Unknown Celery launch subcommand {subcmd!r}; expected one of: worker, beat, flower")

        sys.argv = ["celery", "-A", _CELERY_APP_PATH, subcmd, *rest]

        if subcmd != "worker":
            # beat/flower: nothing else runs on this loop, so inline execution
            # keeps Celery's native signal handling.
            _celery_cli_main()
            return

        # Wire the prefork-pool turnover before the worker boots (worker only).
        prefork.register()
        # Gate the worker start on boot-readiness: the boot self-resync builds the
        # tool registry non-atomically, and a pool child forked before it finishes
        # inherits a half-built registry and fails its jobs permanently. Awaiting here
        # (before the Celery CLI spawns the pool) means no child is forked until the
        # latch opens; the prefork-pool turnover heals later mutations but is not
        # load-bearing at boot.
        await _await_app_ready()
        await self._launch_worker_off_loop()

    async def _launch_worker_off_loop(self) -> None:
        """Run the Celery worker on a worker thread, keeping this loop alive.

        Loop-level SIGTERM/SIGINT handlers request a warm (then cold) shutdown; a
        cancellation of this coroutine requests the same warm shutdown and awaits
        the worker's exit, so no live worker thread is left behind.
        """
        loop = asyncio.get_running_loop()
        installed: list[Any] = []
        for sig in (signal_module.SIGTERM, signal_module.SIGINT):
            try:
                loop.add_signal_handler(sig, _request_worker_shutdown)
            except (RuntimeError, ValueError, NotImplementedError) as e:
                # Signal handlers need the main thread (and POSIX); without them
                # shutdown comes via task cancellation.
                logger.warning("cannot install %s handler for worker shutdown: %s", sig.name, e)
            else:
                installed.append(sig)
        worker_run = loop.run_in_executor(None, _celery_cli_main)
        try:
            await asyncio.shield(worker_run)
        except asyncio.CancelledError:
            _request_worker_shutdown()
            await worker_run
            raise
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)


# Plain call (not decorator) so ``CeleryBackend`` keeps its plain class type.
tai42_app.backends.register_backend(CeleryBackend)
