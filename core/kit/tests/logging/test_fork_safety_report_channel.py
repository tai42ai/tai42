"""Where the child-side fork hook's report goes: beneath the first standard stream of the two stderr names, or nowhere.

The report is written as bytes on the descriptor beneath the first of ``sys.stderr`` and
``sys.__stderr__`` that is a text stream of the standard library's own layers; no stream object's
own code runs to write it. Each test binds both names itself and registers one handler the hook
cannot rebuild, so the hook has exactly one failure to report.
"""

from __future__ import annotations

import codecs
import errno
import gc
import io
import logging
import os
import select
import socket
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

import pytest

from tai42_kit.logging import fork_report_text, fork_safety

from .conftest import (
    BufferedWriterSubclass,
    FileIOSubclass,
    Register,
    UnreadableBufferText,
    UnreadableTextProxy,
    named,
    raw_of,
    reported,
)

# The first line of the hook's report.
_HEADLINE = fork_report_text._headline(fork_safety.__name__)


def _a_handler_it_cannot_rebuild(register: Register) -> logging.StreamHandler[TextIO]:
    """Register a handler over a buffered writer over a ``BytesIO``, which the hook names as not rebuilt."""
    handler: logging.StreamHandler[TextIO] = logging.StreamHandler(
        io.TextIOWrapper(io.BufferedWriter(io.BytesIO()), encoding="utf-8")
    )
    register(handler)
    return handler


def _not_rebuilt(handler: logging.Handler) -> str:
    return f"TypeError: {named(handler)}: not rebuilt, its buffered writer is a BufferedWriter over a BytesIO"


def _pending(reader: int) -> bytes:
    """Every byte waiting in the pipe ``reader`` reads, without waiting for more."""
    data = b""
    while select.select([reader], [], [], 0)[0]:
        chunk = os.read(reader, 1 << 16)
        if not chunk:
            break
        data += chunk
    return data


@dataclass
class _Pipes:
    """Text streams over pipes, each of the standard library's own layers; every one is closed at the end."""

    readers: list[int] = field(default_factory=list)
    streams: list[io.TextIOWrapper] = field(default_factory=list)

    def stream(self, encoding: str = "utf-8") -> tuple[int, io.TextIOWrapper]:
        """A pipe's read end, and a text stream over its write end."""
        reader, writer = os.pipe()
        stream = open(writer, "w", encoding=encoding)  # noqa: SIM115
        self.readers.append(reader)
        self.streams.append(stream)
        return reader, stream


@pytest.fixture
def pipes() -> Iterator[_Pipes]:
    made = _Pipes()
    yield made
    for stream in made.streams:
        stream.close()
    for reader in made.readers:
        os.close(reader)


class _Proxy:
    """A ``sys.stderr`` that is no text stream: it records every ``write`` and ``flush`` made through it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def write(self, text: str) -> int:
        self.calls.append(f"write {text!r}")
        return len(text)

    def flush(self) -> None:
        self.calls.append("flush")


@dataclass
class _Shape:
    """A ``sys.stderr`` of no standard shape, what reached it, and the report lines reading it adds."""

    stream: object
    received: Callable[[], object]
    read_failures: list[str] = field(default_factory=list)


def _a_proxy(tmp_path: Path) -> _Shape:
    proxy = _Proxy()
    return _Shape(proxy, lambda: proxy.calls)


def _a_codecs_writer(tmp_path: Path) -> _Shape:
    path = tmp_path / "codecs.log"
    target = open(path, "wb")  # noqa: SIM115
    writer = codecs.getwriter("utf-8")(target)

    def received() -> bytes:
        target.close()
        return path.read_bytes()

    return _Shape(writer, received)


def _a_text_stream_with_no_buffered_writer(tmp_path: Path) -> _Shape:
    path = tmp_path / "raw.log"
    stream = io.TextIOWrapper(io.FileIO(path, "w"), encoding="utf-8", write_through=True)

    def received() -> bytes:
        stream.close()
        return path.read_bytes()

    return _Shape(stream, received)


def _a_text_stream_over_a_bytes_io(tmp_path: Path) -> _Shape:
    target = io.BytesIO()
    stream = io.TextIOWrapper(target, encoding="utf-8")

    def received() -> bytes:
        stream.flush()
        return target.getvalue()

    return _Shape(stream, received)


def _a_text_stream_whose_buffer_raises(tmp_path: Path) -> _Shape:
    path = tmp_path / "unreadable.log"
    stream = UnreadableBufferText(open(path, "wb"), encoding="utf-8")  # noqa: SIM115

    def received() -> bytes:
        stream.close()
        return path.read_bytes()

    return _Shape(stream, received)


def _a_text_proxy_whose_buffer_raises(tmp_path: Path) -> _Shape:
    return _Shape(
        UnreadableTextProxy(),
        list,
        ["RuntimeError: the buffer cannot be read", "raised reading the stream of sys.stderr"],
    )


def _a_closed_standard_stream(tmp_path: Path) -> _Shape:
    path = tmp_path / "closed.log"
    stream = open(path, "w", encoding="utf-8")  # noqa: SIM115
    stream.close()
    return _Shape(stream, path.read_bytes)


def _a_detached_standard_stream(tmp_path: Path) -> _Shape:
    path = tmp_path / "detached.log"
    stream = open(path, "w", encoding="utf-8")  # noqa: SIM115
    buffer = stream.detach()

    def received() -> bytes:
        buffer.close()
        return path.read_bytes()

    return _Shape(stream, received)


class _PythonRaw(io.RawIOBase):
    """A raw layer written in Python: every byte written to it is kept in ``received``."""

    def __init__(self) -> None:
        self.received = b""

    def writable(self) -> bool:
        return True

    def write(self, data: object) -> int:
        chunk = bytes(data)  # pyright: ignore[reportArgumentType]
        self.received += chunk
        return len(chunk)


def _a_text_stream_over_a_buffered_writer_subclass(tmp_path: Path) -> _Shape:
    path = tmp_path / "subclass.log"
    stream = io.TextIOWrapper(BufferedWriterSubclass(io.FileIO(path, "w")), encoding="utf-8")

    def received() -> bytes:
        stream.close()
        return path.read_bytes()

    return _Shape(stream, received)


def _a_text_stream_over_a_raw_layer_subclass(tmp_path: Path) -> _Shape:
    path = tmp_path / "raw_subclass.log"
    stream = io.TextIOWrapper(io.BufferedWriter(FileIOSubclass(path, "w")), encoding="utf-8")

    def received() -> bytes:
        stream.close()
        return path.read_bytes()

    return _Shape(stream, received)


def _a_text_stream_over_a_python_raw_layer(tmp_path: Path) -> _Shape:
    raw = _PythonRaw()
    stream = io.TextIOWrapper(io.BufferedWriter(raw), encoding="utf-8")

    def received() -> bytes:
        stream.flush()
        return raw.received

    return _Shape(stream, received)


def _a_text_stream_over_a_socket(tmp_path: Path) -> _Shape:
    ours, peer = socket.socketpair()
    stream = ours.makefile("w", encoding="utf-8")
    ours.close()

    def received() -> bytes:
        stream.close()
        peer.setblocking(False)
        try:
            return peer.recv(1 << 16)
        except BlockingIOError:
            return b""
        finally:
            peer.close()

    return _Shape(stream, received)


def _none(tmp_path: Path) -> _Shape:
    return _Shape(None, list)


_SHAPES = {
    "None": _none,
    "a proxy": _a_proxy,
    "a codecs writer": _a_codecs_writer,
    "a text stream with no buffered writer": _a_text_stream_with_no_buffered_writer,
    "a text stream over a BytesIO": _a_text_stream_over_a_bytes_io,
    "a text stream subclass whose buffer raises": _a_text_stream_whose_buffer_raises,
    "a text proxy whose buffer raises": _a_text_proxy_whose_buffer_raises,
    "a text stream over a buffered writer subclass": _a_text_stream_over_a_buffered_writer_subclass,
    "a text stream over a raw layer subclass": _a_text_stream_over_a_raw_layer_subclass,
    "a text stream over a raw layer written in Python": _a_text_stream_over_a_python_raw_layer,
    "a text stream over a socket": _a_text_stream_over_a_socket,
    "a closed standard stream": _a_closed_standard_stream,
    "a detached standard stream": _a_detached_standard_stream,
}


def _nothing(received: object) -> bool:
    return received in ([], b"")


def test_a_standard_stderr_takes_the_report_on_its_descriptor_below_its_buffer(
    register: Register, monkeypatch: pytest.MonkeyPatch, pipes: _Pipes
):
    reader, stderr = pipes.stream(encoding="utf-16")
    monkeypatch.setattr(sys, "stderr", stderr)
    handler = _a_handler_it_cannot_rebuild(register)
    # A line the stream holds, not yet flushed: a report through the stream would reach the pipe after it.
    stderr.write("held by the stream\n")

    fork_safety._after_in_child()

    assert _pending(reader) == f"{_HEADLINE}\n  {_not_rebuilt(handler)}\n".encode("utf-16")
    assert sys.stderr is stderr


def test_a_character_the_stream_s_encoding_cannot_take_is_written_as_its_escape(
    register: Register, monkeypatch: pytest.MonkeyPatch, pipes: _Pipes
):
    reader, stderr = pipes.stream(encoding="ascii")
    monkeypatch.setattr(sys, "stderr", stderr)
    handler = _a_handler_it_cannot_rebuild(register)
    handler.set_name("caf\u00e9")

    fork_safety._after_in_child()

    escaped = _not_rebuilt(handler).replace("\u00e9", "\\xe9")
    assert _pending(reader) == f"{_HEADLINE}\n  {escaped}\n".encode("ascii")


def test_no_report_is_written_when_the_stream_s_encoding_cannot_encode_it(
    register: Register, monkeypatch: pytest.MonkeyPatch, pipes: _Pipes
):
    # The ``idna`` codec takes no error handler but ``strict``, so it raises on the report.
    reader, stderr = pipes.stream(encoding="idna")
    dunder_reader, dunder = pipes.stream()
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "__stderr__", dunder)
    handler = _a_handler_it_cannot_rebuild(register)

    fork_safety._after_in_child()

    assert handler.stream is not None, "the handler the hook reports on is gone"
    assert (_pending(reader), _pending(dunder_reader)) == (b"", b"")


def test_a_descriptor_that_takes_a_few_bytes_at_a_time_receives_the_whole_report(
    register: Register, monkeypatch: pytest.MonkeyPatch, pipes: _Pipes
):
    reader, stderr = pipes.stream()
    monkeypatch.setattr(sys, "stderr", stderr)
    handler = _a_handler_it_cannot_rebuild(register)
    write = os.write

    def write_a_few_bytes(fd: int, data: bytes) -> int:
        return write(fd, data[:7])

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", write_a_few_bytes)
        fork_safety._after_in_child()

    assert _pending(reader) == f"{_HEADLINE}\n  {_not_rebuilt(handler)}\n".encode()


def test_a_raw_layer_with_a_fileno_of_its_own_takes_the_report_on_its_descriptor_and_nothing_is_raised(
    register: Register, monkeypatch: pytest.MonkeyPatch, pipes: _Pipes
):
    reader, stderr = pipes.stream()
    monkeypatch.setattr(sys, "stderr", stderr)

    def fileno_raising() -> int:
        raise RuntimeError("fileno says no")

    monkeypatch.setattr(raw_of(stderr), "fileno", fileno_raising)
    handler = _a_handler_it_cannot_rebuild(register)

    fork_safety._after_in_child()

    assert _pending(reader) == f"{_HEADLINE}\n  {_not_rebuilt(handler)}\n".encode()


@pytest.mark.parametrize("shape", list(_SHAPES))
def test_a_stderr_of_any_other_shape_is_never_written_and_the_report_goes_beneath_dunder_stderr(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pipes: _Pipes, shape: str
):
    made = _SHAPES[shape](tmp_path)
    reader, dunder = pipes.stream()
    monkeypatch.setattr(sys, "stderr", made.stream)
    monkeypatch.setattr(sys, "__stderr__", dunder)
    handler = _a_handler_it_cannot_rebuild(register)

    try:
        fork_safety._after_in_child()

        assert reported(_pending(reader).decode()) == ([*made.read_failures, _not_rebuilt(handler)], "")
        assert sys.stderr is made.stream
    finally:
        received = made.received()
    assert _nothing(received)


@pytest.mark.parametrize(
    ("stderr_shape", "dunder_shape"),
    [
        ("a proxy", "a text stream over a BytesIO"),
        ("a closed standard stream", "a proxy"),
        ("a codecs writer", "a detached standard stream"),
    ],
)
def test_no_report_is_written_when_neither_stderr_is_a_standard_stream(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stderr_shape: str, dunder_shape: str
):
    stderr, dunder = _SHAPES[stderr_shape](tmp_path), _SHAPES[dunder_shape](tmp_path)
    monkeypatch.setattr(sys, "stderr", stderr.stream)
    monkeypatch.setattr(sys, "__stderr__", dunder.stream)
    handler = _a_handler_it_cannot_rebuild(register)

    try:
        fork_safety._after_in_child()
    finally:
        received = stderr.received(), dunder.received()

    assert handler.stream is not None, "the handler the hook reports on is gone"
    assert all(_nothing(each) for each in received)


def test_no_stderr_and_no_dunder_stderr_take_no_report(
    register: Register, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
):
    handler = _a_handler_it_cannot_rebuild(register)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stderr", None)
        patch.setattr(sys, "__stderr__", None)

        fork_safety._after_in_child()

    assert handler.stream is not None
    assert capfd.readouterr() == ("", "")


def _reader_gone(pipes: _Pipes) -> io.TextIOWrapper:
    reader, writer = os.pipe()
    os.close(reader)
    stream = open(writer, "w", encoding="utf-8")  # noqa: SIM115
    pipes.streams.append(stream)
    return stream


def _closed_beneath(pipes: _Pipes) -> io.TextIOWrapper:
    reader, writer = os.pipe()
    os.close(reader)
    stream = open(writer, "w", encoding="utf-8")  # noqa: SIM115
    os.close(writer)
    return stream


def _would_block(pipes: _Pipes) -> io.TextIOWrapper:
    _, stream = pipes.stream()
    os.set_blocking(stream.fileno(), False)
    for chunk in (b"x" * 4096, b"x"):
        try:
            while True:
                os.write(stream.fileno(), chunk)
        except BlockingIOError:
            pass  # the pipe takes no more of this chunk's size
    return stream


_REFUSALS: dict[str, tuple[Callable[[_Pipes], io.TextIOWrapper], int]] = {
    "its reader is gone": (_reader_gone, errno.EPIPE),
    "it is closed beneath the stream": (_closed_beneath, errno.EBADF),
    "it would block": (_would_block, errno.EAGAIN),
}


@pytest.mark.parametrize("refusal", list(_REFUSALS))
def test_a_descriptor_that_refuses_the_report_ends_it_and_nothing_is_raised(
    register: Register, monkeypatch: pytest.MonkeyPatch, pipes: _Pipes, refusal: str
):
    make, refused_with = _REFUSALS[refusal]
    # ``sys.__stderr__``'s pipe is made first, so it cannot take a number the refusing stream frees.
    dunder_reader, dunder = pipes.stream()
    stderr = make(pipes)
    with pytest.raises(OSError, match=os.strerror(refused_with)):
        os.write(stderr.fileno(), b"x")
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "__stderr__", dunder)
    handler = _a_handler_it_cannot_rebuild(register)

    fork_safety._after_in_child()

    assert handler.stream is not None, "the handler the hook reports on is gone"
    assert sys.stderr is stderr
    assert _pending(dunder_reader) == b"", "the report went on to another stream once its descriptor refused it"
    if refusal == "it is closed beneath the stream":
        with pytest.raises(OSError, match=os.strerror(errno.EBADF)):
            stderr.close()


def test_the_report_is_written_while_the_cyclic_garbage_collector_is_off(
    register: Register, monkeypatch: pytest.MonkeyPatch
):
    collecting: list[bool] = []
    monkeypatch.setattr(fork_safety, "write_report", lambda report: collecting.append(gc.isenabled()))
    handler = _a_handler_it_cannot_rebuild(register)
    assert gc.isenabled()

    fork_safety._after_in_child()

    assert handler.stream is not None, "the handler the hook reports on is gone"
    assert collecting == [False]
    assert gc.isenabled()
