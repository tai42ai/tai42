"""What a text stream is bound to, a name in ``sys`` or a handler, and how a binding is moved and read.

A ``sys`` name is set; a handler is assigned its ``stream``, which runs the handler's own code, and
an ``AttributeError`` from that assignment is the handler declining it (:func:`bind`). Each handler
is then read back (:func:`first_not_read_back`), and the read-back decides whether it is bound to the
stream rebuilt for it. On a rollback, or after a failed descriptor put-back, each binding whose set
returned is put back on its inherited stream, once (:func:`put_back`), where every raise, an
``AttributeError`` included, is the binding refusing it.
The promise these serve is :mod:`tai42_kit.logging.fork_safety`'s.
"""

from __future__ import annotations

import dataclasses
import io
import logging
import sys
from typing import final


@final
@dataclasses.dataclass(frozen=True, slots=True)
class SysName:
    """A name in ``sys`` a text stream is bound to; the hook's own type, so telling one apart runs no foreign code."""

    name: str


SYS_NAMES = tuple(SysName(name) for name in ("stdout", "stderr", "__stdout__", "__stderr__"))
# What a text stream is bound to: a handler, or a name in ``sys``.
Binder = logging.Handler | SysName
Binding = tuple[io.TextIOWrapper, Binder]


@final
@dataclasses.dataclass(frozen=True, slots=True)
class NotReadBack:
    """A handler that does not read back the stream rebuilt for it: the ``stream`` it gives, or what its read raised."""

    handler: logging.Handler
    stream: object
    raised: AttributeError | None


def read_stream(binder: Binder) -> object:
    """The stream ``binder`` is bound to: the ``sys`` name's value, or the handler's ``stream``, its own code.

    What reading a handler's ``stream`` raises propagates, an ``AttributeError`` included.
    """
    return vars(sys).get(binder.name) if type(binder) is SysName else binder.stream  # pyright: ignore[reportAttributeAccessIssue]


def stream_of(binder: Binder) -> object:
    """The stream ``binder`` is bound to (:func:`read_stream`), or ``None`` for a handler that has no ``stream``.

    A handler whose ``stream`` raises ``AttributeError`` when read has none, by ``getattr``'s rule, as
    a ``logging.NullHandler`` and a bare ``logging.Handler`` have none. Any other raise propagates.
    """
    try:
        return read_stream(binder)
    except AttributeError:
        return None


def bind(
    bindings: list[Binding], streams: dict[int, io.TextIOWrapper], moved: list[Binding]
) -> dict[int, AttributeError]:
    """Offer each ``sys`` name and handler in ``bindings`` its stream in ``streams``; return the declines.

    A binding whose set returned is added to ``moved``, the undo log. A handler whose assignment
    raises ``AttributeError`` declines it: the attribute protocol's answer that its ``stream`` is
    not set that way (``logging.lastResort``'s read-only property gives it). It is not in ``moved``,
    so it is never put back, and it is left as the raise leaves it: on its inherited stream, or on the
    stream rebuilt for it when its own code set ``stream`` before it raised. It is returned keyed by
    ``id`` for the read-back's message.
    Any other exception stops the bind and propagates, so ``moved`` holds exactly the bindings whose
    set returned.
    """
    declined: dict[int, AttributeError] = {}
    for binding in bindings:
        text, binder = binding
        try:
            _bind_one(binder, streams[id(text)])
        except AttributeError as error:
            if type(binder) is SysName:
                raise
            declined[id(binder)] = error
            continue
        moved.append(binding)
    return declined


def put_back(moved: list[Binding]) -> list[tuple[Binder, Exception]]:
    """Bind each ``sys`` name and handler in ``moved`` back to its inherited stream; return each that refused.

    One that raises, a handler's own code refusing the stream, is left as the raise leaves it and
    returned with what it raised, and the others are bound on. Each is taken out of ``moved``
    whether its bind returns or raises, so none is put back twice.
    """
    refused: list[tuple[Binder, Exception]] = []
    while moved:
        text, binder = moved.pop(0)
        try:
            _bind_one(binder, text)
        except Exception as error:
            refused.append((binder, error))
    return refused


def first_not_read_back(bindings: list[Binding], rebuilt: dict[int, io.TextIOWrapper]) -> NotReadBack | None:
    """The first handler in ``bindings`` that does not read back the stream rebuilt for it, and what it reads.

    A handler does not read it back when its ``stream`` is another object, or when reading it raises
    ``AttributeError``; any other raise propagates. ``None`` when every handler reads back its
    rebuilt stream.
    """
    for text, binder in bindings:
        if type(binder) is SysName:
            continue
        try:
            stream = read_stream(binder)
        except AttributeError as error:
            return NotReadBack(binder, None, error)
        if stream is not rebuilt[id(text)]:
            return NotReadBack(binder, stream, None)
    return None


def _bind_one(binder: Binder, stream: io.TextIOWrapper) -> None:
    """Bind the ``sys`` name ``binder`` to ``stream``, or assign ``stream`` to the handler ``binder``'s ``stream``.

    The assignment runs the handler's own code.
    """
    if type(binder) is SysName:
        setattr(sys, binder.name, stream)
    else:
        binder.stream = stream  # pyright: ignore[reportAttributeAccessIssue]
