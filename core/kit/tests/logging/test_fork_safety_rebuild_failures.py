"""Streams the child-side fork hook cannot rebuild and steps that fail: what each leaves, and the report naming them.

Each test points the hook at a registry holding only its own handlers (``register``), and closes
every stream it opens, the rebuilt ones included.
"""

from __future__ import annotations

import errno
import io
import logging
import os
import socket
import sys
import traceback
import types
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

import pytest

from tai42_kit.logging import fork_safety
from tai42_kit.logging.stream_bindings import NotReadBack

from .conftest import (
    INHERITED,
    REBUILT,
    FileIOSubclass,
    OnceRebindableHandler,
    Register,
    SavedStreamHandler,
    StreamFixedHandler,
    UnreadableBufferProxy,
    UnreadableTextProxy,
    bound_now,
    named,
    raw_of,
    rebuilding,
    record,
    reported,
    reported_on,
    text_stream,
)


def test_streams_it_cannot_rebuild_or_rebind_are_left_and_named_after_the_rest_is_rebuilt(
    register: Register, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    left, right = socket.socketpair()
    over_a_socket = left.makefile("w", encoding="utf-8")
    socket_handler = logging.StreamHandler(over_a_socket)
    private = open(tmp_path / "private.log", "w", encoding="utf-8")  # noqa: SIM115
    saved_handler = SavedStreamHandler(private)
    file_handler = logging.FileHandler(tmp_path / "file.log", encoding="utf-8")
    old = text_stream(file_handler.stream)
    register(socket_handler, saved_handler, file_handler)

    try:
        fork_safety._after_in_child()

        assert reported_on(capfd) == [
            f"TypeError: {named(socket_handler)}: not rebuilt, its buffered writer is a BufferedWriter over a SocketIO",
            f"TypeError: {named(saved_handler)}: not rebuilt, {named(saved_handler)} reads back {private!r}"
            " as its stream, not the stream rebuilt for it",
            bound_now(named(saved_handler), INHERITED, raw_of(private)),
        ]
        assert socket_handler.stream is over_a_socket
        assert not over_a_socket.closed
        assert not private.closed
        assert file_handler.stream is not old
        assert old.closed
    finally:
        file_handler.close()
        private.close()
        over_a_socket.close()
        left.close()
        right.close()


def test_a_step_that_fails_leaves_its_stream_open_and_every_other_stream_is_rebuilt(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    failing = logging.FileHandler(tmp_path / "failing.log", encoding="utf-8")
    failing_stream = text_stream(failing.stream)
    stderr = open(tmp_path / "stderr.log", "w", encoding="utf-8")  # noqa: SIM115
    monkeypatch.setattr(sys, "stderr", stderr)
    last_resort = logging._StderrHandler(logging.WARNING)  # pyright: ignore[reportAttributeAccessIssue]
    later = logging.FileHandler(tmp_path / "later.log", encoding="utf-8")
    later_stream = text_stream(later.stream)
    register(failing, last_resort, later)
    dup = os.dup

    def dup_failing_at_the_descriptor_limit(fd: int) -> int:
        if fd == failing_stream.fileno():
            raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))
        return dup(fd)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "dup", dup_failing_at_the_descriptor_limit)
            fork_safety._after_in_child()

        assert failing.stream is failing_stream
        assert not failing_stream.closed
        assert sys.stderr is not stderr
        assert stderr.closed
        assert not text_stream(sys.stderr).closed
        assert later.stream is not later_stream
        assert not text_stream(later.stream).closed
        failing.emit(record("left as it was"))
        last_resort.emit(record("through the rebuilt stderr"))
        failing_now = bound_now(named(failing), INHERITED, raw_of(failing_stream))
    finally:
        failing.close()
        later.close()
        sys.stderr.close()
    assert (tmp_path / "failing.log").read_text(encoding="utf-8") == "left as it was\n"
    assert reported((tmp_path / "stderr.log").read_text(encoding="utf-8")) == (
        [
            f"OSError: [Errno {errno.EMFILE}] {os.strerror(errno.EMFILE)}",
            rebuilding(named(failing)),
            failing_now,
        ],
        "through the rebuilt stderr\n",
    )


class _UnreadableStreamHandler(logging.Handler):
    """A handler whose ``stream`` raises when read."""

    @property
    def stream(self) -> TextIO:
        raise RuntimeError("the stream cannot be read")


def test_a_handler_whose_stream_raises_is_named_and_every_other_stream_is_rebuilt(
    register: Register, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    unreadable = _UnreadableStreamHandler()
    unreadable.set_name("unreadable")
    file_handler = logging.FileHandler(tmp_path / "file.log", encoding="utf-8")
    old = text_stream(file_handler.stream)
    register(unreadable, file_handler)

    try:
        fork_safety._after_in_child()

        assert reported_on(capfd) == [
            "RuntimeError: the stream cannot be read",
            "raised reading the stream of the _UnreadableStreamHandler named 'unreadable'",
        ]
        assert file_handler.stream is not old
        assert old.closed
    finally:
        file_handler.close()


def test_a_stream_whose_new_layers_fail_to_build_is_left_as_it_was(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    path = tmp_path / "file.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    old = text_stream(handler.stream)
    register(handler)

    def fail_to_build(text: io.TextIOWrapper, buffer: io.BufferedWriter) -> io.TextIOWrapper:
        raise RuntimeError("building the text stream failed")

    monkeypatch.setattr(fork_safety, "_rewrapped", fail_to_build)

    try:
        fork_safety._after_in_child()

        assert reported_on(capfd) == [
            "RuntimeError: building the text stream failed",
            rebuilding(named(handler)),
            bound_now(named(handler), INHERITED, raw_of(old)),
        ]
        assert handler.stream is old
        assert not old.closed
        handler.emit(record("left as it was"))
    finally:
        handler.close()
    assert path.read_text(encoding="utf-8") == "left as it was\n"


class _TextSubclass(io.TextIOWrapper):
    """A subclass of the text stream type, whose own construction the hook cannot repeat."""


def test_a_text_stream_subclass_is_left_and_named(
    register: Register, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    stream = _TextSubclass(open(tmp_path / "subclass.log", "wb"), encoding="utf-8")  # noqa: SIM115
    handler = logging.StreamHandler(stream)
    register(handler)

    try:
        fork_safety._after_in_child()

        assert reported_on(capfd) == [f"TypeError: {named(handler)}: not rebuilt, {stream!r} is a _TextSubclass"]
        assert handler.stream is stream
        assert not stream.closed
    finally:
        stream.close()


def test_a_raw_layer_subclass_is_left_and_named(register: Register, tmp_path: Path, capfd: pytest.CaptureFixture[str]):
    stream = io.TextIOWrapper(io.BufferedWriter(FileIOSubclass(tmp_path / "subclass.log", "w")), encoding="utf-8")
    handler = logging.StreamHandler(stream)
    register(handler)

    try:
        fork_safety._after_in_child()

        assert reported_on(capfd) == [
            f"TypeError: {named(handler)}: not rebuilt, its buffered writer is a BufferedWriter over a FileIOSubclass"
        ]
        assert handler.stream is stream
        assert not stream.closed
    finally:
        stream.close()


@pytest.mark.parametrize("where", ["a pipe", "the start of a file"])
def test_a_byte_order_mark_stream_where_the_mark_cannot_be_read_is_left_and_named(
    register: Register, tmp_path: Path, capfd: pytest.CaptureFixture[str], where: str
):
    if where == "a pipe":
        reader, writer = os.pipe()
        os.close(reader)
        stream = open(writer, "w", encoding="utf-8-sig")  # noqa: SIM115
    else:
        stream = open(tmp_path / "marked.log", "w", encoding="utf-8-sig")  # noqa: SIM115
    handler = logging.StreamHandler(stream)
    register(handler)

    try:
        fork_safety._after_in_child()

        assert reported_on(capfd) == [
            f"TypeError: {named(handler)}: not rebuilt, {stream!r} starts with a byte-order mark the parent may already"
            " have written"
        ]
        assert handler.stream is stream
        assert not stream.closed
    finally:
        stream.close()


def test_a_handler_that_does_not_read_back_its_rebuilt_stream_is_named_and_its_stream_left_as_it_was(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    path = tmp_path / "stderr.log"
    stderr = open(path, "w", encoding="utf-8")  # noqa: SIM115
    monkeypatch.setattr(sys, "stderr", stderr)
    saved = SavedStreamHandler(stderr)
    register(saved)

    try:
        fork_safety._after_in_child()

        assert sys.stderr is stderr
        assert saved.stream is stderr
        assert not stderr.closed
        saved.emit(record("left as it was"))
        bound_back = [bound_now(who, INHERITED, raw_of(stderr)) for who in ("sys.stderr", named(saved))]
    finally:
        stderr.close()
    assert reported(path.read_text(encoding="utf-8")) == (
        [
            f"TypeError: sys.stderr, {named(saved)}: not rebuilt, {named(saved)} reads back {stderr!r} as its stream,"
            " not the stream rebuilt for it",
            *bound_back,
        ],
        "left as it was\n",
    )


class _StreamLostOnSetHandler(logging.StreamHandler[TextIO]):
    """A handler whose ``stream``, once set after construction, raises ``lost`` when read."""

    def __init__(self, saved: TextIO) -> None:
        self.lost = AttributeError("the stream is lost")
        self.stream_lost = False
        super().__init__(saved)

    @property  # pyright: ignore[reportIncompatibleVariableOverride]
    def stream(self) -> TextIO:
        if self.stream_lost:
            raise self.lost
        return self._saved

    @stream.setter
    def stream(self, value: TextIO) -> None:
        if "_saved" in vars(self):
            self.stream_lost = True
        else:
            self._saved = value


def test_a_handler_whose_stream_raises_attribute_error_when_read_back_is_named_with_that_raise(
    register: Register, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    old = open(tmp_path / "file.log", "w", encoding="utf-8")  # noqa: SIM115
    lost = _StreamLostOnSetHandler(old)
    register(lost)

    try:
        fork_safety._after_in_child()

        # The interpreter may add a suggestion to an ``AttributeError`` raised by a property, so the
        # raise is read as the standard library formats it.
        raised = "".join(traceback.format_exception_only(lost.lost)).strip()
        assert raised.startswith("AttributeError: the stream is lost")
        assert reported_on(capfd) == [
            f"TypeError: {named(lost)}: not rebuilt, {named(lost)} raised reading back its stream: {raised}",
            f"the stream of {named(lost)} cannot be read: {raised}",
        ]
        assert not old.closed
    finally:
        lost.stream_lost = False
        old.close()


def test_a_rolled_back_stream_that_owns_its_descriptor_leaves_it_open_for_the_next_stream_over_it(
    register: Register, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    path = tmp_path / "file.log"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT)
    owning = open(fd, "w", encoding="utf-8")  # noqa: SIM115
    saved = SavedStreamHandler(owning)
    borrowing = logging.StreamHandler(open(fd, "w", encoding="utf-8", closefd=False))  # noqa: SIM115
    old = text_stream(borrowing.stream)
    register(saved, borrowing)

    try:
        fork_safety._after_in_child()

        assert reported_on(capfd) == [
            f"TypeError: {named(saved)}: not rebuilt, {named(saved)} reads back {owning!r} as its stream, not the"
            " stream rebuilt for it",
            bound_now(named(saved), INHERITED, raw_of(owning)),
            bound_now(named(borrowing), REBUILT, raw_of(text_stream(borrowing.stream))),
        ]
        assert not owning.closed
        assert not raw_of(text_stream(borrowing.stream)).closed
        assert borrowing.stream is not old
        assert old.closed
        saved.emit(record("owning line"))
        borrowing.emit(record("borrowing line"))
    finally:
        borrowing.stream.close()
        owning.close()
    assert path.read_text(encoding="utf-8") == "owning line\nborrowing line\n"


def _a_text_proxy_whose_buffer_raises() -> UnreadableTextProxy:
    return UnreadableTextProxy()


def _a_text_stream_over_a_buffer_proxy_whose_raw_raises() -> io.TextIOWrapper:
    return io.TextIOWrapper(UnreadableBufferProxy(), encoding="utf-8")  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("unreadable", "failure"),
    [
        (_a_text_proxy_whose_buffer_raises, "RuntimeError: the buffer cannot be read"),
        (_a_text_stream_over_a_buffer_proxy_whose_raw_raises, "RuntimeError: the raw cannot be read"),
    ],
)
def test_a_proxy_whose_own_buffer_or_raw_raises_is_named_and_every_other_stream_is_rebuilt(
    register: Register,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    unreadable: Callable[[], TextIO],
    failure: str,
):
    unreadable_handler = logging.StreamHandler(unreadable())
    unreadable_handler.set_name("unreadable")
    file_handler = logging.FileHandler(tmp_path / "file.log", encoding="utf-8")
    old = text_stream(file_handler.stream)
    register(unreadable_handler, file_handler)

    try:
        fork_safety._after_in_child()

        assert reported_on(capfd) == [failure, "raised reading the stream of the StreamHandler named 'unreadable'"]
        assert file_handler.stream is not old
        assert old.closed
    finally:
        file_handler.close()


class _ReprRaisingText(io.TextIOWrapper):
    """A text stream, of a subclass the hook names as not rebuilt, whose ``repr`` raises."""

    def __repr__(self) -> str:
        raise RuntimeError("the repr cannot be read")


def test_a_stream_that_raises_while_it_is_told_apart_is_named_and_every_other_stream_over_its_descriptor_is_rebuilt(
    register: Register, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    handler = logging.FileHandler(tmp_path / "file.log", encoding="utf-8")
    handler.set_name("rebuilt")
    old = text_stream(handler.stream)
    beside_raw = io.FileIO(old.fileno(), "w", closefd=False)
    beside = _ReprRaisingText(io.BufferedWriter(beside_raw), encoding="utf-8")
    beside_handler = logging.StreamHandler(beside)
    beside_handler.set_name("beside")
    register(handler, beside_handler)

    try:
        fork_safety._after_in_child()

        rebuilt = text_stream(handler.stream)
        assert rebuilt is not old
        assert beside_handler.stream is beside
        assert reported_on(capfd) == [
            "RuntimeError: the repr cannot be read",
            rebuilding(named(beside_handler)),
            bound_now(named(handler), REBUILT, raw_of(rebuilt)),
            bound_now(named(beside_handler), INHERITED, beside_raw),
        ]
        assert not beside_raw.closed
    finally:
        handler.close()
        io.TextIOWrapper.close(beside)


def test_a_raise_after_the_rebind_puts_every_binding_back_and_leaves_its_stream_as_it_was(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    path = tmp_path / "stderr.log"
    stderr = open(path, "w", encoding="utf-8")  # noqa: SIM115
    monkeypatch.setattr(sys, "stderr", stderr)
    handler = logging.StreamHandler(stderr)
    register(handler)

    def raise_reading_back(bindings: object, rebuilt: object) -> NotReadBack | None:
        raise RuntimeError("reading back failed")

    monkeypatch.setattr(fork_safety, "first_not_read_back", raise_reading_back)

    try:
        fork_safety._after_in_child()

        assert sys.stderr is stderr
        assert handler.stream is stderr
        assert not stderr.closed
        handler.emit(record("left as it was"))
        bound_back = [bound_now(who, INHERITED, raw_of(stderr)) for who in ("sys.stderr", named(handler))]
    finally:
        stderr.close()
    assert reported(path.read_text(encoding="utf-8")) == (
        [
            "RuntimeError: reading back failed",
            rebuilding("sys.stderr", named(handler)),
            *bound_back,
        ],
        "left as it was\n",
    )


def test_handlers_that_refuse_the_rebind_stay_on_their_inherited_stream_and_every_other_stream_over_it_is_rebuilt(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    path = tmp_path / "file.log"
    old = open(path, "w", encoding="utf-8")  # noqa: SIM115
    fixed = [StreamFixedHandler(old), StreamFixedHandler(old)]
    fixed[0].set_name("h1")
    fixed[1].set_name("h2")
    fd = old.fileno()
    flushes: list[None] = []

    def flush_failing_once() -> None:
        flushes.append(None)
        if len(flushes) == 1:
            raise RuntimeError("flush failed")

    # The raw layer the hook builds copies this ``flush``, so the first close to run, the one of the raw
    # layer built for the rolled-back stream, raises.
    monkeypatch.setattr(raw_of(old), "flush", flush_failing_once)
    borrowing = logging.StreamHandler(open(fd, "w", encoding="utf-8", closefd=False))  # noqa: SIM115
    borrowing_old = text_stream(borrowing.stream)
    register(*fixed, borrowing)

    try:
        fork_safety._after_in_child()

        assert reported_on(capfd) == [
            f"TypeError: {named(fixed[0])}, {named(fixed[1])}: not rebuilt, {named(fixed[0])} reads back {old!r} as"
            " its stream, not the stream rebuilt for it; it declined the assignment: AttributeError: the stream is"
            " fixed",
            "RuntimeError: flush failed",
            f"raised closing the raw layer built over descriptor {fd}",
            bound_now(named(fixed[0]), INHERITED, raw_of(old)),
            bound_now(named(fixed[1]), INHERITED, raw_of(old)),
            bound_now(named(borrowing), REBUILT, raw_of(text_stream(borrowing.stream))),
        ]
        assert all(handler.stream is old for handler in fixed)
        assert not old.closed
        assert borrowing.stream is not borrowing_old
        assert raw_of(borrowing_old).closed
        assert not raw_of(text_stream(borrowing.stream)).closed
        borrowing.emit(record("borrowing line"))
    finally:
        text_stream(borrowing.stream).close()
        old.close()
    assert path.read_text(encoding="utf-8") == "borrowing line\n"


class _StreamSetRaisingHandler(logging.StreamHandler[TextIO]):
    """A handler whose ``stream``, once set, raises ``RuntimeError`` when it is set again."""

    def __setattr__(self, name: str, value: object) -> None:
        if name == "stream" and "stream" in vars(self):
            raise RuntimeError("setting the stream failed")
        super().__setattr__(name, value)


def test_a_handler_whose_stream_set_raises_anything_but_attribute_error_rolls_back_every_binding_over_its_file(
    register: Register, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    path = tmp_path / "file.log"
    old = open(path, "w", encoding="utf-8")  # noqa: SIM115
    plain, raising = logging.StreamHandler(old), _StreamSetRaisingHandler(old)
    plain.set_name("plain")
    raising.set_name("raising")
    register(plain, raising)

    try:
        fork_safety._after_in_child()

        assert reported_on(capfd) == [
            "RuntimeError: setting the stream failed",
            rebuilding(named(plain), named(raising)),
            bound_now(named(plain), INHERITED, raw_of(old)),
            bound_now(named(raising), INHERITED, raw_of(old)),
        ]
        assert plain.stream is old
        assert raising.stream is old
        assert not old.closed
        plain.emit(record("plain line"))
        raising.emit(record("raising line"))
    finally:
        old.close()
    assert path.read_text(encoding="utf-8") == "plain line\nraising line\n"


class _StderrGuardedModule(types.ModuleType):
    """A module type whose ``stderr`` raises ``AttributeError`` when it is set."""

    def __setattr__(self, name: str, value: object) -> None:
        if name == "stderr":
            raise AttributeError("sys.stderr is guarded")
        super().__setattr__(name, value)


def test_a_sys_name_whose_set_raises_attribute_error_rolls_back_every_binding_over_its_file(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    path = tmp_path / "stderr.log"
    stderr = open(path, "w", encoding="utf-8")  # noqa: SIM115
    monkeypatch.setattr(sys, "stderr", stderr)
    handler = logging.StreamHandler(stderr)
    handler.set_name("h")
    register(handler)

    module_type = type(sys)
    sys.__class__ = _StderrGuardedModule  # pyright: ignore[reportAttributeAccessIssue]
    try:
        fork_safety._after_in_child()

        assert sys.stderr is stderr
        assert handler.stream is stderr
        assert not stderr.closed
        handler.emit(record("left as it was"))
        bound_back = [bound_now(who, INHERITED, raw_of(stderr)) for who in ("sys.stderr", named(handler))]
    finally:
        sys.__class__ = module_type  # pyright: ignore[reportAttributeAccessIssue]
        handler.stream.close()
        stderr.close()
    assert reported(path.read_text(encoding="utf-8")) == (
        [
            "AttributeError: sys.stderr is guarded",
            rebuilding("sys.stderr", named(handler)),
            *bound_back,
        ],
        "left as it was\n",
    )


@pytest.mark.parametrize("another_handler", [True, False])
def test_a_handler_that_refuses_the_rollback_is_left_on_its_closed_rebuilt_stream_and_every_other_is_put_back(
    register: Register, tmp_path: Path, capfd: pytest.CaptureFixture[str], another_handler: bool
):
    old = open(tmp_path / "file.log", "w", encoding="utf-8")  # noqa: SIM115
    refusing = OnceRebindableHandler(old)
    refusing.set_name("refusing")
    plain = logging.StreamHandler(old)
    plain.set_name("plain")
    # A handler that does not read back the stream rebuilt for it, so the rebuild is rolled back.
    saved = SavedStreamHandler(old)
    saved.set_name("saved")
    handlers = [refusing, plain, saved] if another_handler else [refusing, saved]
    register(*handlers)
    written_by = ", ".join(named(handler) for handler in handlers)

    try:
        fork_safety._after_in_child()

        refusing_now = text_stream(refusing.stream)
        assert refusing_now is not old
        assert raw_of(refusing_now).closed
        assert plain.stream is old
        assert not old.closed
        assert reported_on(capfd) == [
            f"TypeError: {written_by}: not rebuilt, {named(saved)} reads back {old!r} as its stream, not the stream"
            " rebuilt for it",
            "AttributeError: the stream was set once already",
            f"raised putting {named(refusing)} back on its inherited stream",
            bound_now(named(refusing), REBUILT, raw_of(refusing_now)),
            *([bound_now(named(plain), INHERITED, raw_of(old))] if another_handler else []),
            bound_now(named(saved), INHERITED, raw_of(old)),
        ]
        with pytest.raises(ValueError, match="closed file"):
            refusing_now.write("refusing line\n")
    finally:
        old.close()


class _UnreadableRepr(io.StringIO):
    """A stream whose ``repr`` raises."""

    def __repr__(self) -> str:
        raise RuntimeError("the repr cannot be read")


class _ElsewhereOnRollbackHandler(logging.StreamHandler[TextIO]):
    """A handler whose ``stream``, set a second time after construction, is left as ``elsewhere`` gives.

    That is ``elsewhere`` itself, another stream, or, when ``elsewhere`` is ``None``, a ``stream``
    that raises when read.
    """

    def __init__(self, stream: TextIO, elsewhere: io.StringIO | None) -> None:
        self._elsewhere = elsewhere
        super().__init__(stream)

    def __setattr__(self, name: str, value: object) -> None:
        if name == "stream":
            sets = vars(self).get("_stream_sets", 0) + 1
            object.__setattr__(self, "_stream_sets", sets)
            if sets == 3:
                object.__setattr__(self, "_lost", self._elsewhere is None)
                value = self._elsewhere
        super().__setattr__(name, value)

    def __getattribute__(self, name: str) -> object:
        if name == "stream" and vars(self).get("_lost"):
            raise RuntimeError("the stream is lost")
        return super().__getattribute__(name)


@pytest.mark.parametrize("left_on", ["another stream", "a stream whose repr raises", "a raise"])
def test_a_binding_the_rollback_leaves_on_a_stream_the_hook_did_not_build_is_reported_as_it_reads(
    register: Register, tmp_path: Path, capfd: pytest.CaptureFixture[str], left_on: str
):
    old = open(tmp_path / "file.log", "w", encoding="utf-8")  # noqa: SIM115
    elsewhere = {"another stream": io.StringIO(), "a stream whose repr raises": _UnreadableRepr(), "a raise": None}[
        left_on
    ]
    moved = _ElsewhereOnRollbackHandler(old, elsewhere)
    moved.set_name("moved")
    # A handler that does not read back the stream rebuilt for it, so the rebuild is rolled back.
    saved = SavedStreamHandler(old)
    saved.set_name("saved")
    register(moved, saved)
    if left_on == "another stream":
        now = f"{named(moved)} is bound to another stream, {elsewhere!r}"
    else:
        raised = "the repr cannot be read" if left_on == "a stream whose repr raises" else "the stream is lost"
        now = f"the stream of {named(moved)} cannot be read: RuntimeError: {raised}"

    try:
        fork_safety._after_in_child()

        assert reported_on(capfd) == [
            f"TypeError: {named(moved)}, {named(saved)}: not rebuilt, {named(saved)} reads back {old!r} as its"
            " stream, not the stream rebuilt for it",
            now,
            bound_now(named(saved), INHERITED, raw_of(old)),
        ]
        assert not old.closed
    finally:
        old.close()


def _over_a_bytes_io() -> io.TextIOWrapper:
    """A text stream over a buffered writer over a ``BytesIO``, which the hook names as not rebuilt."""
    return io.TextIOWrapper(io.BufferedWriter(io.BytesIO()), encoding="utf-8")


_OVER_A_BYTES_IO = "not rebuilt, its buffered writer is a BufferedWriter over a BytesIO"


class _ClassRaisingHandler(logging.StreamHandler[TextIO]):
    """A handler whose ``__class__`` raises when read, as ``isinstance`` reads it against any other type."""

    @property  # pyright: ignore[reportIncompatibleMethodOverride]
    def __class__(self) -> type:  # pyright: ignore[reportIncompatibleVariableOverride]
        raise RuntimeError("the class cannot be read")


@pytest.mark.parametrize("rebuilt", [True, False])
def test_telling_a_handler_from_a_sys_name_runs_none_of_the_handler_s_code(
    register: Register, tmp_path: Path, capfd: pytest.CaptureFixture[str], rebuilt: bool
):
    stream = open(tmp_path / "file.log", "w", encoding="utf-8") if rebuilt else _over_a_bytes_io()  # noqa: SIM115
    handler = _ClassRaisingHandler(stream)
    register(handler)

    try:
        fork_safety._after_in_child()

        if rebuilt:
            assert capfd.readouterr().err == ""
            assert handler.stream is not stream
            assert stream.closed
        else:
            assert reported_on(capfd) == [f"TypeError: {named(handler)}: {_OVER_A_BYTES_IO}"]
            assert handler.stream is stream
    finally:
        handler.stream.close()
        stream.close()


class _ReprRaisingHandler(logging.StreamHandler[TextIO]):
    """A handler whose own ``repr`` raises."""

    def __repr__(self) -> str:
        raise RuntimeError("the repr cannot be made")


class _NameRaisingHandler(logging.StreamHandler[TextIO]):
    """A handler whose ``name`` raises when read."""

    @property  # pyright: ignore[reportIncompatibleVariableOverride]
    def name(self) -> str:
        raise RuntimeError("the name cannot be read")

    @name.setter
    def name(self, value: str) -> None:
        del value


@pytest.mark.parametrize(
    ("handler_type", "named_as"),
    [
        (_ReprRaisingHandler, "the _ReprRaisingHandler named 'unnamable'"),
        (_NameRaisingHandler, "a handler whose name cannot be read"),
    ],
)
def test_a_handler_is_named_by_its_type_and_name_and_one_that_cannot_be_named_raises_nothing(
    register: Register,
    capfd: pytest.CaptureFixture[str],
    handler_type: type[logging.StreamHandler[TextIO]],
    named_as: str,
):
    handler = handler_type(_over_a_bytes_io())
    handler.set_name("unnamable")
    register(handler)

    fork_safety._after_in_child()

    assert reported_on(capfd) == [f"TypeError: {named_as}: {_OVER_A_BYTES_IO}"]


class _UnreadableWithItsOwnNotesHandler(logging.Handler):
    """A handler whose ``stream`` raises an exception that carries its own notes as a tuple."""

    @property
    def stream(self) -> TextIO:
        error = RuntimeError("the stream cannot be read")
        error.__notes__ = ("a note of its own",)  # pyright: ignore[reportAttributeAccessIssue]
        raise error


class _UnformattableType(type):
    """A metaclass whose classes' ``__module__`` raises when read, so no exception of theirs can be formatted."""

    @property
    def __module__(cls) -> str:  # pyright: ignore[reportIncompatibleVariableOverride]
        raise RuntimeError("the module cannot be read")


class _UnformattableError(Exception, metaclass=_UnformattableType):
    """An exception the standard library's formatting raises on."""


class _UnformattableStreamHandler(logging.Handler):
    """A handler whose ``stream`` raises an exception that cannot be formatted."""

    @property
    def stream(self) -> TextIO:
        raise _UnformattableError("the stream cannot be read")


@pytest.mark.parametrize(
    ("handler_type", "failure"),
    [
        (_UnreadableWithItsOwnNotesHandler, ["RuntimeError: the stream cannot be read", "a note of its own"]),
        (_UnformattableStreamHandler, ["an exception whose text cannot be read"]),
    ],
)
def test_an_exception_raised_by_code_the_hook_did_not_write_is_reported_and_nothing_is_raised(
    register: Register, capfd: pytest.CaptureFixture[str], handler_type: type[logging.Handler], failure: list[str]
):
    handler = handler_type()
    handler.set_name("unreadable")
    register(handler)

    fork_safety._after_in_child()

    assert reported_on(capfd) == [
        *failure,
        f"raised reading the stream of the {handler_type.__name__} named 'unreadable'",
    ]
