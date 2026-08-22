"""The generic run-attribution enabler: the ambient deposit + the wrap helper.

``stamp_run_attribution`` ENTERS ``attribute_run(writer, attribution)`` AROUND the drive
when an ambient :class:`RunAttribution` is deposited — the drive runs INSIDE the scope
(entered before, exited after), never a one-shot fired beside it — and no-ops when none is
deposited. It is fail-safe: a writer with no active trace never raises.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from tai42_contract.monitoring import RunAttribution

import tai42_skeleton.monitoring as monitoring_mod
from tai42_skeleton.tools.attribution import (
    get_run_attribution,
    run_attribution,
    stamp_run_attribution,
)


class _SpyWriter:
    """Records the enter/exit of the attribute_run scope relative to the drive."""

    def __init__(self, *, trace_id: str | None) -> None:
        self._trace_id = trace_id
        self.events: list[str] = []

    def current_trace_id(self) -> str | None:
        return self._trace_id

    @contextmanager
    def trace_attributes(self, *, name, tags, metadata):
        self.events.append("enter")
        try:
            yield
        finally:
            self.events.append("exit")


class _SpyMonitoring:
    def __init__(self, writer: _SpyWriter) -> None:
        self.writer = writer


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch):
    def _install(*, trace_id: str | None) -> _SpyWriter:
        writer = _SpyWriter(trace_id=trace_id)
        monkeypatch.setattr(monitoring_mod, "get_monitoring", lambda: _SpyMonitoring(writer))
        return writer

    return _install


def test_ambient_attribution_wraps_the_drive(spy) -> None:
    writer = spy(trace_id="trace-1")
    with run_attribution(RunAttribution(tags=["t"], metadata={"k": "v"})), stamp_run_attribution():
        writer.events.append("drive")
    # Entered BEFORE and exited AFTER the drive — the drive runs inside the scope.
    assert writer.events == ["enter", "drive", "exit"]


def test_no_ambient_attribution_no_wrap(spy) -> None:
    writer = spy(trace_id="trace-1")
    with stamp_run_attribution():
        writer.events.append("drive")
    assert writer.events == ["drive"]


def test_no_trace_writer_never_wraps_and_never_raises(spy) -> None:
    # A no-op writer (no active trace) is a benign no-op — attribute_run returns a
    # nullcontext, so the scope is never entered and nothing raises.
    writer = spy(trace_id=None)
    with run_attribution(RunAttribution(tags=["t"])), stamp_run_attribution():
        writer.events.append("drive")
    assert writer.events == ["drive"]


def test_run_attribution_contextvar_reset() -> None:
    assert get_run_attribution() is None
    with run_attribution(RunAttribution(tags=["x"])):
        assert get_run_attribution() is not None
    assert get_run_attribution() is None
