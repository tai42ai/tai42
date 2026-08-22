"""The periodic sandbox reap loop.

Started at app start ONLY when a sandbox provider is registered (a no-op door
otherwise, so it is never spawned without a provider), cancelled cleanly on
shutdown/reload — the same run-until-cancelled discipline as the interactions
expiry reaper. Each tick reaps every session past its ``expires_at`` and logs the
reaped ids at INFO. A reap-cycle exception is logged LOUDLY at ERROR with its full
cause and the loop continues to the next tick — never a bare swallow, and repeated
consecutive failures keep erroring loudly rather than downgrading.
"""

from __future__ import annotations

import asyncio
import logging

from tai42_contract.app import tai42_app
from tai42_kit.sandbox import SandboxDispatchSettings

logger = logging.getLogger(__name__)


def _reap_interval_seconds() -> float:
    """The reap sweep interval — the kit-declared dispatch default, so the skeleton
    names no concrete provider."""
    return SandboxDispatchSettings().reap_interval_seconds


async def run_sandbox_reap_loop() -> None:
    """Reap expired sandbox sessions on a fixed cadence until cancelled.

    Reads the registered provider through the ``app.sandboxes`` facade each tick; a
    tick with no provider is a benign no-op (the task is only spawned with one, but a
    reload could retire it). A cancellation propagates for a clean shutdown exit; any
    other per-tick error is logged loudly and the loop survives to the next tick — a
    silently dead reaper is the exact failure mode this task removes."""
    interval = _reap_interval_seconds()
    while True:
        await asyncio.sleep(interval)
        try:
            sandbox = tai42_app.sandboxes.sandbox
            if sandbox is None:
                continue
            reaped = await sandbox.reap()
            for session_id in reaped:
                logger.info("sandbox reaper: reaped expired session %s", session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("sandbox reaper: reap cycle failed; continuing to the next tick", exc_info=True)
