"""The channel of the child-side fork hook's report: bytes on the descriptor beneath a standard ``sys.stderr``.

The report goes through no buffered writer and through no stream object's own ``write``: in a child,
running that code may wait on a lock a thread of the parent held at the fork. The report is written
as bytes on the descriptor beneath the first of ``sys.stderr`` and ``sys.__stderr__`` that is a text
stream of the standard library's own layers (an ``io.TextIOWrapper`` over an ``io.BufferedWriter`` or
``io.BufferedRandom`` over an open ``io.FileIO``, each of that exact type), in that stream's encoding,
a character the encoding cannot take written as its backslash escape; a line the stream holds
unflushed stays in its buffer. No report is written when neither name is such a stream (``None``, a
proxy or a ``codecs`` writer, a subclass, a text stream over a raw layer written in Python, over a
socket or with no buffered writer, a closed or detached stream, one the hook closed after its
descriptor failed to be put back), when the encoding's codec raises on the report (``idna``, which
takes no backslash escape), nor when the descriptor refuses the bytes (its reader is gone, it is
closed beneath the stream, it would block), where the ``OSError`` of the write ends the report,
written in part or not at all. The child's streams stand as the hook left them.
"""

from __future__ import annotations

import io
import os
import sys

from tai42_kit.logging.stream_layers import buffer_beneath, exact_buffer, open_file_io, raw_beneath

# The names whose stream takes the report, in the order they are tried.
_STREAM_NAMES = ("stderr", "__stderr__")


def write_report(report: str) -> None:
    """Write ``report`` on the report's channel, or nothing where there is none, as this module describes."""
    channel = _channel()
    if channel is None:
        return
    fd, encoding = channel
    try:
        data = report.encode(encoding, "backslashreplace")
    except Exception:
        # The encoding's codec raises on the report (``idna`` takes no backslash escape): a report in
        # any other encoding would put bytes the stream's reader cannot decode, so none is written.
        return
    try:
        while data:
            data = data[os.write(fd, data) :]
    except OSError:
        # The descriptor refuses the bytes: its reader is gone (EPIPE), it is closed beneath the stream
        # (EBADF), or it would block (EAGAIN). The stream object is no channel, as writing through it
        # may wait on the parent's lock forever. The child's streams stand as the hook left them.
        return


def _channel() -> tuple[int, str] | None:
    """The descriptor and encoding beneath the first of ``sys.stderr`` and ``sys.__stderr__`` with them, or ``None``."""
    for name in _STREAM_NAMES:
        if (channel := _descriptor_of(vars(sys).get(name))) is not None:
            return channel
    return None


def _descriptor_of(stream: object) -> tuple[int, str] | None:
    """The descriptor and encoding beneath ``stream`` when it is a text stream of the standard library's own layers.

    That is an ``io.TextIOWrapper`` over an ``io.BufferedWriter`` or ``io.BufferedRandom`` over an
    open ``io.FileIO``, each of that exact type (:mod:`tai42_kit.logging.stream_layers`), so every
    read runs none of the stream's code and takes no lock; ``None`` for any other stream.
    """
    if type(stream) is not io.TextIOWrapper:
        return None
    buffer = buffer_beneath(stream)  # ``None`` once the text stream is detached
    if not exact_buffer(buffer):
        return None
    opened = open_file_io(raw_beneath(buffer))  # ``None`` once the buffer is detached
    if opened is None:
        return None
    return opened[1], stream.encoding
