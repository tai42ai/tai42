"""The kit Redis client's WARNING log, as the retry tests read and react to it."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import pytest

from tai42_kit.clients.impl import redis as redis_mod

# A password no log line may ever carry.
MARKER_PASSWORD = "m4rker-pw-never-logged"
LOGGER = redis_mod.__name__


def kit_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The messages of the kit WARNINGs ``caplog`` captured."""
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


class _OnWarning(logging.Handler):
    """Runs ``action`` once, when the ``nth`` kit WARNING is emitted."""

    def __init__(self, nth: int, action: Callable[[], None]) -> None:
        super().__init__(logging.WARNING)
        self.nth, self.action, self.seen = nth, action, 0

    def emit(self, record: logging.LogRecord) -> None:
        self.seen += 1
        if self.seen == self.nth:
            self.action()


@asynccontextmanager
async def on_warning(nth: int, action: Callable[[], None]) -> AsyncIterator[None]:
    handler = _OnWarning(nth, action)
    logger = logging.getLogger(LOGGER)
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
