"""The registry refuses a backend whose writer or reader lacks a member of its contract protocol."""

from __future__ import annotations

import pytest

from tai42_skeleton.monitoring import NoOpReader, NoOpWriter, init_monitoring, registry, reset_monitoring


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_monitoring()
    yield
    reset_monitoring()


class _Backend:
    def __init__(self, writer: object, reader: object) -> None:
        self.writer = writer
        self.reader = reader


class _IncompleteWriter(NoOpWriter):
    open_span = None  # type: ignore[assignment]
    is_recording = None  # type: ignore[assignment]

    def __getattribute__(self, name: str):
        if name in {"open_span", "is_recording"}:
            raise AttributeError(name)
        return super().__getattribute__(name)


class _IncompleteReader(NoOpReader):
    def __getattribute__(self, name: str):
        if name == "get_observation":
            raise AttributeError(name)
        return super().__getattribute__(name)


@pytest.mark.parametrize("staged", [False, True])
def test_a_writer_lacking_members_is_refused_naming_them(staged: bool):
    if staged:
        registry.begin_staging()
    with pytest.raises(TypeError) as info:
        init_monitoring(_Backend(_IncompleteWriter(), NoOpReader()))  # type: ignore[arg-type]
    message = str(info.value)
    assert "its writer _IncompleteWriter lacks is_recording, open_span of the MonitoringWriter protocol" in message
    assert message.startswith("monitoring backend _Backend:")
    assert registry.get_monitoring_staged().writer.__class__ is NoOpWriter


def test_a_reader_lacking_members_is_refused_naming_them():
    with pytest.raises(TypeError, match="its reader _IncompleteReader lacks get_observation of the MonitoringReader"):
        init_monitoring(_Backend(NoOpWriter(), _IncompleteReader()))  # type: ignore[arg-type]


def test_a_complete_backend_passes():
    backend = _Backend(NoOpWriter(), NoOpReader())
    init_monitoring(backend)  # type: ignore[arg-type]
    assert registry.get_monitoring() is backend


def test_a_staged_backend_is_checked_then_activated_with_its_export_health_at_commit():
    from tai42_skeleton.monitoring import health_watch

    class _Recording(NoOpWriter):
        def is_recording(self) -> bool:
            return True

    writer = _Recording()
    backend = _Backend(writer, NoOpReader())
    registry.begin_staging()
    init_monitoring(backend)  # type: ignore[arg-type]
    assert health_watch.active_recorder() is None
    registry.commit_staging()
    assert registry.get_monitoring() is backend
    recorder = health_watch.active_recorder()
    assert recorder is not None
    assert recorder.writer is writer


class _RecordingWriter(NoOpWriter):
    def is_recording(self) -> bool:
        return True


def test_a_build_that_stages_no_backend_keeps_the_live_export_health_recorder():
    from tai42_skeleton.monitoring import health_watch

    init_monitoring(_Backend(_RecordingWriter(), NoOpReader()))  # type: ignore[arg-type]
    recorder = health_watch.active_recorder()
    assert recorder is not None
    registry.begin_staging()
    registry.commit_staging()
    assert health_watch.active_recorder() is recorder


def test_an_aborted_build_leaves_the_live_backend_and_its_recorder():
    from tai42_skeleton.monitoring import health_watch

    live = _Backend(_RecordingWriter(), NoOpReader())
    init_monitoring(live)  # type: ignore[arg-type]
    recorder = health_watch.active_recorder()
    registry.begin_staging()
    init_monitoring(_Backend(_RecordingWriter(), NoOpReader()))  # type: ignore[arg-type]
    registry.abort_staging()
    assert registry.get_monitoring() is live
    assert health_watch.active_recorder() is recorder
