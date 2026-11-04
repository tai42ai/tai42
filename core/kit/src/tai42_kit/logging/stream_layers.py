"""The standard library's own stream layers, told apart and read without running any code of theirs.

A layer is told apart by its type alone, compared by ``is`` or looked up in the type's own record
of its bases, which runs no code of that type or its metaclass. The layer beneath a layer of these
types, a subclass's included, is read through the member of the standard library's C type that
holds it, past any attribute of that name the subclass defines, and an open raw layer's descriptor
through ``io.FileIO``'s own ``fileno``: none of these reads runs code of the stream's and none takes
a lock. These are the reads a child's fork hook can make on a stream a thread of the parent held at
the fork. An object of none of these types is told apart by its own ``__class__``, its own code,
which may raise; one that says by it that it is one of these types (a proxy) has the layer beneath
it read by its own ``buffer`` or ``raw``, its own code too.
"""

from __future__ import annotations

import io
from typing import Protocol, TypeGuard

# A buffered writer, the layer of a text stream that holds a lock for the length of each write, as
# the type checker names it.
Buffer = io.BufferedWriter | io.BufferedRandom
# The buffered writer types, the one record every check and read of a buffered writer goes through.
_BUFFER_KINDS: tuple[type[Buffer], ...] = (io.BufferedWriter, io.BufferedRandom)


class RawLayer(Protocol):
    """A buffered writer's raw layer, of any type, as far as a raw layer is read without being rebuilt."""

    @property
    def closed(self) -> bool:
        """Whether the raw layer is closed."""
        ...


# The C member of ``io.TextIOWrapper`` that holds the layer beneath a text stream.
_TEXT_BUFFER = io.TextIOWrapper.__dict__["buffer"]
# Each buffered writer type, with its C member that holds the raw layer beneath it.
_BUFFER_RAWS = tuple((kind, kind.__dict__["raw"]) for kind in _BUFFER_KINDS)


def is_text(stream: object) -> TypeGuard[io.TextIOWrapper]:
    """Whether ``stream`` is an ``io.TextIOWrapper``, of a subclass too, or a proxy for one.

    An object not of this type, a proxy that says it is one included, is told apart by its own
    ``__class__``, which is its own code and may raise.
    """
    return _is_of(stream, (io.TextIOWrapper,))


def is_buffer(buffer: object) -> TypeGuard[Buffer]:
    """Whether ``buffer`` is an ``io.BufferedWriter`` or ``io.BufferedRandom``, of a subclass too, or a proxy for one.

    An object of none of these types, a proxy that says it is one included, is told apart by its
    own ``__class__``, which is its own code and may raise.
    """
    return _is_of(buffer, _BUFFER_KINDS)


def _is_of(layer: object, kinds: tuple[type, ...]) -> bool:
    """Whether ``layer`` is of one of ``kinds`` by its type, else by its own ``__class__``."""
    return _of_type(layer, kinds) or isinstance(layer, kinds)


def _of_type(layer: object, kinds: type | tuple[type, ...]) -> bool:
    """Whether ``layer``'s type is one of ``kinds`` or a subclass, read from the type's own record of its bases."""
    return issubclass(type(layer), kinds)


def exact_buffer(buffer: object) -> TypeGuard[Buffer]:
    """Whether ``buffer`` is an ``io.BufferedWriter`` or ``io.BufferedRandom`` of that exact type.

    Its type is compared by ``is``, which runs no code of a metaclass, as ``==`` or ``in`` can.
    """
    kind = type(buffer)
    return any(kind is exact for exact in _BUFFER_KINDS)


def buffer_beneath(text: io.TextIOWrapper) -> object:
    """The layer beneath ``text``; ``None`` once it is detached.

    An ``io.TextIOWrapper``, of that type or a subclass, is read through the C type's ``buffer``
    member. An object that is none and only says it is one by its ``__class__`` (a proxy) has no
    such member: its own ``buffer`` is read, which is its own code and may raise.
    """
    if _of_type(text, io.TextIOWrapper):
        return _TEXT_BUFFER.__get__(text)
    return text.buffer


def raw_beneath(buffer: Buffer) -> RawLayer | None:
    """The raw layer beneath ``buffer``; ``None`` once it is detached.

    An ``io.BufferedWriter`` or ``io.BufferedRandom``, of that type or a subclass, is read through its
    C type's ``raw`` member. An object that is neither and only says it is one by its ``__class__``
    (a proxy) has no such member: its own ``raw`` is read, which is its own code and may raise.
    """
    for kind, member in _BUFFER_RAWS:
        if _of_type(buffer, kind):
            return member.__get__(buffer)
    return buffer.raw


def open_file_io(raw: object) -> tuple[io.FileIO, int] | None:
    """``raw`` and its descriptor when it is an open ``io.FileIO`` of that exact type; ``None`` for any other.

    The descriptor is read by ``io.FileIO``'s own ``fileno``, past any instance attribute of that
    name.
    """
    if type(raw) is not io.FileIO or raw.closed:
        return None
    return raw, io.FileIO.fileno(raw)
