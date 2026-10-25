"""A conforming ``exclusive_write`` span for in-memory config-store fakes."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


class ExclusiveWriteStore:
    """Base of an in-memory config-store fake whose ``exclusive_write`` holds the store's lock from entry through exit.

    One span is held at a time, whichever thread enters it and whichever exits it, as the
    config manager contract asks of a provider. With ``fail_exit`` set, the span's release
    raises it after the lock is released, whether the body completed or raised, as a provider
    whose release cannot complete raises from the exit.
    """

    def __init__(self, *, fail_exit: Exception | None = None) -> None:
        self._write_span_lock = threading.Lock()
        self.fail_exit = fail_exit

    @contextmanager
    def exclusive_write(self) -> Iterator[None]:
        try:
            with self._write_span_lock:
                yield
        finally:
            if self.fail_exit is not None:
                raise self.fail_exit
