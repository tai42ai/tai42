"""The private-SDK touchpoint module: explicit-time emission and eviction."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime
from unittest.mock import MagicMock

from langfuse import LangfuseOtelSpanAttributes

from tai42_monitoring_langfuse.sdk_internals import (
    LangfuseResourceManager,
    _to_ns,
    bind_public_key,
    current_scoped_public_key,
    emit_closed_span,
    evict_all_clients,
    set_trace_attributes,
)


def test_emit_closed_span_drives_otel_tracer(monkeypatch):
    monkeypatch.setattr("tai42_monitoring_langfuse.sdk_internals.otel_trace.use_span", lambda span: nullcontext())
    client = MagicMock()
    obs = MagicMock()
    client._create_observation_from_otel_span.return_value = obs

    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC)
    emit_closed_span(
        client,
        name="tool",
        as_type="tool",
        start=t0,
        end=t1,
        trace_id="t1",
        parent_span_id="p1",
        input={"a": 1},
        output="r",
    )

    client._create_remote_parent_span.assert_called_once_with(trace_id="t1", parent_span_id="p1")
    assert client._otel_tracer.start_span.call_args.kwargs["start_time"] == _to_ns(t0)
    otel_span = client._otel_tracer.start_span.return_value
    otel_span.set_attribute.assert_called_once_with(LangfuseOtelSpanAttributes.AS_ROOT, True)
    obs_kwargs = client._create_observation_from_otel_span.call_args.kwargs
    assert obs_kwargs["as_type"] == "tool"
    assert obs_kwargs["input"] == {"a": 1}
    assert obs_kwargs["output"] == "r"
    obs.end.assert_called_once_with(end_time=_to_ns(t1))


def test_to_ns_is_epoch_nanoseconds():
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    assert _to_ns(ts) == int(ts.timestamp()) * 1_000_000_000


def test_set_trace_attributes_writes_only_given_fields():
    obs = MagicMock()
    set_trace_attributes(obs, name="n")
    obs._otel_span.set_attribute.assert_called_once_with(LangfuseOtelSpanAttributes.TRACE_NAME, "n")

    obs = MagicMock()
    set_trace_attributes(obs, tags=["a"])
    obs._otel_span.set_attribute.assert_called_once_with(LangfuseOtelSpanAttributes.TRACE_TAGS, ["a"])


def test_bind_public_key_round_trip():
    assert current_scoped_public_key() is None
    with bind_public_key("pk-x"):
        assert current_scoped_public_key() == "pk-x"
    assert current_scoped_public_key() is None


def test_evict_all_clients_resets_then_closes_transport_pools(monkeypatch):
    calls: list[str] = []
    instance = MagicMock()
    instance.httpx_client.close.side_effect = lambda: calls.append("close")
    monkeypatch.setattr(LangfuseResourceManager, "_instances", {"pk-a": instance})
    monkeypatch.setattr(LangfuseResourceManager, "reset", classmethod(lambda cls: calls.append("reset")))

    evict_all_clients()

    # The pool close must come after reset: the final flush inside reset still
    # sends over the pooled transport.
    assert calls == ["reset", "close"]
