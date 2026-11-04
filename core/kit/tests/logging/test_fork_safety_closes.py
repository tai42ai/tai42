"""What the child-side fork hook closes over a descriptor, and the errors those closes raise.

The hook closes the raw layer each rebuild replaces, and every raw layer over a descriptor it
cannot put back. Each test points the hook at a registry holding only its own handlers
(``register``), and closes every stream it opens, the rebuilt ones included.
"""

from __future__ import annotations

import errno
import gc
import io
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

import pytest

from tai42_kit.logging import fork_safety
from tai42_kit.logging.stream_bindings import NotReadBack

from .conftest import (
    INHERITED,
    REBUILT,
    BufferedWriterSubclass,
    FileIOSubclass,
    OnceRebindableHandler,
    Register,
    SavedStreamHandler,
    StreamFixedHandler,
    bound_now,
    named,
    raw_of,
    rebuilding,
    record,
    reported,
    reported_on,
    text_stream,
)


def _flush_raising() -> None:
    raise RuntimeError("flush failed")


def _closing(fd: int, layer: str) -> str:
    return f"raised closing {layer} over descriptor {fd}"


def _closing_inherited(fd: int) -> str:
    return _closing(fd, "the inherited raw layer")


def _rebuilt_now(*handlers: logging.StreamHandler[Any]) -> list[str]:
    """The report's line for each of ``handlers`` bound to the stream rebuilt for it, read now."""
    return [bound_now(named(handler), REBUILT, raw_of(text_stream(handler.stream))) for handler in handlers]


def _drop_copied_attribute(stream: object, name: str) -> None:
    """Take ``name`` off the raw layer the hook built, which copied it from the inherited raw layer."""
    vars(raw_of(text_stream(stream))).pop(name, None)


def test_a_built_raw_whose_close_raises_on_a_rollback_still_puts_every_binding_back(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    path = tmp_path / "stderr.log"
    stderr = open(path, "w", encoding="utf-8")  # noqa: SIM115
    monkeypatch.setattr(sys, "stderr", stderr)
    fd = stderr.fileno()
    flushes: list[None] = []

    def flush_failing_once() -> None:
        flushes.append(None)
        if len(flushes) == 1:
            raise OSError(errno.EIO, os.strerror(errno.EIO))

    # ``FileIO.close`` calls ``flush``, and the hook copies the inherited raw's instance attributes onto
    # the raw it builds, so the first close to run, the built raw's on the rollback, raises.
    monkeypatch.setattr(raw_of(stderr), "flush", flush_failing_once)
    plain = logging.StreamHandler(stderr)
    saved = SavedStreamHandler(stderr)
    register(plain, saved)

    try:
        fork_safety._after_in_child()

        assert sys.stderr is stderr
        assert plain.stream is stderr
        assert not stderr.closed
        plain.emit(record("left as it was"))
        bound_back = [bound_now(who, INHERITED, raw_of(stderr)) for who in ("sys.stderr", named(plain), named(saved))]
    finally:
        stderr.close()
    assert reported(path.read_text(encoding="utf-8")) == (
        [
            f"TypeError: sys.stderr, {named(plain)}, {named(saved)}: not rebuilt, {named(saved)} reads back"
            f" {stderr!r} as its stream, not the stream rebuilt for it",
            f"OSError: [Errno {errno.EIO}] {os.strerror(errno.EIO)}",
            _closing(fd, "the raw layer built"),
            *bound_back,
        ],
        "left as it was\n",
    )


@pytest.mark.parametrize("closing_it", ["raises from its flush", "runs a close of its own"])
def test_every_inherited_raw_layer_over_a_descriptor_is_closed_whatever_closing_one_of_them_does(
    register: Register,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    closing_it: str,
):
    path = tmp_path / "file.log"
    owning = logging.FileHandler(path, encoding="utf-8")
    owning_old = text_stream(owning.stream)
    fd = owning_old.fileno()
    borrowing = logging.StreamHandler(open(fd, "w", encoding="utf-8", closefd=False))  # noqa: SIM115
    borrowing_old = text_stream(borrowing.stream)
    inherited_raws = raw_of(owning_old), raw_of(borrowing_old)
    register(owning, borrowing)
    attribute = "flush" if closing_it == "raises from its flush" else "close"
    monkeypatch.setattr(inherited_raws[0], attribute, _flush_raising if attribute == "flush" else lambda: None)
    # A line the borrowing stream holds unflushed: a write the parent was in the middle of.
    borrowing_old.write("parent half-line\n")

    fork_safety._after_in_child()

    try:
        if attribute == "close":
            assert capfd.readouterr().err == ""
        else:
            assert reported_on(capfd) == [
                "RuntimeError: flush failed",
                _closing_inherited(fd),
                *_rebuilt_now(owning, borrowing),
            ]
        assert all(raw.closed for raw in inherited_raws)
        assert owning.stream is not owning_old
        assert borrowing.stream is not borrowing_old
        # Dropped, the inherited borrowing stream's finaliser finds its raw layer closed and writes nothing.
        del borrowing_old
        gc.collect()
        owning.emit(record("child line"))
    finally:
        _drop_copied_attribute(owning.stream, attribute)
        owning.close()
        borrowing.stream.close()
    assert path.read_text(encoding="utf-8") == "child line\n"


# A raw layer that does not own the descriptor, closed before it, frees no number.
@pytest.mark.parametrize("borrowed_first", [False, True])
def test_a_close_that_finds_its_descriptor_closed_before_the_hook_freed_it_is_reported(
    register: Register,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    borrowed_first: bool,
):
    path = tmp_path / "file.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    old = text_stream(handler.stream)
    fd = old.fileno()
    borrowing = [logging.StreamHandler(open(fd, "w", encoding="utf-8", closefd=False))] if borrowed_first else []  # noqa: SIM115
    closed_beneath: list[None] = []

    def flush_closing_the_descriptor() -> None:
        if not closed_beneath:
            closed_beneath.append(None)
            os.close(fd)

    # ``FileIO.close`` calls ``flush`` before it closes the descriptor, which is then already closed.
    monkeypatch.setattr(raw_of(old), "flush", flush_closing_the_descriptor)
    register(*borrowing, handler)

    fork_safety._after_in_child()

    try:
        assert reported_on(capfd) == [
            f"OSError: [Errno {errno.EBADF}] {os.strerror(errno.EBADF)}",
            _closing_inherited(fd),
            *_rebuilt_now(*borrowing, handler),
        ]
        assert not raw_of(text_stream(handler.stream)).closed
        assert raw_of(old).closed
        handler.emit(record("child line"))
    finally:
        _drop_copied_attribute(handler.stream, "flush")
        handler.close()
        for each in borrowing:
            each.stream.close()
    assert path.read_text(encoding="utf-8") == "child line\n"


def test_what_a_close_raises_before_the_ebadf_of_a_descriptor_already_freed_is_reported(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    fd = os.open(tmp_path / "file.log", os.O_WRONLY | os.O_CREAT)
    first = logging.StreamHandler(open(fd, "w", encoding="utf-8"))  # noqa: SIM115
    second = logging.StreamHandler(open(fd, "w", encoding="utf-8"))  # noqa: SIM115
    # The first close frees the number, so the second one's close of it raises ``EBADF``, chained to
    # what its ``flush`` raised before.
    monkeypatch.setattr(raw_of(text_stream(second.stream)), "flush", _flush_raising)
    register(first, second)

    fork_safety._after_in_child()

    try:
        assert reported_on(capfd) == [
            "RuntimeError: flush failed",
            _closing_inherited(fd),
            *_rebuilt_now(first, second),
        ]
    finally:
        _drop_copied_attribute(second.stream, "flush")
        text_stream(first.stream).close()
        # Both rebuilt streams own the descriptor, which the first one closes.
        with pytest.raises(OSError, match=os.strerror(errno.EBADF)):
            text_stream(second.stream).close()


def _dup2_failing(fd: int, fd2: int, inheritable: bool = True) -> int:
    raise OSError(errno.EBUSY, os.strerror(errno.EBUSY))


def _hook_failing_to_put_back(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Run the hook with ``os.dup2`` failing; return what was unraisable meanwhile.

    Garbage left by earlier tests is collected first, so every unraisable recorded comes from this hook.
    """
    gc.collect()
    unraisable: list[object] = []
    with monkeypatch.context() as patch:
        patch.setattr(sys, "unraisablehook", unraisable.append)
        patch.setattr(os, "dup2", _dup2_failing)
        fork_safety._after_in_child()
        gc.collect()
    return unraisable


def _put_back_failed(fd: int) -> str:
    return f"raised putting descriptor {fd} back"


def _inherited_now(stream: io.TextIOWrapper, *binders: str) -> list[str]:
    """The report's line for each of ``binders`` bound to its inherited ``stream``, read now."""
    return [bound_now(binder, INHERITED, raw_of(stream)) for binder in binders]


_EBUSY = f"OSError: [Errno {errno.EBUSY}] {os.strerror(errno.EBUSY)}"


def test_a_descriptor_that_cannot_be_put_back_leaves_its_handler_on_its_inherited_stream(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    handler = logging.FileHandler(tmp_path / "file.log", encoding="utf-8")
    old = text_stream(handler.stream)
    fd = old.fileno()
    register(handler)

    unraisable = _hook_failing_to_put_back(monkeypatch)

    assert reported_on(capfd) == [_EBUSY, _put_back_failed(fd), *_inherited_now(old, named(handler))]
    assert handler.stream is old
    assert old.closed
    assert unraisable == []
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(fd)


def test_a_descriptor_no_raw_layer_owns_is_never_held_as_a_duplicate_so_its_rebuild_never_puts_it_back(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    path = tmp_path / "borrowed.log"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT)
    handler = logging.StreamHandler(open(fd, "w", encoding="utf-8", closefd=False))  # noqa: SIM115
    old = text_stream(handler.stream)
    register(handler)

    try:
        unraisable = _hook_failing_to_put_back(monkeypatch)

        assert capfd.readouterr().err == ""
        assert unraisable == []
        assert old.closed
        new = text_stream(handler.stream)
        assert new is not old
        handler.emit(record("after the rebuild"))
        new.close()
    finally:
        os.close(fd)
    assert path.read_text(encoding="utf-8") == "after the rebuild\n"


def test_a_descriptor_that_cannot_be_put_back_reports_each_error_closing_a_raw_layer_over_it(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    handler = logging.FileHandler(tmp_path / "file.log", encoding="utf-8")
    old = text_stream(handler.stream)
    fd = old.fileno()
    # The raw layer the hook builds copies this ``flush``, so closing either raw layer raises.
    monkeypatch.setattr(raw_of(old), "flush", _flush_raising)
    register(handler)

    unraisable = _hook_failing_to_put_back(monkeypatch)

    assert reported_on(capfd) == [
        "RuntimeError: flush failed",
        _closing_inherited(fd),
        _EBUSY,
        _put_back_failed(fd),
        "RuntimeError: flush failed",
        _closing(fd, "a raw layer"),
        *_inherited_now(old, named(handler)),
    ]
    assert handler.stream is old
    assert old.closed
    assert unraisable == []
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(fd)


@pytest.mark.parametrize("rolled_back_by", ["a handler that does not read back", "a raise"])
def test_a_descriptor_that_cannot_be_put_back_after_a_rollback_leaves_no_stream_open_over_it(
    register: Register,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    rolled_back_by: str,
):
    stream = open(tmp_path / "file.log", "w", encoding="utf-8")  # noqa: SIM115
    fd = stream.fileno()
    handler = logging.StreamHandler(stream)
    if rolled_back_by == "a raise":

        def raise_reading_back(bindings: object, rebuilt: object) -> NotReadBack | None:
            raise RuntimeError("reading back failed")

        monkeypatch.setattr(fork_safety, "first_not_read_back", raise_reading_back)
        register(handler)
        everyone = [named(handler)]
        rolled_back = [
            "RuntimeError: reading back failed",
            rebuilding(named(handler)),
        ]
    else:
        saved = SavedStreamHandler(stream)
        register(handler, saved)
        everyone = [named(handler), named(saved)]
        reason = f"{named(saved)} reads back {stream!r} as its stream, not the stream rebuilt for it"
        rolled_back = [f"TypeError: {', '.join(everyone)}: not rebuilt, {reason}"]

    try:
        unraisable = _hook_failing_to_put_back(monkeypatch)

        assert reported_on(capfd) == [*rolled_back, _EBUSY, _put_back_failed(fd), *_inherited_now(stream, *everyone)]
        assert handler.stream is stream
        assert stream.closed
        assert unraisable == []
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(fd)
    finally:
        stream.close()


def _written_through_raises(write: Callable[[], object]) -> None:
    with pytest.raises(ValueError, match="closed file"):
        write()


@pytest.mark.parametrize("owner", ["sys.stderr", "the handler"])
def test_a_descriptor_that_cannot_be_put_back_closes_every_buffered_writer_over_it(
    register: Register,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    owner: str,
):
    fd = os.open(tmp_path / "stderr.log", os.O_WRONLY | os.O_CREAT)
    if owner == "sys.stderr":
        stderr = open(fd, "w", encoding="utf-8")  # noqa: SIM115
        dunder = open(fd, "w", encoding="utf-8", closefd=False)  # noqa: SIM115
        handler = logging.StreamHandler(dunder)
        register(logging.lastResort, handler)
    else:
        stderr = dunder = open(fd, "w", encoding="utf-8", closefd=False)  # noqa: SIM115
        handler = logging.StreamHandler(open(fd, "w", encoding="utf-8"))  # noqa: SIM115
        register(handler)
    other = tmp_path / "other.log"

    with monkeypatch.context() as patch:
        patch.setattr(sys, "stderr", stderr)
        patch.setattr(sys, "__stderr__", dunder)
        try:
            unraisable = _hook_failing_to_put_back(monkeypatch)
            # The number is free: a file opened now may take it.
            os.dup2(os.open(other, os.O_WRONLY | os.O_CREAT), fd)

            _written_through_raises(lambda: handler.stream.write("handler line\n"))
            _written_through_raises(lambda: print("print to sys.stderr", file=sys.stderr))
            _written_through_raises(lambda: sys.__stderr__.write("sys.__stderr__ line\n"))  # pyright: ignore[reportOptionalMemberAccess]
            assert unraisable == []
            assert capfd.readouterr().err == ""
        finally:
            os.close(fd)
    assert other.read_bytes() == b""


def test_a_descriptor_that_cannot_be_put_back_is_reported_once_with_every_buffered_writer_over_it(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    fd = os.open(tmp_path / "file.log", os.O_WRONLY | os.O_CREAT)
    owning = logging.StreamHandler(open(fd, "w", encoding="utf-8"))  # noqa: SIM115
    borrowing = logging.StreamHandler(open(fd, "w", encoding="utf-8", closefd=False))  # noqa: SIM115
    register(owning, borrowing)
    other = tmp_path / "other.log"

    try:
        unraisable = _hook_failing_to_put_back(monkeypatch)
        os.dup2(os.open(other, os.O_WRONLY | os.O_CREAT), fd)

        assert reported_on(capfd) == [
            _EBUSY,
            _put_back_failed(fd),
            *_inherited_now(text_stream(owning.stream), named(owning)),
            *_inherited_now(text_stream(borrowing.stream), named(borrowing)),
        ]
        assert raw_of(text_stream(owning.stream)).closed
        assert raw_of(text_stream(borrowing.stream)).closed
        _written_through_raises(lambda: owning.stream.write("owning line\n"))
        _written_through_raises(lambda: borrowing.stream.write("borrowing line\n"))
        assert unraisable == []
    finally:
        os.close(fd)
    assert other.read_bytes() == b""


def test_a_descriptor_that_cannot_be_put_back_closes_a_buffered_writer_subclass_over_it(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    fd = os.open(tmp_path / "file.log", os.O_WRONLY | os.O_CREAT)
    owning = logging.StreamHandler(open(fd, "w", encoding="utf-8"))  # noqa: SIM115
    subclassed = logging.StreamHandler(
        io.TextIOWrapper(BufferedWriterSubclass(io.FileIO(fd, "w", closefd=False)), encoding="utf-8")
    )
    register(owning, subclassed)
    other = tmp_path / "other.log"

    try:
        unraisable = _hook_failing_to_put_back(monkeypatch)
        os.dup2(os.open(other, os.O_WRONLY | os.O_CREAT), fd)

        assert reported_on(capfd) == [
            f"TypeError: {named(subclassed)}: not rebuilt, its buffered writer is a BufferedWriterSubclass over a"
            " FileIO",
            _EBUSY,
            _put_back_failed(fd),
            *_inherited_now(text_stream(owning.stream), named(owning)),
            *_inherited_now(text_stream(subclassed.stream), named(subclassed)),
        ]
        assert raw_of(text_stream(subclassed.stream)).closed
        _written_through_raises(lambda: subclassed.stream.write("subclass line\n"))
        assert unraisable == []
    finally:
        os.close(fd)
    assert other.read_bytes() == b""


def test_a_descriptor_that_cannot_be_put_back_beneath_sys_stderr_leaves_the_report_to_sys_dunder_stderr(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    last_resort = logging.lastResort
    assert last_resort is not None
    register(last_resort)
    fd = os.open(tmp_path / "stderr.log", os.O_WRONLY | os.O_CREAT)
    stderr = open(fd, "w", encoding="utf-8")  # noqa: SIM115
    dunder_path = tmp_path / "dunder.log"
    dunder = open(dunder_path, "w", encoding="utf-8")  # noqa: SIM115
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "__stderr__", dunder)
    dup2 = os.dup2

    def dup2_failing_onto_stderr(source: int, target: int, inheritable: bool = True) -> int:
        if target == fd:
            raise OSError(errno.EBUSY, os.strerror(errno.EBUSY))
        return dup2(source, target, inheritable)

    gc.collect()
    unraisable: list[object] = []
    with monkeypatch.context() as patch:
        patch.setattr(sys, "unraisablehook", unraisable.append)
        patch.setattr(os, "dup2", dup2_failing_onto_stderr)
        fork_safety._after_in_child()
        gc.collect()

    try:
        assert sys.stderr is stderr
        assert stderr.closed
        assert sys.__stderr__ is dunder, "a stream only sys names write to is rebuilt"
        assert unraisable == []
        bound_back = _inherited_now(stderr, "sys.stderr", named(last_resort))
    finally:
        dunder.close()
    assert reported(dunder_path.read_text(encoding="utf-8")) == ([_EBUSY, _put_back_failed(fd), *bound_back], "")


class _ClosedStreamRefusingHandler(logging.StreamHandler[TextIO]):
    """A handler that raises when its ``stream`` is set to a closed stream."""

    def __setattr__(self, name: str, value: object) -> None:
        if name == "stream" and getattr(value, "closed", False):
            raise ValueError("a closed stream is refused")
        super().__setattr__(name, value)


@pytest.mark.parametrize("another_handler", [True, False])
def test_a_handler_that_raises_when_put_back_after_a_failed_put_back_is_named_and_every_other_is_put_back(
    register: Register,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    another_handler: bool,
):
    fd = os.open(tmp_path / "file.log", os.O_WRONLY | os.O_CREAT)
    refusing = _ClosedStreamRefusingHandler(open(fd, "w", encoding="utf-8"))  # noqa: SIM115
    refusing_old = text_stream(refusing.stream)
    register(refusing)
    borrowing = None
    if another_handler:
        borrowing = logging.StreamHandler(open(fd, "w", encoding="utf-8", closefd=False))  # noqa: SIM115
        register(borrowing)
    borrowing_old = None if borrowing is None else borrowing.stream
    other = tmp_path / "other.log"

    try:
        unraisable = _hook_failing_to_put_back(monkeypatch)
        os.dup2(os.open(other, os.O_WRONLY | os.O_CREAT), fd)

        assert reported_on(capfd) == [
            _EBUSY,
            _put_back_failed(fd),
            "ValueError: a closed stream is refused",
            f"raised putting {named(refusing)} back on its inherited stream",
            *_rebuilt_now(refusing),
            *([] if borrowing is None else _inherited_now(text_stream(borrowing.stream), named(borrowing))),
        ]
        assert raw_of(text_stream(refusing.stream)).closed
        # Left on the stream rebuilt for it, which is closed with every other stream over the descriptor.
        assert refusing.stream is not refusing_old
        _written_through_raises(lambda: refusing.stream.write("refusing line\n"))
        if borrowing is not None:
            assert borrowing.stream is borrowing_old
            _written_through_raises(lambda: borrowing.stream.write("borrowing line\n"))
        assert unraisable == []
    finally:
        os.close(fd)
    assert other.read_bytes() == b""


def test_a_handler_that_refuses_the_rebind_is_not_put_back_after_a_failed_put_back(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    fd = os.open(tmp_path / "file.log", os.O_WRONLY | os.O_CREAT)
    old = open(fd, "w", encoding="utf-8")  # noqa: SIM115
    fixed = StreamFixedHandler(old)
    fixed.set_name("fixed")
    register(fixed)
    other = tmp_path / "other.log"

    try:
        unraisable = _hook_failing_to_put_back(monkeypatch)
        os.dup2(os.open(other, os.O_WRONLY | os.O_CREAT), fd)

        assert reported_on(capfd) == [
            f"TypeError: {named(fixed)}: not rebuilt, {named(fixed)} reads back {old!r} as its stream, not the stream"
            " rebuilt for it; it declined the assignment: AttributeError: the stream is fixed",
            _EBUSY,
            _put_back_failed(fd),
            *_inherited_now(old, named(fixed)),
        ]
        assert fixed.stream is old
        assert old.closed
        assert unraisable == []
    finally:
        os.close(fd)
    assert other.read_bytes() == b""


def test_a_descriptor_that_cannot_be_put_back_leaves_a_raw_layer_subclass_over_it_open_and_reports_what_it_reads(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    fd = os.open(tmp_path / "file.log", os.O_WRONLY | os.O_CREAT)
    old = open(fd, "w", encoding="utf-8")  # noqa: SIM115
    owning = logging.StreamHandler(old)
    owning.set_name("owning")
    sub_raw = FileIOSubclass(fd, "w", closefd=False)
    over_the_subclass = io.TextIOWrapper(io.BufferedWriter(sub_raw), encoding="utf-8")
    subclassed = logging.StreamHandler(over_the_subclass)
    subclassed.set_name("subclassed")
    register(owning, subclassed)

    try:
        unraisable = _hook_failing_to_put_back(monkeypatch)

        assert reported_on(capfd) == [
            _EBUSY,
            _put_back_failed(fd),
            *_inherited_now(old, named(owning)),
            f"TypeError: {named(subclassed)}: not rebuilt, its buffered writer is a BufferedWriter over a"
            " FileIOSubclass",
        ]
        assert owning.stream is old
        assert old.closed
        assert subclassed.stream is over_the_subclass
        assert not sub_raw.closed
        assert unraisable == []
    finally:
        over_the_subclass.close()


def test_a_handler_that_refuses_the_rollback_is_not_put_back_again_after_a_failed_put_back(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    fd = os.open(tmp_path / "file.log", os.O_WRONLY | os.O_CREAT)
    old = open(fd, "w", encoding="utf-8")  # noqa: SIM115
    refusing = OnceRebindableHandler(old)
    refusing.set_name("refusing")
    # A handler that does not read back the stream rebuilt for it, so the rebuild is rolled back.
    saved = SavedStreamHandler(old)
    saved.set_name("saved")
    register(refusing, saved)
    written_by = f"{named(refusing)}, {named(saved)}"

    try:
        unraisable = _hook_failing_to_put_back(monkeypatch)

        refusing_now = text_stream(refusing.stream)
        assert refusing_now is not old
        assert reported_on(capfd) == [
            f"TypeError: {written_by}: not rebuilt, {named(saved)} reads back {old!r} as its stream, not the stream"
            " rebuilt for it",
            "AttributeError: the stream was set once already",
            f"raised putting {named(refusing)} back on its inherited stream",
            _EBUSY,
            _put_back_failed(fd),
            bound_now(named(refusing), REBUILT, raw_of(refusing_now)),
            *_inherited_now(old, named(saved)),
        ]
        assert raw_of(refusing_now).closed
        assert old.closed
        assert unraisable == []
    finally:
        old.close()
