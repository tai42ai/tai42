"""Readiness sentinel writer: the boot-ready latch writes it, shutdown removes it.

Path from ``TAI_READY_SENTINEL_PATH`` (default ``/tmp/tai-ready``), written
atomically, removed idempotently, and a raise on an unwritable path (a loud boot
fault, never a silently unready pod).
"""

from __future__ import annotations

import pytest

from tai42_skeleton.app.readiness_sentinel import (
    DEFAULT_READY_SENTINEL_PATH,
    ready_sentinel_path,
    remove_ready_sentinel,
    write_ready_sentinel,
)


def test_path_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_READY_SENTINEL_PATH", raising=False)
    assert ready_sentinel_path() == DEFAULT_READY_SENTINEL_PATH


def test_path_reads_the_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    target = tmp_path / "ready"
    monkeypatch.setenv("TAI_READY_SENTINEL_PATH", str(target))
    assert ready_sentinel_path() == str(target)


def test_write_creates_the_sentinel_atomically(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    target = tmp_path / "ready"
    monkeypatch.setenv("TAI_READY_SENTINEL_PATH", str(target))
    write_ready_sentinel()
    assert target.read_text() == "ready\n"
    # The temp file used for the atomic rename never lingers.
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    target = tmp_path / "ready"
    monkeypatch.setenv("TAI_READY_SENTINEL_PATH", str(target))
    write_ready_sentinel()
    write_ready_sentinel()
    assert target.read_text() == "ready\n"


def test_write_raises_on_an_unwritable_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # Parent directory does not exist — the write must raise loudly, not swallow.
    target = tmp_path / "missing" / "ready"
    monkeypatch.setenv("TAI_READY_SENTINEL_PATH", str(target))
    with pytest.raises(FileNotFoundError, match="missing"):
        write_ready_sentinel()


def test_remove_deletes_the_sentinel(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    target = tmp_path / "ready"
    monkeypatch.setenv("TAI_READY_SENTINEL_PATH", str(target))
    write_ready_sentinel()
    remove_ready_sentinel()
    assert not target.exists()


def test_remove_missing_is_a_noop(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    target = tmp_path / "ready"
    monkeypatch.setenv("TAI_READY_SENTINEL_PATH", str(target))
    # No write first — a boot that failed before the latch leaves no sentinel.
    remove_ready_sentinel()
    assert not target.exists()


def test_remove_logs_and_continues_on_other_errors(monkeypatch: pytest.MonkeyPatch, tmp_path, caplog) -> None:
    monkeypatch.setenv("TAI_READY_SENTINEL_PATH", str(tmp_path / "ready"))
    import tai42_skeleton.app.readiness_sentinel as sentinel

    def boom(_path: str) -> None:
        raise PermissionError("nope")

    monkeypatch.setattr(sentinel.os, "remove", boom)
    with caplog.at_level("ERROR"):
        remove_ready_sentinel()  # must NOT raise — shutdown teardown continues
    assert "readiness sentinel" in caplog.text
