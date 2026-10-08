"""A trace id generator that starts a root span in a chosen trace (the SDK's public ``IdGenerator`` plug-in)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

_CHOSEN_TRACE_ID: ContextVar[int | None] = ContextVar("tai42_otel_chosen_trace_id", default=None)


class ChosenTraceIdGenerator(RandomIdGenerator):
    """Returns the trace id chosen by :func:`chosen_trace_id` for a parent-less span, else a random one."""

    def generate_trace_id(self) -> int:
        """The chosen trace id inside :func:`chosen_trace_id`, else a random one."""
        chosen = _CHOSEN_TRACE_ID.get()
        return chosen if chosen is not None else super().generate_trace_id()

    def is_trace_id_random(self) -> bool:
        """A chosen trace id is not random."""
        return _CHOSEN_TRACE_ID.get() is None


@contextmanager
def chosen_trace_id(trace_id: int) -> Iterator[None]:
    """Make the next parent-less span started in the block a root of ``trace_id``."""
    token = _CHOSEN_TRACE_ID.set(trace_id)
    try:
        yield
    finally:
        _CHOSEN_TRACE_ID.reset(token)
