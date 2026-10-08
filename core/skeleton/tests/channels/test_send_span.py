"""Tier 1 of the send-outcome monitoring layer: the ``send_span`` helper.

Covers the span SHAPE on success (name/kind/metadata/input), the ERROR marking with
the typed ``ChannelDeliveryError`` / ``ChannelInputError`` detail on failure, the
per-attempt retry ordinal, and the gated no-op when no trace is ambient.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import orjson
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tai42_contract.channels import ChannelDeliveryError, ChannelInputError
from tai42_contract.monitoring import MonitoringLevel, SpanKind
from tai42_contract.secrets import SecretValue
from tai42_kit.monitoring.otel import OtelWriter

from tai42_skeleton.channels.send_span import send_span
from tai42_skeleton.monitoring import NoOpMonitoring, init_monitoring, reset_monitoring

from .._fakes.recording_monitoring import RecordingMonitoring


@pytest.fixture
def backend() -> Iterator[RecordingMonitoring]:
    reset_monitoring()
    backend = RecordingMonitoring()
    init_monitoring(backend)
    yield backend
    reset_monitoring()


def test_no_trace_is_a_no_op_yielding_none(backend: RecordingMonitoring) -> None:
    # Outside a trace the helper opens no span (a rootless send span would attach to no
    # run) and yields None so the caller runs the send unwrapped.
    backend.writer.active_trace_id = None

    with send_span("sms", recipient="+15550001111") as span:
        assert span is None

    assert backend.writer.spans == []


def test_success_span_shape(backend: RecordingMonitoring) -> None:
    backend.writer.active_trace_id = "trace-1"

    with send_span("sms", recipient="+15550001111") as span:
        assert span is not None
        span.update(output={"messaging.message.id": ["m1"]})

    (recorded,) = backend.writer.spans
    assert recorded["name"] == "send:sms"
    assert recorded["kind"] is SpanKind.TOOL
    # The recipient rides the INPUT path, never the metadata.
    assert recorded["input"] == {"recipient": "+15550001111"}
    assert recorded["metadata"] == {"messaging.system": "sms", "messaging.operation": "send"}
    # The caller's success output is the accepted provider ids; no ERROR level.
    assert recorded["span"].updates == [
        {"output": {"messaging.message.id": ["m1"]}, "metadata": None, "level": None, "status_message": None}
    ]


def test_delivery_error_marks_error_with_typed_metadata(backend: RecordingMonitoring) -> None:
    backend.writer.active_trace_id = "trace-1"

    with pytest.raises(ChannelDeliveryError), send_span("sms", recipient="x"):  # re-raised unchanged
        raise ChannelDeliveryError("provider 503", retryable=True, retry_after=5.0)

    (recorded,) = backend.writer.spans
    (update,) = recorded["span"].updates
    assert update["level"] is MonitoringLevel.ERROR
    assert update["status_message"] == "provider 503"
    assert update["metadata"] == {
        "error.type": "ChannelDeliveryError",
        "error.kind": "delivery_failed",
        "retryable": True,
        "retry_after": 5.0,
    }


def test_non_retryable_delivery_error_omits_retry_after(backend: RecordingMonitoring) -> None:
    backend.writer.active_trace_id = "trace-1"

    with pytest.raises(ChannelDeliveryError), send_span("sms", recipient="x"):
        raise ChannelDeliveryError("bad recipient")  # retryable defaults False, no retry_after

    (update,) = backend.writer.spans[0]["span"].updates
    assert update["metadata"] == {
        "error.type": "ChannelDeliveryError",
        "error.kind": "delivery_failed",
        "retryable": False,
    }


def test_input_error_is_never_retryable(backend: RecordingMonitoring) -> None:
    backend.writer.active_trace_id = "trace-1"

    with pytest.raises(ChannelInputError), send_span("sms", recipient="x"):
        raise ChannelInputError("unrenderable")

    (update,) = backend.writer.spans[0]["span"].updates
    assert update["level"] is MonitoringLevel.ERROR
    assert update["metadata"] == {
        "error.type": "ChannelInputError",
        "error.kind": "bad_input",
        "retryable": False,
    }


def test_attempt_ordinal_is_stamped(backend: RecordingMonitoring) -> None:
    backend.writer.active_trace_id = "trace-1"

    with send_span("sms", recipient="x", attempt=2, retry_decision=lambda exc: False):
        pass

    # The retry ordinal is a platform-local attribute OUTSIDE the messaging.* namespace
    # (OTel messaging semconv has no retry-attempt attribute).
    assert backend.writer.spans[0]["metadata"]["retry.attempt"] == 2


def test_a_wrapped_recipient_reaches_the_writer_raw(backend: RecordingMonitoring) -> None:
    backend.writer.active_trace_id = "trace-1"
    secret = SecretValue("+15550001111")

    with send_span("sms", recipient=cast(Any, secret)):
        pass

    (recorded,) = backend.writer.spans
    assert recorded["input"]["recipient"] is secret


def test_the_writer_records_a_wrapped_recipient_as_the_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", "true")
    exporter = InMemorySpanExporter()
    writer = OtelWriter(exporter=exporter)

    class _Otel(NoOpMonitoring):
        @property
        def writer(self) -> OtelWriter:  # type: ignore[override]
            return writer

    reset_monitoring()
    init_monitoring(_Otel())
    try:
        with (
            writer.start_span(name="run", kind=SpanKind.CHAIN),
            send_span("sms", recipient=cast(Any, SecretValue("+1555"))),
        ):
            pass
        writer.flush()
        send = next(s for s in exporter.get_finished_spans() if s.name == "send:sms")
        arguments = dict(send.attributes or {})["gen_ai.tool.call.arguments"]
        assert orjson.loads(str(arguments)) == {"recipient": "[secret]"}
        assert "+1555" not in str(arguments)
    finally:
        reset_monitoring()
        writer.shutdown()


# -- a retrying seam: the span's retryable is the seam's decision ------------------


def test_a_single_send_keeps_the_error_classification(backend: RecordingMonitoring) -> None:
    backend.writer.active_trace_id = "trace-1"

    with pytest.raises(ChannelDeliveryError), send_span("sms", recipient="x"):
        raise ChannelDeliveryError("provider 503", retryable=True)

    (update,) = backend.writer.spans[0]["span"].updates
    assert update["metadata"]["retryable"] is True


def test_an_unstamped_exception_carries_the_unknown_kind(backend: RecordingMonitoring) -> None:
    backend.writer.active_trace_id = "trace-1"

    with pytest.raises(RuntimeError), send_span("sms", recipient="x"):
        raise RuntimeError("socket gone")

    (update,) = backend.writer.spans[0]["span"].updates
    assert update["metadata"] == {"error.type": "RuntimeError", "error.kind": "unknown"}


def test_a_retrying_seam_stamps_its_decision(backend: RecordingMonitoring) -> None:
    backend.writer.active_trace_id = "trace-1"
    decided: list[BaseException] = []

    def no_more_attempts(exc: BaseException) -> bool:
        decided.append(exc)
        return False

    error = ChannelDeliveryError("provider 503", retryable=True, retry_after=5.0)
    with (
        pytest.raises(ChannelDeliveryError),
        send_span("sms", recipient="x", attempt=2, retry_decision=no_more_attempts),
    ):
        raise error

    (update,) = backend.writer.spans[0]["span"].updates
    assert update["metadata"] == {
        "error.type": "ChannelDeliveryError",
        "error.kind": "delivery_failed",
        "retryable": False,
    }
    assert decided == [error]


def test_the_decision_is_made_once_even_outside_a_trace(backend: RecordingMonitoring) -> None:
    backend.writer.active_trace_id = None
    decided: list[BaseException] = []

    def decide(exc: BaseException) -> bool:
        decided.append(exc)
        return True

    with pytest.raises(ChannelDeliveryError), send_span("sms", recipient="x", attempt=1, retry_decision=decide):
        raise ChannelDeliveryError("provider 503", retryable=True)

    assert len(decided) == 1
    assert backend.writer.spans == []


@pytest.mark.parametrize(
    "kwargs",
    [{"attempt": 1}, {"retry_decision": lambda exc: True}],
)
def test_attempt_and_retry_decision_are_given_together(backend: RecordingMonitoring, kwargs: dict[str, Any]) -> None:
    backend.writer.active_trace_id = "trace-1"

    with (
        pytest.raises(ValueError, match="send_span: attempt and retry_decision are given together"),
        send_span("sms", recipient="x", **kwargs),
    ):
        pass
