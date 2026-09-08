"""The generic run-attribution enabler: the ambient deposit + the wrap helpers.

``stamp_run_attribution`` ENTERS ``attribute_run(writer, attribution)`` AROUND the drive
when an ambient :class:`RunAttribution` is deposited — the drive runs INSIDE the scope
(entered before, exited after), never a one-shot fired beside it — and no-ops when none is
deposited. It is fail-safe: a writer with no active trace never raises AND (post guard-rework)
still stamps, since the deposit is context-scoped and lands on the trace opened inside it.

``stamp_preset_attribution`` layers a registered preset's identity + version onto the run
trace at the OUTERMOST preset dispatch only — a nested sub-preset dispatch adds nothing.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest
from tai42_contract.monitoring import RUN_VERSION_METADATA_KEY, RunAttribution

import tai42_skeleton.monitoring as monitoring_mod
from tai42_skeleton.tools.attribution import (
    get_run_attribution,
    run_attribution,
    stamp_preset_attribution,
    stamp_run_attribution,
)


class _SpyWriter:
    """Records the enter/exit of the attribute_run scope + the stamp kwargs."""

    def __init__(self, *, trace_id: str | None) -> None:
        self._trace_id = trace_id
        self.events: list[str] = []
        self.stamps: list[dict] = []

    def current_trace_id(self) -> str | None:
        return self._trace_id

    @contextmanager
    def trace_attributes(self, *, name, tags, metadata, user_id=None, session_id=None):
        self.stamps.append(
            {"name": name, "tags": tags, "metadata": metadata, "user_id": user_id, "session_id": session_id}
        )
        self.events.append("enter")
        try:
            yield
        finally:
            self.events.append("exit")


class _SpyMonitoring:
    # Carries whatever writer a test injects — a conforming spy or a deliberately
    # off-contract/faulting one — so the type is ``object``, not the spy's concrete type.
    def __init__(self, writer: object) -> None:
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


def test_ambient_attribution_forwards_identity_dimensions(spy) -> None:
    writer = spy(trace_id="trace-1")
    with (
        run_attribution(RunAttribution(tags=["t"], user_id="person-1", session_id="thread-9")),
        stamp_run_attribution(),
    ):
        pass
    assert writer.stamps[0]["user_id"] == "person-1"
    assert writer.stamps[0]["session_id"] == "thread-9"


def test_no_ambient_attribution_no_wrap(spy) -> None:
    writer = spy(trace_id="trace-1")
    with stamp_run_attribution():
        writer.events.append("drive")
    assert writer.events == ["drive"]


def test_deposit_before_a_trace_still_stamps(spy) -> None:
    # With an ambient attribution deposited, the stamp is entered even when the writer
    # reports no active trace yet — the deposit is context-scoped and lands on the trace
    # opened inside the scope, so it is NOT skipped.
    writer = spy(trace_id=None)
    with run_attribution(RunAttribution(tags=["t"])), stamp_run_attribution():
        writer.events.append("drive")
    assert writer.events == ["enter", "drive", "exit"]


def test_run_attribution_contextvar_reset() -> None:
    assert get_run_attribution() is None
    with run_attribution(RunAttribution(tags=["x"])):
        assert get_run_attribution() is not None
    assert get_run_attribution() is None


# -- preset attribution (outermost-only, nested does not restamp) -------------


def test_preset_attribution_stamps_tags_metadata_and_version(spy) -> None:
    writer = spy(trace_id="trace-1")
    with stamp_preset_attribution("weather_ny", 7):
        writer.events.append("drive")
    assert writer.events == ["enter", "drive", "exit"]
    stamp = writer.stamps[0]
    assert stamp["tags"] == ["preset:weather_ny", "preset-v:7"]
    assert stamp["metadata"] == {
        "preset_name": "weather_ny",
        "preset_version": "7",
        RUN_VERSION_METADATA_KEY: "7",
    }


def test_nested_preset_dispatch_does_not_restamp(spy) -> None:
    # A sub-preset dispatched INSIDE an outer preset must NOT layer a second preset
    # stamp — only the outermost preset's tags/version reach the root trace.
    writer = spy(trace_id="trace-1")
    with stamp_preset_attribution("outer", 3), stamp_preset_attribution("inner", 9):
        writer.events.append("drive")
    # Exactly one stamp — the OUTER one. The inner dispatch saw the armed guard and
    # added nothing (no enter/exit, no second tags entry).
    assert writer.events == ["enter", "drive", "exit"]
    assert len(writer.stamps) == 1
    assert writer.stamps[0]["tags"] == ["preset:outer", "preset-v:3"]


def test_preset_arm_guard_resets_after_the_block(spy) -> None:
    # After an outermost preset dispatch unwinds, the guard is disarmed so the NEXT
    # top-level preset dispatch stamps again (the arming is per-dispatch, not sticky).
    writer = spy(trace_id="trace-1")
    with stamp_preset_attribution("first", 1):
        pass
    with stamp_preset_attribution("second", 2):
        pass
    assert [s["tags"] for s in writer.stamps] == [["preset:first", "preset-v:1"], ["preset:second", "preset-v:2"]]


# -- hardening: a monitoring-writer fault never breaks the run ----------------


class _OffContractWriter:
    """A writer whose trace_attributes is OFF-CONTRACT — missing the user_id/session_id
    kwargs the contract declares (a plugin that fell behind). Called with them it raises
    TypeError at argument binding, BEFORE any enter guard could run."""

    @contextmanager
    def trace_attributes(self, *, name=None, tags=None, metadata=None):
        yield


class _EnterRaisingWriter:
    """A contract-shaped writer that raises INSIDE trace_attributes on enter."""

    @contextmanager
    def trace_attributes(self, *, name=None, tags=None, metadata=None, user_id=None, session_id=None):
        raise RuntimeError("writer boom on enter")
        yield  # pragma: no cover - unreachable, keeps this a generator


def _install(monkeypatch: pytest.MonkeyPatch, writer: object) -> None:
    monkeypatch.setattr(monitoring_mod, "get_monitoring", lambda: _SpyMonitoring(writer))


def test_off_contract_writer_does_not_break_the_run(monkeypatch, caplog) -> None:
    # An off-contract writer (TypeError at binding) must NOT break the run: the drive
    # completes UNattributed and the fault is logged loudly, not swallowed silently.
    _install(monkeypatch, _OffContractWriter())
    ran = []
    with (
        caplog.at_level(logging.WARNING),
        run_attribution(RunAttribution(tags=["t"], user_id="u", session_id="s")),
        stamp_run_attribution(),
    ):
        ran.append("drive")
    assert ran == ["drive"]
    assert "monitoring writer" in caplog.text.lower()


def test_writer_raising_on_enter_does_not_break_the_run(monkeypatch) -> None:
    _install(monkeypatch, _EnterRaisingWriter())
    ran = []
    with run_attribution(RunAttribution(tags=["t"])), stamp_run_attribution():
        ran.append("drive")
    assert ran == ["drive"]


def test_off_contract_writer_still_propagates_the_bodys_own_error(monkeypatch) -> None:
    # The guard tolerates WRITER faults; it must never swallow the run's OWN error.
    _install(monkeypatch, _OffContractWriter())
    with (
        pytest.raises(ValueError, match="boom"),
        run_attribution(RunAttribution(tags=["t"], user_id="u")),
        stamp_run_attribution(),
    ):
        raise ValueError("boom")


def test_conforming_writer_scope_still_propagates_the_bodys_own_error(spy) -> None:
    # With the scope genuinely entered, the exit must not mask the body's exception.
    spy(trace_id="trace-1")
    with (
        pytest.raises(ValueError, match="boom"),
        run_attribution(RunAttribution(tags=["t"])),
        stamp_run_attribution(),
    ):
        raise ValueError("boom")


def test_off_contract_writer_does_not_break_a_preset_stamp(monkeypatch) -> None:
    _install(monkeypatch, _OffContractWriter())
    ran = []
    with stamp_preset_attribution("weather_ny", 7):
        ran.append("drive")
    assert ran == ["drive"]
