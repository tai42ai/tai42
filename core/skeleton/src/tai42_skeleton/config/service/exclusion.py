"""The write exclusion every config write holds from its first store read through its persist.

The exclusion is two holds, taken in this order: the process-wide config-write lock, then the
config store's exclusive write span. Every write entry persists as the last statement inside
it, so the span's exit is the first step after the persist. An exit that raises after the
write's body completed is therefore a failed release of a write that landed: it is
ERROR-logged and recorded on the yielded :class:`WriteSpan`, and the write goes on to its
reload and broadcast tail. An exit that raises after the body raised is ERROR-logged and the
body's own error propagates, so the caller reads the error of the write it made. A cancellation
the exit held (its ``__context__``) is re-raised after the log instead, so a cancelled writer
stays cancelled.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tai42_skeleton.app.reload_gate import hold_recorded, hold_recorded_blocking

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from contextlib import AbstractContextManager

    from tai42_kit.process_lock import ProcessLock

logger = logging.getLogger(__name__)


@dataclass
class WriteSpan:
    """What the write exclusion reports to the write it covers once it is released.

    ``release_error`` is the store's error (``<ErrorType>: <message>``) when its exclusive
    write span failed to release after the write persisted, ``None`` when it released.
    """

    release_error: str | None = None


@asynccontextmanager
async def write_exclusion(lock: ProcessLock, span: AbstractContextManager[None]) -> AsyncIterator[WriteSpan]:
    """Hold ``lock``, then the store's exclusive write ``span``, each with the caller recorded as waiting.

    Yields the :class:`WriteSpan` the release outcome is recorded on. An error entering
    either hold, an error of the body with a clean exit, and a ``BaseException`` leaving the
    exit propagate unchanged. An exit that raises an ``Exception`` is logged at ERROR with
    its traceback; then a cancellation it held is re-raised, else the body's error when the
    body raised, else the failure is recorded as :attr:`WriteSpan.release_error` and the
    exclusion returns normally.
    """
    record = WriteSpan()
    body_error: BaseException | None = None
    completed = False
    release_failure: Exception | None = None
    try:
        async with hold_recorded(lock) as party, hold_recorded_blocking(span, party):
            try:
                yield record
            except BaseException as exc:
                body_error = exc
                raise
            completed = True
    except Exception as exit_error:
        if exit_error is body_error or not (completed or body_error is not None):
            # The body's own error, or a hold that failed on entry.
            raise
        release_failure = exit_error
    if release_failure is None:
        return
    held = release_failure.__context__
    cancelled = held if isinstance(held, asyncio.CancelledError) else None
    _log_release_failure(release_failure, completed=completed, cancelled=cancelled is not None)
    # Raised outside the handler, so the error keeps the context it was raised with.
    if cancelled is not None:
        raise cancelled
    if body_error is not None:
        raise body_error
    record.release_error = f"{type(release_failure).__name__}: {release_failure}"


def _log_release_failure(error: Exception, *, completed: bool, cancelled: bool) -> None:
    """ERROR-log a failed release of the store's write span, with its traceback, naming what it leaves behind.

    ``completed`` is whether the write's body completed, which is its persist; a body that raised
    states only that, since its own error names what it wrote.
    """
    if not completed:
        raised = "the writer's cancellation" if cancelled else "its own error"
        outcome = f"the write raised before it completed, and {raised} is raised"
    elif cancelled:
        outcome = (
            "the write persisted and the change stands, but the writer was cancelled, so its reload and broadcast "
            "do not run"
        )
    else:
        outcome = "the write persisted and the change stands; its reload and broadcast still run"
    logger.error(
        "the config store failed to release its exclusive write span: %s — %s; the store may still hold the span "
        "until this process exits, so a later write can block or be refused until then",
        f"{type(error).__name__}: {error}",
        outcome,
        exc_info=error,
    )
