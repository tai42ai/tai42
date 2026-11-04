"""What the tests of the child-side fork hook's stream rebuild share: their own registry, stream layers, the report.

The hook walks logging's registry of live handlers; each test points it at a registry holding only
the test's own handlers, so no stream of the test session (pytest's capture, another test's file)
is ever rebuilt.
"""

from __future__ import annotations

import io
import logging
import weakref
from collections.abc import Callable
from typing import TextIO

import pytest

from tai42_kit.logging import fork_report_text, fork_safety

Register = Callable[..., None]


@pytest.fixture
def register(monkeypatch: pytest.MonkeyPatch) -> Register:
    """Point the hook at a registry of the test's own handlers; return the function that adds to it."""
    refs: list[weakref.ReferenceType[logging.Handler]] = []
    monkeypatch.setattr(fork_safety, "_handler_refs", refs)

    def add(*handlers: logging.Handler) -> None:
        refs.extend(weakref.ref(handler) for handler in handlers)

    return add


def text_stream(stream: object) -> io.TextIOWrapper:
    assert isinstance(stream, io.TextIOWrapper)
    return stream


def raw_of(stream: io.TextIOWrapper) -> io.FileIO:
    """The raw layer under ``stream``'s buffered writer."""
    buffer = stream.buffer
    assert isinstance(buffer, io.BufferedWriter | io.BufferedRandom)
    raw = buffer.raw
    assert isinstance(raw, io.FileIO)
    return raw


def record(message: str) -> logging.LogRecord:
    return logging.LogRecord("rebuild", logging.WARNING, __file__, 0, message, None, None)


def named(handler: logging.Handler) -> str:
    """How the hook's report names ``handler``: by its type and its name."""
    return f"the {type(handler).__qualname__} named {handler.name!r}"


def rebuilding(*who: str) -> str:
    """The report's line that a step raised rebuilding the stream that each of ``who`` writes to."""
    return f"raised rebuilding the stream of {', '.join(who)}"


# Where the hook's report says a ``sys`` name or handler is bound.
INHERITED = "its inherited stream"
REBUILT = "the stream rebuilt for it"


def bound_now(who: str, where: str, raw: io.FileIO) -> str:
    """The report's line that ``who`` is bound to ``where``, open or closed as the test reads ``raw`` beneath it."""
    return f"{who} is bound to {where}, which is {'closed' if raw.closed else 'open'}"


class SavedStreamHandler(logging.StreamHandler[TextIO]):
    """A handler whose ``stream`` is a reference to ``saved``, kept from construction on."""

    def __init__(self, saved: TextIO) -> None:
        self._saved = saved
        super().__init__(saved)

    @property  # pyright: ignore[reportIncompatibleVariableOverride]
    def stream(self) -> TextIO:
        return self._saved

    @stream.setter
    def stream(self, value: TextIO) -> None:
        del value


class StreamFixedHandler(logging.StreamHandler[TextIO]):
    """A handler whose ``stream``, once set, raises when it is set again."""

    def __setattr__(self, name: str, value: object) -> None:
        if name == "stream" and "stream" in vars(self):
            raise AttributeError("the stream is fixed")
        super().__setattr__(name, value)


class OnceRebindableHandler(logging.StreamHandler[TextIO]):
    """A handler whose ``stream`` can be set once after construction; a later set raises."""

    def __setattr__(self, name: str, value: object) -> None:
        if name == "stream":
            sets = vars(self).get("_stream_sets", 0)
            if sets == 2:
                raise AttributeError("the stream was set once already")
            object.__setattr__(self, "_stream_sets", sets + 1)
        super().__setattr__(name, value)


class BufferedWriterSubclass(io.BufferedWriter):
    """A subclass of the buffered writer type, whose own construction the hook cannot repeat."""


class FileIOSubclass(io.FileIO):
    """A subclass of the raw file layer type, whose own construction the hook cannot repeat."""


class UnreadableBufferText(io.TextIOWrapper):
    """A text stream whose ``buffer`` raises when read."""

    @property
    def buffer(self) -> io.BufferedWriter:  # pyright: ignore[reportIncompatibleMethodOverride]
        raise RuntimeError("the buffer cannot be read")


class UnreadableTextProxy:
    """No text stream, but one by its ``__class__``, as a proxy says; its ``buffer`` raises when read."""

    @property
    def __class__(self) -> type:  # pyright: ignore[reportIncompatibleVariableOverride]
        return io.TextIOWrapper

    @property
    def buffer(self) -> io.BufferedWriter:
        raise RuntimeError("the buffer cannot be read")


class UnreadableBufferProxy:
    """No buffered writer, but one by its ``__class__``, as a proxy says; its ``raw`` raises when read."""

    closed = False

    @property
    def __class__(self) -> type:  # pyright: ignore[reportIncompatibleVariableOverride]
        return io.BufferedWriter

    @property
    def raw(self) -> io.RawIOBase:
        raise RuntimeError("the raw cannot be read")

    def readable(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def reported(text: str) -> tuple[list[str], str]:
    """The lines of the hook's report that starts ``text``, each failure's and each note's, and the text after it."""
    headline, _, rest = text.partition("\n")
    assert headline == fork_report_text._headline(fork_safety.__name__)
    lines: list[str] = []
    while rest.startswith("  "):
        line, _, rest = rest.partition("\n")
        lines.append(line.removeprefix("  "))
    return lines, rest


def reported_on(capfd: pytest.CaptureFixture[str]) -> list[str]:
    """The lines of the hook's report, the one text written to the captured descriptor 2.

    Under ``capfd`` ``sys.stderr`` is the capture's own text stream, of no standard shape, so the
    report goes to the descriptor beneath ``sys.__stderr__``, which the capture holds.
    """
    lines, rest = reported(capfd.readouterr().err)
    assert rest == ""
    return lines
