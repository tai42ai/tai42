"""Unit tests for ``bind_run_trace`` (``tai42_kit.llm.run_trace``).

``bind_run_trace`` returns the per-invoke ``RunnableConfig`` with the kit's monitoring callback handler bound to
the given context appended after the caller's, when a context is given and the active writer records; nothing is
appended otherwise. Exercised against a fake app bound to ``tai42_app``.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from opentelemetry import context as otel_context
from tai42_contract.app import tai42_app
from tai42_contract.monitoring import TraceContext

from tai42_kit.llm.monitoring_callbacks import MonitoringCallbackHandler
from tai42_kit.llm.run_trace import bind_run_trace

T1 = "1" * 32
PARENT = "c" * 16
CONTEXT = TraceContext(trace_id=T1, parent_span_id=PARENT)


class _RecordingWriter:
    def __init__(self) -> None:
        self.recording = True
        self.opened: list[str] = []

    def open_span(self, *, name: str, **_: Any) -> Any:
        self.opened.append(name)
        raise AssertionError("bind_run_trace opens no span")

    def is_recording(self) -> bool:
        return self.recording


class _Monitoring:
    def __init__(self, writer: _RecordingWriter) -> None:
        self.writer = writer


class _MonitoringFacet:
    def __init__(self, writer: _RecordingWriter) -> None:
        self._backend = _Monitoring(writer)

    @property
    def active(self) -> _Monitoring:
        return self._backend


class _FakeApp:
    def __init__(self, writer: _RecordingWriter) -> None:
        self.monitoring = _MonitoringFacet(writer)


@pytest.fixture
def writer() -> Any:
    recording = _RecordingWriter()
    with tai42_app.bound(_FakeApp(recording)):
        yield recording


def _handlers(config: dict[str, Any]) -> list[MonitoringCallbackHandler]:
    return [cb for cb in config["callbacks"] if isinstance(cb, MonitoringCallbackHandler)]


def test_bind_returns_the_config_with_the_handler_bound_to_the_context(writer: _RecordingWriter) -> None:
    config = bind_run_trace({"configurable": {"thread_id": "t"}}, CONTEXT)
    assert config["configurable"] == {"thread_id": "t"}
    (handler,) = _handlers(config)
    assert handler.trace_context == CONTEXT
    assert handler.writer is writer
    assert handler.grouping_nodes == frozenset()
    assert handler.chain_payloads == "references"


def test_bind_with_no_context_appends_no_handler(writer: _RecordingWriter) -> None:
    existing = BaseCallbackHandler()
    config = bind_run_trace({"callbacks": [existing]}, None)
    assert config["callbacks"] == [existing]


def test_bind_passes_the_recording_options(writer: _RecordingWriter) -> None:
    config = bind_run_trace(None, CONTEXT, grouping_nodes=frozenset({"model"}), chain_payloads="producer")
    (handler,) = _handlers(config)
    assert handler.grouping_nodes == frozenset({"model"})
    assert handler.chain_payloads == "producer"


def test_bind_binds_no_handler_when_the_writer_records_nothing(writer: _RecordingWriter) -> None:
    writer.recording = False
    existing = BaseCallbackHandler()
    config = bind_run_trace({"callbacks": [existing]}, CONTEXT)
    assert config["callbacks"] == [existing]


def test_bind_appends_callbacks_after_the_callers(writer: _RecordingWriter) -> None:
    existing = BaseCallbackHandler()
    config = bind_run_trace({"callbacks": [existing]}, CONTEXT)
    assert config["callbacks"][0] is existing
    assert len(config["callbacks"]) == 2


def test_bind_does_not_mutate_the_input(writer: _RecordingWriter) -> None:
    existing = {"configurable": {"thread_id": "t"}, "callbacks": ["cb"]}
    bind_run_trace(existing, CONTEXT)
    assert existing == {"configurable": {"thread_id": "t"}, "callbacks": ["cb"]}


def test_bind_yields_independent_copies(writer: _RecordingWriter) -> None:
    shared = {"configurable": {}, "callbacks": ["cb"]}
    first = bind_run_trace(shared, CONTEXT)
    second = bind_run_trace(shared, CONTEXT)
    assert first is not second
    assert first["configurable"] is not second["configurable"]
    assert first["callbacks"] is not second["callbacks"]
    assert _handlers(first)[0] is not _handlers(second)[0]
    assert shared == {"configurable": {}, "callbacks": ["cb"]}


def test_bind_opens_no_span(writer: _RecordingWriter) -> None:
    key = otel_context.create_key("run-trace-probe")
    token = otel_context.attach(otel_context.set_value(key, "caller-attribution"))
    try:
        bind_run_trace(None, CONTEXT)
        assert otel_context.get_value(key) == "caller-attribution"
        assert writer.opened == []
    finally:
        otel_context.detach(token)
