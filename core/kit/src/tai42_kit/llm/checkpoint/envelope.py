"""The kit's checkpoint envelope, the reference it carries, and the compressed blob codec.

A value the kit stores in a form of its own is an lc:2 constructor envelope::

    {"lc": 2, "type": "constructor", "id": KIT_ENVELOPE_ID, "args": [kind, payload]}

The ``ref`` envelope stands where a value stored once per thread was: its payload is the value's
digest, its encoded size and the digests of every reference inside it. :class:`BlobCompressCodec`
compresses a msgpack blob above a size under its own type tag.
"""

from __future__ import annotations

import zlib
from typing import Any, Final, Protocol

from tai42_kit.llm.checkpoint.checkpoint import CheckpointSerializationError
from tai42_kit.llm.checkpoint.chunking import CheckpointValueRef

KIT_ENVELOPE_ID: Final[tuple[str, ...]] = ("tai42_kit", "llm", "checkpoint", "codec", "value")

# The type tag of a compressed msgpack blob.
COMPRESSED_BLOB_TYPE: Final = "msgpack+zlib"

ZLIB_LEVEL: Final = 1


def envelope(kind: str, payload: Any) -> dict[str, Any]:
    """The kit envelope of ``kind`` carrying ``payload``."""
    return {"lc": 2, "type": "constructor", "id": list(KIT_ENVELOPE_ID), "args": [kind, payload]}


def is_kit_envelope(value: dict[str, Any]) -> bool:
    """Whether ``value`` is a kit envelope."""
    return value.get("lc") == 2 and value.get("type") == "constructor" and value.get("id") == list(KIT_ENVELOPE_ID)


def reference_envelope(ref: CheckpointValueRef) -> dict[str, Any]:
    """The ``ref`` envelope a checkpoint stores in place of the shared value ``ref`` names."""
    return envelope("ref", [ref.digest, ref.size, list(ref.closure)])


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def reference_from_payload(payload: Any) -> CheckpointValueRef:
    """The reference a ``ref`` envelope's payload names; a malformed payload raises ``ValueError``."""
    if not isinstance(payload, list) or len(payload) != 3:
        raise ValueError("a ref envelope carries [digest, size, closure]")
    digest, size, closure = payload
    if not _is_digest(digest):
        raise ValueError("a ref envelope's digest must be 64 lowercase hex characters")
    if type(size) is not int or size < 0:
        raise ValueError("a ref envelope's size must be a non-negative integer")
    if not isinstance(closure, list) or not all(_is_digest(item) for item in closure):
        raise ValueError("a ref envelope's closure must be a list of digests")
    return CheckpointValueRef(digest, size, tuple(closure))


def envelope_reference(value: Any) -> CheckpointValueRef | None:
    """The reference ``value`` is when it is a ``ref`` envelope as stored; ``None`` when it is not one."""
    if type(value) is not dict or not is_kit_envelope(value):
        return None
    args = value.get("args")
    if not isinstance(args, list) or len(args) != 2 or args[0] != "ref":
        return None
    try:
        return reference_from_payload(args[1])
    except ValueError as exc:
        raise CheckpointSerializationError(f"checkpoint envelope 'ref' cannot be decoded: {exc}") from exc


class CheckpointCodec(Protocol):
    """A codec the serialization guard installs around a saver's serializer."""

    def dumps_typed(self, inner: Any, obj: Any) -> tuple[str, bytes]:
        """Encode ``obj`` through ``inner``."""
        ...

    def loads_typed(self, inner: Any, data: tuple[str, bytes]) -> Any:
        """Decode ``data`` through ``inner``."""
        ...


class BlobCompressCodec:
    """Compress a msgpack blob above the threshold under its own type tag."""

    def __init__(self, threshold: int | None) -> None:
        """``threshold``: the blob size in bytes above which it is compressed; ``None`` never compresses."""
        self._threshold = threshold

    def dumps_typed(self, inner: Any, obj: Any) -> tuple[str, bytes]:
        """Encode ``obj`` with the saver's serializer; a large msgpack blob is compressed."""
        type_, data = inner.dumps_typed(obj)
        if type_ == "msgpack" and self._threshold is not None and len(data) > self._threshold:
            return COMPRESSED_BLOB_TYPE, zlib.compress(data, ZLIB_LEVEL)
        return type_, data

    def loads_typed(self, inner: Any, data: tuple[str, bytes]) -> Any:
        """Decode a blob; a compressed one is decompressed first, and a corrupt one raises."""
        type_, payload = data
        if type_ != COMPRESSED_BLOB_TYPE:
            return inner.loads_typed(data)
        try:
            raw = zlib.decompress(payload)
        except zlib.error as exc:
            raise CheckpointSerializationError(f"checkpoint blob cannot be decompressed: {exc}") from exc
        return inner.loads_typed(("msgpack", raw))
