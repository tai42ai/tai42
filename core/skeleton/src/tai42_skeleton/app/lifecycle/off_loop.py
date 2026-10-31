"""Run a coroutine to completion off any caller's loop on a throwaway worker-thread loop."""

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from tai42_kit.fork_gate import fork_gate

from tai42_skeleton.app import lifecycle as _lifecycle

logger = logging.getLogger(__name__)


def run_blocking(coro_factory: Callable[[], Any]) -> Any:
    """Run a coroutine to completion regardless of the caller's context.

    The single off-loop runner: a private event loop on a worker thread, safe
    from a loop-less caller and from inside the server loop. Every off-loop
    snapshot / probe / reload-handler run goes through here.

    The worker thread copies no context, so the private loop starts from the fork gate's
    :meth:`~tai42_kit.fork_gate.ForkGate.fresh_context`: nothing of the caller's context
    but the fork gate's marks. Called by a rebuild holding the fork gate, a fork the
    coroutine makes is the rebuild's own and does not wait on it; called inside a job
    span, such a fork nests in that span instead of queueing behind a pending rebuild.
    """

    async def _run_and_cleanup() -> Any:
        try:
            return await coro_factory()
        finally:
            # asyncio.run tears down this throwaway loop without closing the
            # per-loop pooled clients the coroutine opened (reload handlers
            # open pooled clients via app.clients.client_ctx), so close them
            # here before teardown. A cleanup failure is logged loudly but
            # must not replace the coroutine's result or its exception.
            try:
                await _lifecycle.shutdown_all_clients()
            except Exception:
                logger.exception("Error closing pooled clients after run_blocking")

    with ThreadPoolExecutor(max_workers=1) as executor:
        context = fork_gate.fresh_context()
        return executor.submit(context.run, lambda: asyncio.run(_run_and_cleanup())).result()
