"""Buffered writers built over one raw layer object: the child-side fork hook rebuilds them as one.

Each test points the hook at a registry holding only its own handlers (``register``), and closes
every stream it opens, the rebuilt ones included.
"""

from __future__ import annotations

import io
import logging
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tai42_kit.logging import fork_safety

from .conftest import (
    INHERITED,
    BufferedWriterSubclass,
    Register,
    SavedStreamHandler,
    UnreadableBufferText,
    bound_now,
    named,
    raw_of,
    record,
    reported_on,
    text_stream,
)


def test_buffered_writers_over_one_raw_layer_are_rebuilt_over_one_new_raw_layer_a_sys_only_one_included(
    register: Register, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    path = tmp_path / "file.log"
    raw = io.FileIO(path, "w")
    old = io.TextIOWrapper(io.BufferedWriter(raw), encoding="utf-8")
    stdout = io.TextIOWrapper(io.BufferedWriter(raw), encoding="utf-8")
    handler = logging.StreamHandler(old)
    register(handler)

    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", stdout)
        try:
            fork_safety._after_in_child()

            new_stdout = text_stream(sys.stdout)
            assert capfd.readouterr().err == ""
            assert handler.stream is not old
            assert new_stdout is not stdout
            assert raw_of(new_stdout) is raw_of(text_stream(handler.stream))
            assert raw.closed
            handler.emit(record("handler line"))
            print("print line", file=new_stdout, flush=True)
        finally:
            sys.stdout.close()
            handler.stream.close()
    assert path.read_text(encoding="utf-8") == "handler line\nprint line\n"


def test_a_buffered_writer_over_the_raw_layer_of_one_it_cannot_rebuild_is_left_and_named_with_it(
    register: Register, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    path = tmp_path / "file.log"
    raw = io.FileIO(path, "w")
    exact = io.TextIOWrapper(io.BufferedWriter(raw), encoding="utf-8")
    subclass = io.TextIOWrapper(BufferedWriterSubclass(raw), encoding="utf-8")
    first, second = logging.StreamHandler(exact), logging.StreamHandler(subclass)
    first.name, second.name = "exact", "subclass"
    register(first, second)

    try:
        fork_safety._after_in_child()

        assert reported_on(capfd) == [
            f"TypeError: {named(first)}: not rebuilt, its raw layer is shared with a buffered writer the hook cannot"
            " rebuild",
            f"TypeError: {named(second)}: not rebuilt, its buffered writer is a BufferedWriterSubclass over a FileIO",
        ]
        assert first.stream is exact
        assert second.stream is subclass
        assert not raw.closed
        first.emit(record("exact line"))
        second.emit(record("subclass line"))
    finally:
        exact.close()
        subclass.close()
    assert path.read_text(encoding="utf-8") == "exact line\nsubclass line\n"


def test_buffered_writers_over_one_raw_layer_are_rolled_back_together_and_left_as_they_were(
    register: Register, tmp_path: Path, capfd: pytest.CaptureFixture[str]
):
    path = tmp_path / "file.log"
    raw = io.FileIO(path, "w")
    rebindable = io.TextIOWrapper(io.BufferedWriter(raw), encoding="utf-8")
    saved_stream = io.TextIOWrapper(io.BufferedWriter(raw), encoding="utf-8")
    handler, saved = logging.StreamHandler(rebindable), SavedStreamHandler(saved_stream)
    register(handler, saved)

    try:
        fork_safety._after_in_child()

        assert reported_on(capfd) == [
            f"TypeError: {named(handler)}, {named(saved)}: not rebuilt, {named(saved)} reads back {saved_stream!r} as"
            " its stream, not the stream rebuilt for it",
            bound_now(named(handler), INHERITED, raw),
            bound_now(named(saved), INHERITED, raw),
        ]
        assert handler.stream is rebindable
        assert not raw.closed
        handler.emit(record("rebindable line"))
        saved.emit(record("saved line"))
    finally:
        rebindable.close()
        saved_stream.close()
    assert path.read_text(encoding="utf-8") == "rebindable line\nsaved line\n"


class _UnreadableRawBuffer(io.BufferedWriter):
    """A buffered writer whose ``raw`` raises when read."""

    @property
    def raw(self) -> io.RawIOBase:  # pyright: ignore[reportIncompatibleMethodOverride]
        raise RuntimeError("the raw cannot be read")


class _UnreadableRawRandom(io.BufferedRandom):
    """A buffered reader and writer whose ``raw`` raises when read."""

    @property
    def raw(self) -> io.RawIOBase:  # pyright: ignore[reportIncompatibleMethodOverride]
        raise RuntimeError("the raw cannot be read")


class _UnreadableClassRandom(io.BufferedRandom):
    """A buffered reader and writer whose ``__class__`` raises when read."""

    @property
    def __class__(self) -> type:  # pyright: ignore[reportIncompatibleMethodOverride]
        raise RuntimeError("the class cannot be read")


def _over_a_buffer_of_its_own(raw: io.FileIO, plain: io.TextIOWrapper) -> io.TextIOWrapper:
    return UnreadableBufferText(io.BufferedWriter(raw), encoding="utf-8")


def _over_the_plain_one_s_buffer(raw: io.FileIO, plain: io.TextIOWrapper) -> io.TextIOWrapper:
    return UnreadableBufferText(plain.buffer, encoding="utf-8")


def _over_a_buffer_whose_raw_raises(raw: io.FileIO, plain: io.TextIOWrapper) -> io.TextIOWrapper:
    return io.TextIOWrapper(_UnreadableRawBuffer(raw), encoding="utf-8")


def _over_a_random_buffer_whose_raw_raises(raw: io.FileIO, plain: io.TextIOWrapper) -> io.TextIOWrapper:
    return io.TextIOWrapper(_UnreadableRawRandom(raw), encoding="utf-8")


def _over_a_random_buffer_whose_class_raises(raw: io.FileIO, plain: io.TextIOWrapper) -> io.TextIOWrapper:
    return io.TextIOWrapper(_UnreadableClassRandom(raw), encoding="utf-8")


def _a_subclass(odd: io.TextIOWrapper) -> str:
    return f"{odd!r} is a UnreadableBufferText"


def _a_buffer_subclass(odd: io.TextIOWrapper) -> str:
    return f"its buffered writer is a {type(odd.buffer).__qualname__} over a FileIO"


@pytest.mark.parametrize(
    ("unreadable", "shares_the_buffer", "reason"),
    [
        (_over_a_buffer_of_its_own, False, _a_subclass),
        (_over_the_plain_one_s_buffer, True, _a_subclass),
        (_over_a_buffer_whose_raw_raises, False, _a_buffer_subclass),
        (_over_a_random_buffer_whose_raw_raises, False, _a_buffer_subclass),
        (_over_a_random_buffer_whose_class_raises, False, _a_buffer_subclass),
    ],
)
def test_a_stream_whose_own_buffer_or_raw_raises_is_read_through_its_layers_and_left_whole_with_its_raw_layer(
    register: Register,
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    unreadable: Callable[[io.FileIO, io.TextIOWrapper], io.TextIOWrapper],
    shares_the_buffer: bool,
    reason: Callable[[io.TextIOWrapper], str],
):
    path = tmp_path / "file.log"
    raw = io.FileIO(path, "w+")
    plain = io.TextIOWrapper(io.BufferedWriter(raw), encoding="utf-8")
    odd = unreadable(raw, plain)
    first, second = logging.StreamHandler(plain), logging.StreamHandler(odd)
    first.name, second.name = "plain", "odd"
    register(first, second)

    try:
        fork_safety._after_in_child()

        assert reported_on(capfd) == (
            [f"TypeError: {named(first)}, {named(second)}: not rebuilt, {reason(odd)}"]
            if shares_the_buffer
            else [
                f"TypeError: {named(first)}: not rebuilt, its raw layer is shared with a buffered writer the hook"
                " cannot rebuild",
                f"TypeError: {named(second)}: not rebuilt, {reason(odd)}",
            ]
        )
        assert first.stream is plain
        assert second.stream is odd
        assert not raw.closed
        first.emit(record("plain line"))
        second.emit(record("odd line"))
    finally:
        second.stream.close()
        first.stream.close()
        plain.close()
    assert path.read_text(encoding="utf-8") == "plain line\nodd line\n"
