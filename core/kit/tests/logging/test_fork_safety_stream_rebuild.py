"""The child-side fork hook's stream rebuild, run in this process on streams it owns.

Each test points the hook at a registry holding only its own handlers (``register``), and closes
every stream it opens, the rebuilt ones included.
"""

from __future__ import annotations

import codecs
import gc
import io
import logging
import os
import sys
from pathlib import Path
from typing import TextIO

import pytest

from tai42_kit.logging import fork_safety

from .conftest import Register, raw_of, record, text_stream


# The raw's mode is its inherited one's, an append mode built as the write mode, which does not seek.
@pytest.mark.parametrize(
    ("mode", "buffer_type", "raw_mode"),
    [("a", io.BufferedWriter, "wb"), ("a+", io.BufferedRandom, "rb+"), ("w+", io.BufferedRandom, "rb+")],
)
def test_a_file_handlers_stream_is_rebuilt_over_the_same_descriptor_with_its_settings(
    register: Register, tmp_path: Path, mode: str, buffer_type: type, raw_mode: str
):
    path = tmp_path / "file.log"
    handler = logging.FileHandler(path, mode=mode, encoding="utf-8", errors="strict")
    old = text_stream(handler.stream)
    vars(old.buffer)["label"] = "the buffer's own"
    fd, identity = old.fileno(), os.fstat(old.fileno())
    register(handler)

    fork_safety._after_in_child()

    new = text_stream(handler.stream)
    try:
        assert new is not old
        assert old.closed
        assert not new.closed
        assert new.fileno() == fd
        assert (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == (identity.st_dev, identity.st_ino)
        assert type(new.buffer) is buffer_type
        assert vars(new.buffer) == {"label": "the buffer's own"}
        assert raw_of(new).closefd
        assert (new.name, new.mode, raw_of(new).mode) == (old.name, old.mode, raw_mode)
        assert (new.encoding, new.errors) == ("utf-8", "strict")
        assert (new.line_buffering, new.write_through) == (old.line_buffering, old.write_through)
        handler.emit(record("after the rebuild"))
    finally:
        handler.close()
    assert path.read_text(encoding="utf-8") == "after the rebuild\n"


@pytest.mark.parametrize("mode", ["a", "a+"])
def test_an_append_mode_stream_is_rebuilt_without_moving_the_shared_file_offset(
    register: Register, tmp_path: Path, mode: str
):
    path = tmp_path / "appended.log"
    path.write_text("first\n", encoding="utf-8")
    handler = logging.FileHandler(path, mode=mode, encoding="utf-8")
    fd = text_stream(handler.stream).fileno()
    # The offset belongs to the open file description, which a forked parent shares with its child.
    os.lseek(fd, 0, os.SEEK_SET)
    register(handler)

    try:
        fork_safety._after_in_child()

        assert os.lseek(fd, 0, os.SEEK_CUR) == 0
        handler.emit(record("after the rebuild"))
    finally:
        handler.close()
    assert path.read_text(encoding="utf-8") == "first\nafter the rebuild\n"


def test_a_stream_that_does_not_own_its_descriptor_is_rebuilt_without_closing_it(register: Register, tmp_path: Path):
    fd = os.open(tmp_path / "borrowed.log", os.O_WRONLY | os.O_CREAT)
    old = open(fd, "w", buffering=1, encoding="latin-1", errors="replace", closefd=False)  # noqa: SIM115
    old.reconfigure(write_through=True)
    handler = logging.StreamHandler(old)
    register(handler)

    try:
        fork_safety._after_in_child()
        new = text_stream(handler.stream)
        try:
            assert old.closed
            assert new.fileno() == fd
            os.fstat(fd)
            assert not raw_of(new).closefd
            assert (new.encoding, new.errors, new.line_buffering, new.write_through) == (
                "latin-1",
                "replace",
                True,
                True,
            )
        finally:
            new.close()
        os.fstat(fd)
    finally:
        os.close(fd)


def test_a_stream_two_handlers_share_is_rebuilt_once_and_stays_shared(register: Register, tmp_path: Path):
    old = open(tmp_path / "shared.log", "w", encoding="utf-8")  # noqa: SIM115
    first, second = logging.StreamHandler(old), logging.StreamHandler(old)
    register(first, second)

    fork_safety._after_in_child()

    try:
        assert first.stream is second.stream
        assert first.stream is not old
        assert old.closed
    finally:
        first.stream.close()


class _SlottedStreamHandler(logging.StreamHandler[TextIO]):
    """A handler that stores its ``stream`` in a slot, not in its instance dictionary."""

    __slots__ = ("stream",)


def test_a_handler_storing_its_stream_in_a_slot_is_rebuilt_with_the_others_over_its_file(
    register: Register, tmp_path: Path
):
    path = tmp_path / "slotted.log"
    old = open(path, "w", encoding="utf-8")  # noqa: SIM115
    slotted, plain = _SlottedStreamHandler(old), logging.StreamHandler(old)
    register(slotted, plain)

    try:
        fork_safety._after_in_child()

        assert slotted.stream is plain.stream
        assert slotted.stream is not old
        assert old.closed
        slotted.emit(record("slotted line"))
        plain.emit(record("plain line"))
    finally:
        plain.stream.close()
    assert path.read_text(encoding="utf-8") == "slotted line\nplain line\n"


class _StoringPropertyHandler(logging.StreamHandler[TextIO]):
    """A handler whose ``stream`` is a property whose setter stores the stream it is given."""

    @property  # pyright: ignore[reportIncompatibleVariableOverride]
    def stream(self) -> TextIO:
        return self._stream

    @stream.setter
    def stream(self, value: TextIO) -> None:
        self._stream = value


def test_a_handler_whose_stream_setter_stores_it_is_rebuilt_with_the_others_over_its_file(
    register: Register, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    path = tmp_path / "storing.log"
    old = open(path, "w", encoding="utf-8")  # noqa: SIM115
    storing, plain = _StoringPropertyHandler(old), logging.StreamHandler(old)
    register(storing, plain)

    try:
        fork_safety._after_in_child()

        assert capfd.readouterr().err == ""
        assert storing.stream is plain.stream
        assert storing.stream is not old
        assert old.closed
        storing.emit(record("storing line"))
        plain.emit(record("plain line"))
    finally:
        plain.stream.close()
    assert path.read_text(encoding="utf-8") == "storing line\nplain line\n"


def test_every_sys_name_bound_to_a_rebuilt_stream_is_rebound(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    old = open(tmp_path / "stderr.log", "w", encoding="utf-8")  # noqa: SIM115
    stdout = sys.stdout
    monkeypatch.setattr(sys, "stderr", old)
    monkeypatch.setattr(sys, "__stderr__", old)
    handler = logging.StreamHandler(old)
    register(handler)

    fork_safety._after_in_child()

    try:
        assert sys.stderr is sys.__stderr__ is handler.stream
        assert handler.stream is not old
        assert sys.stdout is stdout
    finally:
        handler.stream.close()


def test_a_last_resort_handler_writes_to_the_rebuilt_stderr(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    path = tmp_path / "stderr.log"
    old = open(path, "w", encoding="utf-8")  # noqa: SIM115
    monkeypatch.setattr(sys, "stderr", old)
    # The class of ``logging.lastResort``: its ``stream`` is ``sys.stderr``, looked up at each write.
    last_resort = logging._StderrHandler(logging.WARNING)  # pyright: ignore[reportAttributeAccessIssue]
    register(last_resort)

    fork_safety._after_in_child()

    try:
        assert sys.stderr is not old
        assert old.closed
        last_resort.emit(record("through last resort"))
    finally:
        sys.stderr.close()
    # The file is the report's channel, the descriptor beneath ``sys.stderr``: it holds the line alone, so the
    # handler's declined assignment, read back as the rebuilt ``sys.stderr``, is no failure and is not reported.
    assert path.read_text(encoding="utf-8") == "through last resort\n"


def test_streams_without_an_inherited_buffer_lock_are_left_as_they_are_and_unreported(
    register: Register, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    unbuffered_raw = open(tmp_path / "unbuffered.log", "wb", buffering=0)  # noqa: SIM115
    closed = open(tmp_path / "closed.log", "w", encoding="utf-8")  # noqa: SIM115
    closed.close()
    streams: list[TextIO | io.StringIO] = [
        io.StringIO(),
        io.TextIOWrapper(unbuffered_raw, encoding="utf-8", write_through=True),
        closed,
    ]
    handlers: list[logging.Handler] = [logging.StreamHandler(stream) for stream in streams]
    delayed = logging.FileHandler(tmp_path / "delayed.log", delay=True)
    gone = logging.StreamHandler(io.StringIO())
    register(*handlers, delayed, gone)
    del gone

    try:
        fork_safety._after_in_child()

        assert [handler.stream for handler in handlers] == streams  # pyright: ignore[reportAttributeAccessIssue]
        assert not streams[1].closed
        assert delayed.stream is None
        assert capfd.readouterr().err == ""
    finally:
        streams[1].close()
        delayed.close()


def test_a_handler_that_has_no_stream_is_passed_over_unreported_and_the_others_are_rebuilt(
    register: Register, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    path = tmp_path / "file.log"
    null = logging.NullHandler()
    handler = logging.FileHandler(path, encoding="utf-8")
    old = text_stream(handler.stream)
    register(null, handler)

    try:
        fork_safety._after_in_child()

        assert capfd.readouterr().err == ""
        assert handler.stream is not old
        assert old.closed
        handler.emit(record("after the rebuild"))
    finally:
        handler.close()
    assert path.read_text(encoding="utf-8") == "after the rebuild\n"


class _LogsWhenCollected:
    """An object in a reference cycle, freed only by the cyclic collector, whose finaliser logs."""

    def __init__(self, handler: logging.Handler, logged: list[None]) -> None:
        self.handler = handler
        self.logged = logged
        self.cycle = self

    def __del__(self) -> None:
        self.handler.emit(record("collected"))
        self.logged.append(None)


def test_a_finaliser_that_logs_never_meets_a_stream_mid_rebuild(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    path = tmp_path / "file.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    failures: list[logging.LogRecord] = []
    monkeypatch.setattr(handler, "handleError", failures.append)
    others = [logging.FileHandler(tmp_path / f"other-{i}.log", encoding="utf-8") for i in range(20)]
    register(handler, *others)
    logged: list[None] = []
    making = [True]

    def make_garbage(phase: str, info: dict[str, int]) -> None:
        # After each collection, garbage for the next one, so a collection at any allocation finalises some.
        if phase == "stop" and making[0]:
            _LogsWhenCollected(handler, logged)

    threshold = gc.get_threshold()
    gc.callbacks.append(make_garbage)
    _LogsWhenCollected(handler, logged)
    gc.set_threshold(1, 1, 1)
    try:
        fork_safety._after_in_child()
    finally:
        making[0] = False
        gc.set_threshold(*threshold)
        gc.callbacks.remove(make_garbage)
    try:
        gc.collect()
        assert failures == []
        assert logged
    finally:
        handler.close()
        for other in others:
            other.close()
    assert path.read_text(encoding="utf-8") == "collected\n" * len(logged)


def test_text_streams_over_one_buffer_are_rebuilt_over_one_new_buffer(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    path = tmp_path / "stdout.log"
    first = open(path, "w", encoding="utf-8")  # noqa: SIM115
    # A second text stream over the same buffer, the way a program re-wraps ``sys.stdout``.
    second = io.TextIOWrapper(first.buffer, encoding="utf-8", line_buffering=True)
    unbound = io.TextIOWrapper(first.buffer, encoding="utf-8")
    monkeypatch.setattr(sys, "__stdout__", first)
    monkeypatch.setattr(sys, "stdout", second)
    first_handler, second_handler = logging.StreamHandler(first), logging.StreamHandler(second)
    register(first_handler, second_handler)

    fork_safety._after_in_child()

    try:
        assert sys.__stdout__ is first_handler.stream
        assert sys.stdout is second_handler.stream
        assert first_handler.stream is not first
        assert second_handler.stream is not second
        new_first, new_second = text_stream(first_handler.stream), text_stream(second_handler.stream)
        assert new_first.buffer is new_second.buffer
        assert new_second.line_buffering
        assert first.closed
        assert second.closed
        assert unbound.closed
        first_handler.emit(record("through the first"))
        second_handler.emit(record("through the second"))
        print("through print", file=sys.stdout)
    finally:
        first_handler.stream.close()
        second_handler.stream.close()
    assert path.read_text(encoding="utf-8") == "through the first\nthrough the second\nthrough print\n"


@pytest.mark.parametrize("inheritable", [False, True])
def test_a_rebuilt_descriptor_keeps_its_inheritable_flag(register: Register, tmp_path: Path, inheritable: bool):
    handler = logging.FileHandler(tmp_path / "file.log", encoding="utf-8")
    fd = text_stream(handler.stream).fileno()
    os.set_inheritable(fd, inheritable)
    register(handler)

    try:
        fork_safety._after_in_child()

        assert text_stream(handler.stream).fileno() == fd
        assert os.get_inheritable(fd) is inheritable
    finally:
        handler.close()


def test_a_byte_order_mark_stream_past_the_start_of_a_file_is_rebuilt_without_a_second_mark(
    register: Register, tmp_path: Path
):
    path = tmp_path / "marked.log"
    handler = logging.FileHandler(path, encoding="utf-8-sig")
    handler.emit(record("before the rebuild"))
    register(handler)

    fork_safety._after_in_child()

    try:
        handler.emit(record("after the rebuild"))
    finally:
        handler.close()
    assert path.read_bytes() == codecs.BOM_UTF8 + b"before the rebuild\nafter the rebuild\n"


def _lowest_free_descriptor() -> int:
    fd = os.dup(0)
    os.close(fd)
    return fd


def test_a_rebuild_leaves_no_descriptor_open_behind_it(register: Register, tmp_path: Path):
    handler = logging.FileHandler(tmp_path / "file.log", encoding="utf-8")
    register(handler)
    before = _lowest_free_descriptor()

    try:
        fork_safety._after_in_child()

        assert _lowest_free_descriptor() == before
    finally:
        handler.close()


def test_buffered_writers_over_one_descriptor_are_rebuilt_together_whichever_owns_it(
    register: Register, tmp_path: Path
):
    path = tmp_path / "file.log"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT)
    owning = logging.StreamHandler(open(fd, "w", encoding="utf-8"))  # noqa: SIM115
    borrowing = logging.StreamHandler(open(fd, "w", encoding="utf-8", closefd=False))  # noqa: SIM115
    inherited = [text_stream(owning.stream), text_stream(borrowing.stream)]
    register(owning, borrowing)

    try:
        fork_safety._after_in_child()

        assert owning.stream is not inherited[0]
        assert borrowing.stream is not inherited[1]
        assert all(stream.closed for stream in inherited)
        owning.emit(record("owning line"))
        borrowing.emit(record("borrowing line"))
    finally:
        borrowing.stream.close()
        owning.stream.close()
    assert path.read_text(encoding="utf-8") == "owning line\nborrowing line\n"
