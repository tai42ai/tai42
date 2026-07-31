"""Every private-Langfuse-SDK touchpoint, concentrated in one module.

These functions reach into ``langfuse`` internals (underscore-prefixed modules,
attributes, methods) for capabilities with no public API. They are verified
against the pinned ``langfuse`` version and may break on any SDK upgrade —
re-verify every symbol imported or accessed below when bumping the pin. Nothing
outside this module touches a private SDK member.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any

from langfuse import Langfuse, LangfuseOtelSpanAttributes
from langfuse._client.get_client import _current_public_key, _set_current_public_key
from langfuse._client.resource_manager import LangfuseResourceManager
from opentelemetry import trace as otel_trace


def current_scoped_public_key() -> str | None:
    """The public key bound by the innermost active ``bind_public_key`` block,
    or ``None`` outside any scope."""
    return _current_public_key.get(None)


def bind_public_key(public_key: str) -> AbstractContextManager[None]:
    """Bind the SDK's ambient project to ``public_key`` for the block, so both
    this plugin's client resolution and the SDK's own ``get_client()`` see it."""
    return _set_current_public_key(public_key)


def evict_all_clients() -> None:
    """Evict every cached SDK client (all projects) so the next use rebuilds clean.

    ``LangfuseResourceManager.reset()`` leaves each instance's pooled
    ``httpx.Client`` open, so each pool is closed here — after ``reset()``'s
    final flush has sent over it — rather than stranding its live SSL connections.
    """
    with LangfuseResourceManager._lock:
        instances = list(LangfuseResourceManager._instances.values())
        LangfuseResourceManager.reset()
        for instance in instances:
            instance.httpx_client.close()


def _to_ns(ts: datetime) -> int:
    """A ``datetime`` as integer nanoseconds since the epoch (OTel time base)."""
    return int(ts.timestamp() * 1_000_000_000)


def emit_closed_span(
    client: Langfuse,
    *,
    name: str,
    as_type: str,
    start: datetime,
    end: datetime,
    trace_id: str,
    parent_span_id: str | None,
    input: Any = None,
    output: Any = None,
    level: str | None = None,
    status_message: str | None = None,
    model: str | None = None,
    usage_details: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit one already-closed observation with explicit start/end times into
    the trace ``trace_id`` (nested under ``parent_span_id`` when given)."""
    remote_parent = client._create_remote_parent_span(trace_id=trace_id, parent_span_id=parent_span_id)
    with otel_trace.use_span(remote_parent):
        otel_span = client._otel_tracer.start_span(name=name, start_time=_to_ns(start))
        otel_span.set_attribute(LangfuseOtelSpanAttributes.AS_ROOT, True)
        obs = client._create_observation_from_otel_span(
            otel_span=otel_span,
            as_type=as_type,  # type: ignore[arg-type]  # narrowed by the writer's SpanKind map
            input=input,
            output=output,
            metadata=metadata,
            level=level,  # type: ignore[arg-type]  # MonitoringLevel values match the SDK literal
            status_message=status_message,
            model=model,
            usage_details=usage_details,
        )
        obs.end(end_time=_to_ns(end))


def set_trace_attributes(obs: Any, *, name: str | None = None, tags: list[str] | None = None) -> None:
    """Set the enclosing trace's name/tags via OTel attributes on ``obs``'s
    underlying span (``obs`` is the SDK observation behind a ``Span`` handle)."""
    if name is not None:
        obs._otel_span.set_attribute(LangfuseOtelSpanAttributes.TRACE_NAME, name)
    if tags is not None:
        obs._otel_span.set_attribute(LangfuseOtelSpanAttributes.TRACE_TAGS, tags)
