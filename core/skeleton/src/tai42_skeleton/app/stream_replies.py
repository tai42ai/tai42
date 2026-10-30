"""The replies a session's event stream has written, read from the bytes it sent.

On the SSE transport every call of a session answers on the session's long-lived event
stream. A call's reply counts as written once the stream's ASGI ``send`` of the bytes that
carry it has returned, exactly as a streamable-HTTP reply counts as written once its own
``POST`` response has been sent. :class:`StreamReplies` reads the events the stream writes
(the ``text/event-stream`` format) and settles each call whose JSON-RPC response they carry.
"""

from __future__ import annotations

import codecs
import json
from collections.abc import Callable, Iterator
from typing import Any

__all__ = ["StreamReplies"]


def _line_ends(text: str) -> Iterator[tuple[int, int]]:
    """The ``(start, end)`` span of each line end in ``text``: a CRLF, a lone LF or a lone CR.

    Found with ``str.find``, so a reply of many megabytes on one line is scanned at memory
    speed rather than character by character.
    """
    next_cr, next_lf = text.find("\r"), text.find("\n")
    while next_cr != -1 or next_lf != -1:
        if next_lf == -1 or (next_cr != -1 and next_cr < next_lf):
            start, end = next_cr, (next_cr + 2 if next_lf == next_cr + 1 else next_cr + 1)
        else:
            start, end = next_lf, next_lf + 1
        yield start, end
        if next_cr != -1 and next_cr < end:
            next_cr = text.find("\r", end)
        if next_lf != -1 and next_lf < end:
            next_lf = text.find("\n", end)


class StreamReplies:
    """The calls a session's event stream answers, each settled once the stream has written its reply.

    A call is expected by its JSON-RPC request id before its reply can be handed to the
    stream (:meth:`expect`). The stream reports every body chunk once its ``send`` returned
    (:meth:`wrote`); a ``message`` event that carries a JSON-RPC response settles the first
    call expected under that response's id. When the stream ends no reply can follow, so
    every call still expected is settled (:meth:`end`), as is a call expected after the end.
    """

    def __init__(self) -> None:
        """Start with no call expected, at the start of the stream."""
        self._unwritten: dict[Any, list[Callable[[], None]]] = {}
        self._ended = False
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        # The pieces of a line still open at the end of the last chunk, joined once it ends,
        # so a line spread over many chunks costs its length once rather than once per chunk.
        self._partial_line: list[str] = []
        # The previous chunk ended in CR: an LF opening the next one completes that line end.
        self._after_cr = False
        self._event = ""
        self._data: list[str] = []

    def expect(self, request_id: Any, on_written: Callable[[], None]) -> None:
        """Call ``on_written`` once the stream has written the response to ``request_id``, or has ended."""
        if self._ended:
            on_written()
            return
        self._unwritten.setdefault(request_id, []).append(on_written)

    def wrote(self, body: bytes) -> None:
        """Read the bytes the stream has just written, settling each call whose reply they complete."""
        text = self._decoder.decode(body)
        if not text:
            return
        if self._after_cr and text.startswith("\n"):
            text = text[1:]
        self._after_cr = text.endswith("\r")
        start = 0
        for line_start, line_end in _line_ends(text):
            self._partial_line.append(text[start:line_start])
            self._line("".join(self._partial_line))
            self._partial_line.clear()
            start = line_end
        if start < len(text):
            self._partial_line.append(text[start:])

    def end(self) -> None:
        """Record that the stream ended: every call still expected is settled, and so is any expected later."""
        self._ended = True
        unwritten, self._unwritten = self._unwritten, {}
        for callbacks in unwritten.values():
            for on_written in callbacks:
                on_written()

    def _line(self, line: str) -> None:
        if not line:
            self._dispatch()
            return
        if line.startswith(":"):
            return
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)

    def _dispatch(self) -> None:
        event, data = self._event or "message", self._data
        self._event, self._data = "", []
        if event != "message" or not data or not self._unwritten:
            return
        message = json.loads("\n".join(data))
        # A JSON-RPC response carries the id of the request it answers and no method; a
        # request the server sends carries a method beside an id of the server's own.
        if not isinstance(message, dict) or "method" in message or not ("result" in message or "error" in message):
            return
        callbacks = self._unwritten.get(message.get("id"))
        if not callbacks:
            return
        on_written = callbacks.pop(0)
        if not callbacks:
            del self._unwritten[message["id"]]
        on_written()
