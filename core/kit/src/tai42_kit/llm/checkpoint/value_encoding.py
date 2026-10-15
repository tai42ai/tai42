"""The msgpack value encoding: a store whose saver writes values through its serializer's msgpack form.

A value is encoded by the saver's own serializer (no codec), exactly as the library stores it, and a
reference is the ``ref`` envelope as a plain dictionary inside the msgpack blob. Any store whose saver
encodes values with ``serde.dumps_typed`` may use it.
"""

from __future__ import annotations

import zlib
from typing import TYPE_CHECKING, Any, Final

from tai42_kit.llm.checkpoint.chunking import CheckpointReferenceError
from tai42_kit.llm.checkpoint.envelope import COMPRESSED_BLOB_TYPE, ZLIB_LEVEL, envelope_reference, reference_envelope
from tai42_kit.llm.checkpoint.value_store import StoredBlob

if TYPE_CHECKING:
    from tai42_kit.llm.checkpoint.chunking import CheckpointValueRef

_MSGPACK: Final = "msgpack"


class MsgpackValueEncoding:
    """A store's msgpack encoding, for the chunker, its value store and the resolver."""

    def __init__(self, serde: Any, compress_threshold: int | None) -> None:
        """Encode through ``serde``'s inner serializer; a value-store blob above ``compress_threshold`` is compressed.

        ``serde`` is the guarded serializer the kit installs on the saver.
        """
        self._serde = serde
        self._compress_threshold = compress_threshold

    def encode(self, value: Any) -> tuple[str, bytes]:
        """The ``(type, bytes)`` the library serializer writes for ``value``."""
        return self._serde.dumps_plain_typed(value)

    def reference(self, ref: CheckpointValueRef) -> Any:
        """The ``ref`` envelope, a plain dictionary in the msgpack blob."""
        return reference_envelope(ref)

    def as_reference(self, value: Any) -> CheckpointValueRef | None:
        """A reference as msgpack reads it back: the ``ref`` envelope dictionary."""
        return envelope_reference(value)

    def store_form(self, encoded: tuple[str, bytes]) -> StoredBlob:
        """The blob, zlib compressed under its own type tag when it is msgpack above the compress threshold."""
        type_, data = encoded
        if type_ == _MSGPACK and self._compress_threshold is not None and len(data) > self._compress_threshold:
            return StoredBlob(COMPRESSED_BLOB_TYPE, zlib.compress(data, ZLIB_LEVEL))
        return StoredBlob(type_, data)

    def decode(self, blob: StoredBlob, ref: CheckpointValueRef, thread_id: Any) -> Any:
        """Decompress and decode ``blob``."""
        type_, data = blob.type, blob.data
        if type_ == COMPRESSED_BLOB_TYPE:
            try:
                data = zlib.decompress(data)
            except zlib.error as exc:
                raise CheckpointReferenceError(
                    f"checkpoint thread {thread_id!r} value {ref.digest} cannot be decompressed: {exc}"
                ) from exc
            type_ = _MSGPACK
        if len(data) != ref.size:
            raise CheckpointReferenceError(
                f"checkpoint thread {thread_id!r} value {ref.digest} is {len(data)} bytes, its reference "
                f"records {ref.size}; the thread cannot be read — delete it"
            )
        return self._serde.loads_typed((type_, data))
