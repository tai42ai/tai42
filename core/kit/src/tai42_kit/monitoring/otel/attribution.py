"""Trace-level attribution: frames pushed by ``trace_attributes`` and stamped on every span started in scope."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from opentelemetry.context import Context
from opentelemetry.sdk.trace import Span as SdkSpan
from opentelemetry.sdk.trace import SpanProcessor
from tai42_contract.monitoring import RUN_VERSION_METADATA_KEY

from tai42_kit.monitoring.otel import attributes as attr


@dataclass(frozen=True, slots=True)
class AttributionFrame:
    """One ``trace_attributes`` block's values."""

    name: str | None
    tags: tuple[str, ...]
    metadata: Mapping[str, Any]
    user_id: str | None
    session_id: str | None


_ATTRIBUTION: ContextVar[tuple[AttributionFrame, ...]] = ContextVar("tai42_otel_attribution", default=())


@contextmanager
def pushed_frame(frame: AttributionFrame) -> Iterator[None]:
    """Push ``frame`` for the block."""
    token = _ATTRIBUTION.set((*_ATTRIBUTION.get(), frame))
    try:
        yield
    finally:
        _ATTRIBUTION.reset(token)


# Encodes one value of the named record into an attribute string; never raises.
FieldEncoder = Callable[[Any, str], str]


def merged_attributes(encode: FieldEncoder, record_name: str) -> dict[str, str]:
    """The span attributes of the frames in scope, merged; empty outside every frame.

    Tags are the ordered union, metadata a dict merge (inner wins), name / user / session the
    innermost non-``None``, and the run version the OUTERMOST frame's
    ``RUN_VERSION_METADATA_KEY`` value (lifted out of the trace metadata).
    """
    frames = _ATTRIBUTION.get()
    if not frames:
        return {}
    tags: dict[str, None] = {}
    metadata: dict[str, Any] = {}
    name = user_id = session_id = version = None
    for frame in frames:
        tags.update(dict.fromkeys(frame.tags))
        metadata.update(frame.metadata)
        name = frame.name if frame.name is not None else name
        user_id = frame.user_id if frame.user_id is not None else user_id
        session_id = frame.session_id if frame.session_id is not None else session_id
        if version is None and frame.metadata.get(RUN_VERSION_METADATA_KEY) is not None:
            version = str(frame.metadata[RUN_VERSION_METADATA_KEY])
    metadata.pop(RUN_VERSION_METADATA_KEY, None)
    out: dict[str, str] = {}
    if name is not None:
        out[attr.TRACE_NAME] = name
    if tags:
        out[attr.TRACE_TAGS] = encode(list(tags), record_name)
    if metadata:
        out[attr.TRACE_METADATA] = encode(metadata, record_name)
    if user_id is not None:
        out[attr.USER_ID] = user_id
    if session_id is not None:
        out[attr.SESSION_ID] = session_id
    if version is not None:
        out[attr.RUN_VERSION] = version
    return out


class AttributionSpanProcessor(SpanProcessor):
    """Stamps the merged attribution frames on every span at its start (on the starting thread)."""

    def __init__(self, encode: FieldEncoder) -> None:
        """Encode JSON-valued attributes with ``encode``."""
        self._encode = encode

    def on_start(self, span: SdkSpan, parent_context: Context | None = None) -> None:
        """Set the merged attribution attributes on ``span``."""
        stamped = merged_attributes(self._encode, span.name)
        if stamped:
            span.set_attributes(stamped)
