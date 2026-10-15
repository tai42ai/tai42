"""The checkpoint read in progress: the values it failed to decode and the references it decoded.

A saver opens a :class:`CheckpointRead` around the library's read. The Redis saver's library catches
every exception raised while it revives a checkpoint document and returns the raw value, so the kit
records each decode failure here and the saver raises it once the read finishes. A stored value that
holds a reference announces it here as it is decoded, and a saver resolves a read only when it holds one.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextvars import ContextVar, Token
from typing import Any

from tai42_kit.llm.checkpoint.checkpoint import CheckpointSerializationError

decode_failures: ContextVar[list[CheckpointSerializationError] | None] = ContextVar(
    "tai42_checkpoint_decode_failures", default=None
)
_read_references: ContextVar[list[Any] | None] = ContextVar("tai42_checkpoint_read_references", default=None)


def record_failure(error: CheckpointSerializationError) -> None:
    """Record ``error`` for the read in progress; outside a read it is not recorded."""
    failures = decode_failures.get()
    if failures is not None:
        failures.append(error)


def record_reference(item: Any) -> None:
    """Announce a decoded reference to the read in progress; outside a read it is not recorded."""
    references = _read_references.get()
    if references is not None:
        references.append(item)


class CheckpointRead:
    """The decode failures and the announced references of one checkpoint read, open inside its ``with``."""

    def __init__(self) -> None:
        """An empty read."""
        self.failures: list[CheckpointSerializationError] = []
        self.references: list[Any] = []

    def __enter__(self) -> CheckpointRead:
        """Open the read for the code it encloses."""
        self._tokens = (decode_failures.set(self.failures), _read_references.set(self.references))
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the read, restoring the enclosing one."""
        failures, references = self._tokens
        _read_references.reset(references)
        decode_failures.reset(failures)


def open_failures() -> tuple[list[CheckpointSerializationError], Token[Any]]:
    """Open a failure record alone; the caller resets ``decode_failures`` with the token."""
    failures: list[CheckpointSerializationError] = []
    return failures, decode_failures.set(failures)


def raise_failures(failures: Sequence[CheckpointSerializationError], thread_id: Any) -> None:
    """Raise the first of ``failures``, counting them, when there is any."""
    if failures:
        raise CheckpointSerializationError(
            f"checkpoint read for thread {thread_id!r} found {len(failures)} undecodable value(s): {failures[0]}"
        ) from failures[0]
