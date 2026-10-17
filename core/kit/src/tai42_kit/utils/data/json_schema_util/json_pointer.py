"""Reading a JSON Pointer array index as RFC 6901 writes one."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import unquote

# RFC 6901: an array index is ``0``, or ASCII digits that do not start with ``0``.
_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")


def json_pointer_array_index(segment: str, *, keyword: str, ref: str) -> int:
    """The array index the JSON Pointer ``segment`` of the reference ``ref`` names.

    ``segment`` is one pointer segment, percent-decoded, reached at an array. Raises ``ValueError``
    naming ``keyword``, ``ref`` and the segment when it is not an index RFC 6901 allows: ``0`` or
    ASCII digits without a leading zero (no sign, no space, no other digits, no ``-``).
    """
    if _ARRAY_INDEX.fullmatch(segment) is None:
        raise ValueError(
            f"{keyword} {ref!r} indexes an array with {segment!r}, which is not a JSON Pointer array index: "
            "write the index as a whole number like 0 or 2"
        )
    return int(segment)


def check_array_indexes(contents: Any, pointer: str, *, keyword: str, ref: str) -> None:
    """Raise ``ValueError`` when the JSON Pointer ``pointer`` (``/…``) indexes an array in ``contents`` wrongly.

    The pointer is read as ``referencing`` reads it: percent-decoded, split on ``/``, with ``~1`` and
    ``~0`` unescaped in an object key. Each segment reaching an array (or another sequence) is held
    to :func:`json_pointer_array_index`. The walk stops where the pointer names no value or reaches
    a scalar; resolving the pointer reports that.
    """
    node = contents
    for segment in unquote(pointer[1:]).split("/"):
        if isinstance(node, Mapping):
            key = segment.replace("~1", "/").replace("~0", "~")
            if key not in node:
                return
            node = node[key]
        elif isinstance(node, Sequence) and not isinstance(node, str):
            index = json_pointer_array_index(segment, keyword=keyword, ref=ref)
            if index >= len(node):
                return
            node = node[index]
        else:
            return
