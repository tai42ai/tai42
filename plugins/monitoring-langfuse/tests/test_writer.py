"""Writer emit -> SDK-call mapping, fail-safe behavior, and scoping."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from tai42_contract.monitoring import MonitoringLevel, SpanKind, TraceContext

from tai42_monitoring_langfuse.sdk_internals import current_scoped_public_key
from tai42_monitoring_langfuse.writer import LangfuseWriter, _level_str


def _enter_obs(mock_client):
    """The observation yielded by the mocked start_as_current_observation CM."""
    obs = MagicMock()
    mock_client.start_as_current_observation.return_value.__enter__.return_value = obs
    return obs


def test_start_span_emits_observation_and_exits_cm(manager, mock_client):
    obs = _enter_obs(mock_client)
    writer = LangfuseWriter(manager)

    with writer.start_span(name="node", kind=SpanKind.TOOL, input={"a": 1}) as span:
        span.update(output="result", usage_details={"input": 5})

    kwargs = mock_client.start_as_current_observation.call_args.kwargs
    assert kwargs["name"] == "node"
    assert kwargs["as_type"] == "tool"
    assert kwargs["input"] == {"a": 1}
    obs.update.assert_called_once_with(output="result", usage_details={"input": 5})
    # The SDK context manager ends + detaches the observation on exit.
    mock_client.start_as_current_observation.return_value.__exit__.assert_called_once_with(None, None, None)


def test_start_span_passes_trace_context_and_generation_detail(manager, mock_client):
    _enter_obs(mock_client)
    writer = LangfuseWriter(manager)

    ctx = TraceContext(trace_id="t1", parent_span_id="p1")
    with writer.start_span(
        name="x",
        kind=SpanKind.LLM,
        trace_context=ctx,
        model="gpt",
        model_parameters={"temperature": 0.1},
        metadata={"m": 1},
    ):
        pass

    kwargs = mock_client.start_as_current_observation.call_args.kwargs
    assert kwargs["trace_context"] == {"trace_id": "t1", "parent_span_id": "p1"}
    assert kwargs["as_type"] == "generation"
    assert kwargs["model"] == "gpt"
    assert kwargs["model_parameters"] == {"temperature": 0.1}
    assert kwargs["metadata"] == {"m": 1}


def test_span_update_maps_every_field(manager, mock_client):
    obs = _enter_obs(mock_client)
    writer = LangfuseWriter(manager)

    with writer.start_span(name="x", kind=SpanKind.TOOL) as span:
        span.update(
            output="o",
            model="gpt",
            usage_details={"input": 1},
            metadata={"m": 1},
            level=MonitoringLevel.WARNING,
            status_message="msg",
        )

    obs.update.assert_called_once_with(
        output="o",
        model="gpt",
        usage_details={"input": 1},
        metadata={"m": 1},
        level="WARNING",
        status_message="msg",
    )


def test_span_update_without_fields_is_noop(manager, mock_client):
    obs = _enter_obs(mock_client)
    with LangfuseWriter(manager).start_span(name="x", kind=SpanKind.TOOL) as span:
        span.update()
    obs.update.assert_not_called()


def test_start_span_body_exception_still_exits_cm(manager, mock_client):
    _enter_obs(mock_client)
    writer = LangfuseWriter(manager)

    def _run_body_that_raises():
        with writer.start_span(name="x", kind=SpanKind.TOOL):
            raise RuntimeError("body boom")

    with pytest.raises(RuntimeError, match="body boom"):
        _run_body_that_raises()
    mock_client.start_as_current_observation.return_value.__exit__.assert_called_once_with(None, None, None)


def test_start_span_cleanup_failure_is_swallowed(manager, mock_client):
    _enter_obs(mock_client)
    mock_client.start_as_current_observation.return_value.__exit__.side_effect = RuntimeError("exit boom")
    writer = LangfuseWriter(manager)

    with writer.start_span(name="x", kind=SpanKind.TOOL) as span:
        span.update(output="o")  # body ran with a real span; cleanup failure stays internal


def test_record_span_maps_to_explicit_time_emission(manager, mock_client, monkeypatch):
    captured = {}

    def _emit(client, **kwargs):
        captured["client"] = client
        captured.update(kwargs)

    monkeypatch.setattr("tai42_monitoring_langfuse.writer.emit_closed_span", _emit)
    writer = LangfuseWriter(manager)

    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC)
    writer.record_span(
        name="tool",
        kind=SpanKind.TOOL,
        start=t0,
        end=t1,
        trace_context=TraceContext(trace_id="t1", parent_span_id="p1"),
        input={"a": 1},
        output="r",
        level=MonitoringLevel.WARNING,
        usage_details={"input": 3},
    )

    assert captured["client"] is mock_client
    assert captured["name"] == "tool"
    assert captured["as_type"] == "tool"
    assert captured["start"] == t0
    assert captured["end"] == t1
    assert captured["trace_id"] == "t1"
    assert captured["parent_span_id"] == "p1"
    assert captured["input"] == {"a": 1}
    assert captured["output"] == "r"
    assert captured["level"] == "WARNING"
    assert captured["usage_details"] == {"input": 3}


def test_record_span_requires_trace_id(manager, mock_client):
    writer = LangfuseWriter(manager)
    with pytest.raises(ValueError, match=r"trace_context\.trace_id"):
        writer.record_span(
            name="tool",
            kind=SpanKind.TOOL,
            start=datetime.now(UTC),
            end=datetime.now(UTC),
            trace_context=TraceContext(parent_span_id="p1"),
        )
    mock_client.start_as_current_observation.assert_not_called()


def test_record_span_is_fail_safe(manager, monkeypatch):
    monkeypatch.setattr(manager, "active_client", MagicMock(side_effect=RuntimeError("boom")))
    writer = LangfuseWriter(manager)
    # A backend failure is caught + logged, not surfaced (trace_id is present).
    writer.record_span(
        name="tool",
        kind=SpanKind.TOOL,
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        trace_context=TraceContext(trace_id="t1"),
    )


def test_create_event_and_update_current_span(manager, mock_client):
    writer = LangfuseWriter(manager)
    writer.create_event(name="evt", input={"i": 1}, output={"o": 2})
    writer.update_current_span(level=MonitoringLevel.ERROR, status_message="bad")

    assert mock_client.create_event.call_args.kwargs["level"] == "DEFAULT"
    assert mock_client.create_event.call_args.kwargs["name"] == "evt"
    assert mock_client.create_event.call_args.kwargs["input"] == {"i": 1}
    assert mock_client.create_event.call_args.kwargs["output"] == {"o": 2}
    assert mock_client.update_current_span.call_args.kwargs == {
        "level": "ERROR",
        "status_message": "bad",
    }


def test_create_event_passes_trace_context_and_status(manager, mock_client):
    writer = LangfuseWriter(manager)
    writer.create_event(
        name="evt",
        trace_context=TraceContext(trace_id="t9"),
        status_message="note",
        metadata={"k": "v"},
    )
    kwargs = mock_client.create_event.call_args.kwargs
    assert kwargs["trace_context"] == {"trace_id": "t9"}
    assert kwargs["status_message"] == "note"
    assert kwargs["metadata"] == {"k": "v"}


def test_update_current_span_no_kwargs_is_noop(manager, mock_client):
    LangfuseWriter(manager).update_current_span()
    mock_client.update_current_span.assert_not_called()


def test_update_current_span_maps_metadata_and_output(manager, mock_client):
    LangfuseWriter(manager).update_current_span(metadata={"m": 1}, output={"o": 2})
    assert mock_client.update_current_span.call_args.kwargs == {"metadata": {"m": 1}, "output": {"o": 2}}


def test_trace_attributes_propagates_and_restores(manager, monkeypatch):
    cm = MagicMock()
    factory = MagicMock(return_value=cm)
    monkeypatch.setattr("tai42_monitoring_langfuse.writer.propagate_attributes", factory)
    writer = LangfuseWriter(manager)

    with writer.trace_attributes(name="run", tags=["run:7"], metadata={"k": "v"}):
        pass

    factory.assert_called_once_with(trace_name="run", tags=["run:7"], metadata={"k": "v"})
    cm.__enter__.assert_called_once()
    cm.__exit__.assert_called_once_with(None, None, None)


def test_trace_attributes_setup_and_teardown_fail_safe(manager, monkeypatch):
    # Setup failure: the body still runs, nothing raises.
    monkeypatch.setattr(
        "tai42_monitoring_langfuse.writer.propagate_attributes", MagicMock(side_effect=RuntimeError("boom"))
    )
    ran = False
    with LangfuseWriter(manager).trace_attributes(name="x"):
        ran = True
    assert ran

    # Teardown failure: caught + logged.
    cm = MagicMock()
    cm.__exit__.side_effect = RuntimeError("exit boom")
    monkeypatch.setattr("tai42_monitoring_langfuse.writer.propagate_attributes", MagicMock(return_value=cm))
    with LangfuseWriter(manager).trace_attributes(name="x"):
        pass


def test_current_trace_id_passthrough(manager, mock_client):
    mock_client.get_current_trace_id.return_value = "trace-123"
    assert LangfuseWriter(manager).current_trace_id() == "trace-123"


def test_disable_suppresses_emission(manager, mock_client):
    writer = LangfuseWriter(manager)
    with writer.disable():
        with writer.start_span(name="x", kind=SpanKind.TOOL) as span:
            span.update(output="o")  # no-op span, must not raise
            assert span.id == ""
            span.set_trace_metadata(name="n", tags=["t"])  # no-op, must not raise
        writer.create_event(name="e")
        writer.update_current_span(metadata={"k": "v"})
        writer.record_span(
            name="r",
            kind=SpanKind.TOOL,
            start=datetime.now(UTC),
            end=datetime.now(UTC),
            trace_context=TraceContext(trace_id="t"),
        )
        with writer.trace_attributes(name="n"):
            pass
    mock_client.start_as_current_observation.assert_not_called()
    mock_client.create_event.assert_not_called()
    mock_client.update_current_span.assert_not_called()


def test_emit_is_fail_safe(manager, monkeypatch):
    # active_client raising must never surface to the caller.
    monkeypatch.setattr(manager, "active_client", MagicMock(side_effect=RuntimeError("boom")))
    writer = LangfuseWriter(manager)

    with writer.start_span(name="x", kind=SpanKind.TOOL) as span:
        span.update(output="o")
    writer.create_event(name="e")
    writer.update_current_span(level=MonitoringLevel.ERROR)
    assert writer.current_trace_id() is None  # returns None, never raises


def test_span_handle_is_fail_safe(manager, mock_client):
    obs = _enter_obs(mock_client)
    obs.update.side_effect = RuntimeError("update boom")
    del obs._otel_span  # set_trace_metadata's private access fails -> caught + logged
    writer = LangfuseWriter(manager)

    with writer.start_span(name="x", kind=SpanKind.TOOL) as span:
        span.update(output="o")
        span.set_trace_metadata(name="n", tags=["t"])


def test_span_handle_id_and_trace_metadata(manager, mock_client):
    from langfuse import LangfuseOtelSpanAttributes

    obs = _enter_obs(mock_client)
    obs.id = "span-1"
    writer = LangfuseWriter(manager)

    with writer.start_span(name="x", kind=SpanKind.TOOL) as span:
        assert span.id == "span-1"
        span.set_trace_metadata(name="run-name", tags=["run:7"])

    obs._otel_span.set_attribute.assert_any_call(LangfuseOtelSpanAttributes.TRACE_NAME, "run-name")
    obs._otel_span.set_attribute.assert_any_call(LangfuseOtelSpanAttributes.TRACE_TAGS, ["run:7"])


def test_level_str_accepts_plain_string():
    assert _level_str(None) is None
    assert _level_str(MonitoringLevel.ERROR) == "ERROR"
    assert _level_str("WARNING") == "WARNING"  # type: ignore[arg-type]


def test_inject_context_blob(manager):
    writer = LangfuseWriter(manager)
    blob = writer.inject_context(TraceContext(trace_id="t", parent_span_id="p", tags=["run:7"], metadata={"k": "v"}))
    assert blob["monitoring_provider"] == "langfuse"
    assert blob["configurable"] == {"monitoring_trace_id": "t", "monitoring_parent_span_id": "p"}
    assert blob["metadata"]["langfuse_tags"] == ["run:7"]
    assert blob["metadata"]["k"] == "v"


def test_inject_context_empty(manager):
    blob = LangfuseWriter(manager).inject_context(TraceContext())
    assert blob == {"monitoring_provider": "langfuse"}


def test_get_monitoring_callbacks_binds_public_key(manager, monkeypatch):
    fake_handler = MagicMock()
    captured = {}

    def _factory(*, public_key=None, trace_context=None):
        captured["public_key"] = public_key
        captured["trace_context"] = trace_context
        return fake_handler

    monkeypatch.setattr("tai42_monitoring_langfuse.writer.CallbackHandler", _factory)
    writer = LangfuseWriter(manager)

    callbacks = writer.get_monitoring_callbacks(TraceContext(trace_id="t"))
    assert callbacks == [fake_handler]
    assert captured["public_key"] == "pk-test"
    assert captured["trace_context"] == {"trace_id": "t"}
    assert fake_handler.run_inline is True


def test_get_monitoring_callbacks_empty_when_disabled(manager):
    writer = LangfuseWriter(manager)
    with writer.disable():
        assert writer.get_monitoring_callbacks(TraceContext(trace_id="t")) == []


def test_scope_rejects_unconfigured_project(manager):
    writer = LangfuseWriter(manager)
    with pytest.raises(ValueError, match="pk-not-configured"), writer.scope("pk-not-configured"):
        pass


def test_scope_binds_and_restores_active_project(manager):
    writer = LangfuseWriter(manager)
    assert current_scoped_public_key() is None
    assert manager.resolve_public_key() == "pk-test"  # default outside any scope

    with writer.scope("pk-test"):
        assert current_scoped_public_key() == "pk-test"
        assert manager.resolve_public_key() == "pk-test"

    # restored on exit
    assert current_scoped_public_key() is None


def test_scope_restores_on_exception(manager):
    writer = LangfuseWriter(manager)

    def _raise_inside_scope():
        with writer.scope("pk-test"):
            assert current_scoped_public_key() == "pk-test"
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _raise_inside_scope()
    assert current_scoped_public_key() is None


def test_flush_and_shutdown_delegate(manager, monkeypatch):
    flush = MagicMock()
    shutdown = MagicMock()
    monkeypatch.setattr(manager, "flush", flush)
    monkeypatch.setattr(manager, "shutdown", shutdown)
    writer = LangfuseWriter(manager)
    writer.flush()
    writer.shutdown()
    flush.assert_called_once_with()
    shutdown.assert_called_once_with()
