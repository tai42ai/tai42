"""The ``claude_code`` drive's session teardown, run when the agent stream ends or is closed.

Every step is attempted even when an earlier one failed, so one failure never strands the
rest — above all, an ephemeral session is always destroyed, its volume reaped and its
injected env gone with it. Every failure reaches the caller once every step was attempted:
one failure as itself, several together as an ``ExceptionGroup``. A step that raises its own
``CancelledError`` while nothing cancelled the teardown has failed like any other step. A
cancellation of the caller does not cut the teardown short: the steps finish first, then the
cancellation goes on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from tai42_contract.sandbox import SandboxSession

from tai42_agents.claude_code.credentials import redact_transcript, scrub_credentials
from tai42_agents.claude_code.errors import ClaudeCodeError
from tai42_agents.claude_code.frames import drain_handle

logger = logging.getLogger(__name__)


async def tear_down_session(
    session: SandboxSession,
    *,
    handle: Any,
    ws: str,
    policy: Any,
    secrets: list[str],
    terminal: bool,
    ephemeral: bool,
) -> None:
    """Stop the runner and clean the session, attempting every step.

    1. Kill the runner's exec (``handle``, when one was started), then await its output to end,
       so no runner code is still mid-drive while the volume is cleaned. The wait follows only
       a kill that succeeded: an un-killed runner's output may not end before its exec timeout.
    2. On a ``terminal`` exit, scrub the injected credential files and redact the kept
       transcript. A park-suspend is not terminal: the bearer file stays for the resume.
    3. Destroy an ``ephemeral`` session: it is never cached, so it is reaped now.

    The steps run as their own task awaited through ``asyncio.shield``. When the caller is
    cancelled, it waits for that task inside a shielded anyio scope (an anyio cancel scope
    re-delivers its cancellation at every wake-up, so a plain re-await would end at once) and
    keeps waiting through every further plain asyncio cancel until the steps are done. It then
    logs any step failure — the cancellation is what propagates — and re-raises the first
    cancellation once.
    """
    steps = asyncio.ensure_future(
        _run_steps(
            session, handle=handle, ws=ws, policy=policy, secrets=secrets, terminal=terminal, ephemeral=ephemeral
        )
    )
    try:
        await asyncio.shield(steps)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):
            while not steps.done():
                try:
                    await asyncio.wait([steps])
                except asyncio.CancelledError:
                    # A further plain asyncio cancel: the task's cancel count keeps it, and the
                    # wait goes on until the steps finish.
                    continue
        _log_failure_under_cancellation(steps)
        raise


async def _run_steps(
    session: SandboxSession,
    *,
    handle: Any,
    ws: str,
    policy: Any,
    secrets: list[str],
    terminal: bool,
    ephemeral: bool,
) -> None:
    failures: list[Exception] = []

    async def attempt(step: Callable[[], Awaitable[None]]) -> bool:
        try:
            await step()
        except asyncio.CancelledError as exc:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            # Nothing cancelled the teardown: the step's own ``CancelledError`` is its failure,
            # raised with the others instead of cancelling a caller nobody cancelled.
            failure = ClaudeCodeError("a claude_code teardown step was cancelled while the teardown was not")
            failure.__cause__ = exc
            failures.append(failure)
            return False
        except Exception as exc:
            failures.append(exc)
            return False
        return True

    if handle is not None and await attempt(handle.kill):
        await attempt(lambda: drain_handle(handle))
    if terminal:
        await attempt(lambda: scrub_credentials(session, ws=ws))
        await attempt(lambda: redact_transcript(session, ws=ws, policy=policy, secrets=secrets))
    if ephemeral:
        await attempt(session.destroy)

    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise ExceptionGroup("claude_code session teardown failed", failures)


def _log_failure_under_cancellation(steps: asyncio.Future[None]) -> None:
    """Log the teardown's failure when a cancellation, not the failure, leaves the caller."""
    if steps.cancelled():
        return
    exc = steps.exception()
    if exc is not None:
        logger.error("claude_code session teardown failed while its caller was cancelled", exc_info=exc)
