"""Large checkpoint values stored once per thread: the chunker, the reference and the resolver.

A sub-value whose encoding is larger than the shared-value threshold is stored once per thread in the
saver's value store, keyed by the SHA-256 of its encoding, and the checkpoint or pending write that
carries it holds a reference in its place. The chunker works top down: a node above the threshold has
its children chunked first and is encoded again with their references in place; when it is still
above the threshold it becomes a blob and its place holds a reference. A node at or below the
threshold stays inline.

Only plain dictionaries and lists are descended, and LangGraph's ``Interrupt``, ``Send`` and
``_DeltaSnapshot`` markers through their payload (a marker itself is never replaced). Every other
value, a LangChain-style serialized object included, is replaced whole, so a reference only ever sits
where the store reads back a plain container. Nothing here depends on a channel name or a value's shape.

A reference carries its blob's digest, the blob's encoded length and the digests of every reference
inside the blob, transitively, so a write that carries the reference can keep all of them alive.

A stored value announces, as it is decoded, that it holds a reference; the resolver runs only then. A
``_DeltaSnapshot`` rebuilt around a reference is a :class:`SharedSnapshot`, so the store can mark it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.types import Interrupt, Send

from tai42_kit.llm.checkpoint.checkpoint import CheckpointSerializationError


class CheckpointReferenceError(CheckpointSerializationError):
    """A stored checkpoint references a value its thread's value store does not hold, or holds altered."""


@dataclass(frozen=True, slots=True)
class CheckpointValueRef:
    """A reference to a value stored once in its thread's value store.

    ``digest``: the SHA-256 (hex) of the value's encoding. ``size``: the encoding's length in bytes.
    ``closure``: the digests of every reference inside that value, transitively.
    """

    digest: str
    size: int
    closure: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StoredBlob:
    """A value as its thread's value store holds it: a type tag and the stored bytes."""

    type: str
    data: bytes


class ValueEncoding(Protocol):
    """One store's encoding of checkpoint values, as the chunker and the resolver use it."""

    def encode(self, value: Any) -> tuple[str, bytes]:
        """The ``(type, bytes)`` the store's serializer writes for ``value``."""
        ...

    def reference(self, ref: CheckpointValueRef) -> Any:
        """The value a checkpoint stores in place of the value ``ref`` names."""
        ...

    def as_reference(self, value: Any) -> CheckpointValueRef | None:
        """The reference ``value`` is as the store reads it back, or ``None`` when it is none."""
        ...

    def store_form(self, encoded: tuple[str, bytes]) -> StoredBlob:
        """The blob the value store holds for a value encoded as ``encoded``."""
        ...

    def decode(self, blob: StoredBlob, ref: CheckpointValueRef, thread_id: Any) -> Any:
        """The value ``blob`` holds; a blob whose encoding is not ``ref.size`` long raises."""
        ...


class SharedSnapshot(_DeltaSnapshot):
    """A ``_DeltaSnapshot`` whose payload holds a reference."""

    __slots__ = ()


def _is_serialized_object(value: dict[Any, Any]) -> bool:
    return value.get("lc") in (1, 2) and "type" in value


def _marker_payload(
    value: Any, snapshot: Callable[[Any], Any] = _DeltaSnapshot
) -> tuple[Any, Callable[[Any], Any]] | None:
    """The payload of a LangGraph marker object and a rebuild of the marker around a new payload.

    A ``_DeltaSnapshot`` is rebuilt by ``snapshot``.
    """
    if isinstance(value, Interrupt):
        return value.value, lambda payload: dataclasses.replace(value, value=payload)
    if isinstance(value, Send):
        return value.arg, lambda payload: Send(value.node, payload)
    if isinstance(value, _DeltaSnapshot):
        return value.value, snapshot
    return None


_EMPTY: Final[frozenset[str]] = frozenset()


class ValueChunker:
    """Chunk values above ``threshold`` into blobs; ``None`` keeps every value inline.

    ``blobs`` maps every digest the chunked values reference, at any depth, to its encoding.
    """

    def __init__(self, encoding: ValueEncoding, threshold: int | None) -> None:
        """Chunk with ``encoding``'s serializer above ``threshold`` bytes."""
        self._encoding = encoding
        self._threshold = threshold
        self.blobs: dict[str, tuple[str, bytes]] = {}

    def chunk(self, value: Any) -> tuple[Any, tuple[str, bytes]]:
        """``value`` with its sub-values above the threshold replaced by references, and its encoding."""
        if self._threshold is None:
            return value, self._encoding.encode(value)
        rewritten, encoded, _closure = self._node(value, self._threshold)
        if encoded is None:
            encoded = self._encoding.encode(rewritten)
        return rewritten, encoded

    def _node(self, value: Any, threshold: int) -> tuple[Any, tuple[str, bytes] | None, frozenset[str]]:
        encoded = self._encoding.encode(value)
        if len(encoded[1]) <= threshold:
            return value, encoded, _EMPTY
        closure = _EMPTY
        children = self._children(value, threshold)
        if children is not None:
            rebuilt, closure = children
            if rebuilt is not value:
                value = rebuilt
                encoded = self._encoding.encode(value)
                if len(encoded[1]) <= threshold:
                    return value, encoded, closure
        if _marker_payload(value) is not None:
            return value, encoded, closure
        digest = hashlib.sha256(encoded[1]).hexdigest()
        self.blobs.setdefault(digest, encoded)
        ref = CheckpointValueRef(digest, len(encoded[1]), tuple(sorted(closure)))
        return self._encoding.reference(ref), None, closure | {digest}

    def _children(self, value: Any, threshold: int) -> tuple[Any, frozenset[str]] | None:
        """``value`` with its children chunked (``value`` itself when none changed) and their closure."""
        kind = type(value)
        if kind is dict and not _is_serialized_object(value):
            closure = _EMPTY
            items: dict[Any, Any] = {}
            changed = False
            for key, item in value.items():
                new, _encoded, item_closure = self._node(item, threshold)
                items[key] = new
                closure |= item_closure
                changed = changed or new is not item
            return (items if changed else value), closure
        if kind is list:
            closure = _EMPTY
            elements: list[Any] = []
            changed = False
            for item in value:
                new, _encoded, item_closure = self._node(item, threshold)
                elements.append(new)
                closure |= item_closure
                changed = changed or new is not item
            return (elements if changed else value), closure
        marker = _marker_payload(value, SharedSnapshot)
        if marker is not None:
            payload, rebuild = marker
            new, _encoded, closure = self._node(payload, threshold)
            return (value if new is payload else rebuild(new)), closure
        return None


# --------------------------------------------------------------------------- #
# The read side: resolve every reference a stored checkpoint holds
# --------------------------------------------------------------------------- #
def find_references(value: Any, encoding: ValueEncoding, found: dict[str, CheckpointValueRef]) -> None:
    """Add every reference inside ``value`` to ``found``, by digest."""
    ref = encoding.as_reference(value)
    if ref is not None:
        found.setdefault(ref.digest, ref)
        return
    kind = type(value)
    if kind is dict:
        for item in value.values():
            find_references(item, encoding, found)
    elif kind in (list, tuple):
        for item in value:
            find_references(item, encoding, found)
    else:
        marker = _marker_payload(value)
        if marker is not None:
            find_references(marker[0], encoding, found)


def _substitute(value: Any, encoding: ValueEncoding, lookup: Callable[[str], Any]) -> Any:
    ref = encoding.as_reference(value)
    if ref is not None:
        return lookup(ref.digest)
    kind = type(value)
    if kind is dict:
        items = {key: _substitute(item, encoding, lookup) for key, item in value.items()}
        return value if all(items[key] is item for key, item in value.items()) else items
    if kind in (list, tuple):
        elements = [_substitute(item, encoding, lookup) for item in value]
        if all(new is old for new, old in zip(elements, value, strict=True)):
            return value
        return elements if kind is list else tuple(elements)
    marker = _marker_payload(value)
    if marker is not None:
        payload, rebuild = marker
        new = _substitute(payload, encoding, lookup)
        return value if new is payload else rebuild(new)
    return value


FetchBlobs = Callable[[Collection[str]], Awaitable[Mapping[str, StoredBlob]]]


async def resolve_references(value: Any, encoding: ValueEncoding, fetch: FetchBlobs, thread_id: Any) -> Any:
    """``value`` with every reference inside it, and inside the values it references, resolved.

    The referenced blobs, nested ones included (a reference names them), are fetched in one round trip.
    Every place that references a value receives its own object, as if the value were stored there. A
    blob the store does not hold, or one whose encoding is not the length its reference records, raises
    :class:`CheckpointReferenceError`; nothing is returned partially resolved.
    """
    pending: dict[str, CheckpointValueRef] = {}
    find_references(value, encoding, pending)
    if not pending:
        return value
    blobs: dict[str, StoredBlob] = {}

    async def fetch_missing(digests: Collection[str]) -> None:
        wanted = sorted(digest for digest in digests if digest not in blobs)
        if not wanted:
            return
        fetched = await fetch(wanted)
        missing = [digest for digest in wanted if digest not in fetched]
        if missing:
            raise CheckpointReferenceError(
                f"checkpoint thread {thread_id!r} references a value {missing[0]} the store does not hold; "
                "the thread cannot be read — delete it"
            )
        blobs.update(fetched)

    await fetch_missing({*pending, *(digest for ref in pending.values() for digest in ref.closure)})
    refs: dict[str, CheckpointValueRef] = {}
    decoded: dict[str, Any] = {}
    while pending:
        await fetch_missing(pending)
        refs.update(pending)
        loaded = await asyncio.to_thread(_decode_all, encoding, blobs, pending, thread_id)
        decoded.update(loaded)
        nested: dict[str, CheckpointValueRef] = {}
        for item in loaded.values():
            find_references(item, encoding, nested)
        pending = {digest: ref for digest, ref in nested.items() if digest not in decoded}
    return await asyncio.to_thread(_resolve_all, value, encoding, _Decoded(encoding, blobs, refs, decoded, thread_id))


def _decode_all(
    encoding: ValueEncoding,
    blobs: Mapping[str, StoredBlob],
    refs: Mapping[str, CheckpointValueRef],
    thread_id: Any,
) -> dict[str, Any]:
    return {digest: encoding.decode(blobs[digest], ref, thread_id) for digest, ref in refs.items()}


@dataclass
class _Decoded:
    """The fetched values of one read: the first place a value fills takes its decode, every other a fresh one."""

    encoding: ValueEncoding
    blobs: Mapping[str, StoredBlob]
    refs: Mapping[str, CheckpointValueRef]
    first: dict[str, Any]
    thread_id: Any

    def take(self, digest: str) -> Any:
        if digest in self.first:
            return self.first.pop(digest)
        return self.encoding.decode(self.blobs[digest], self.refs[digest], self.thread_id)


def _resolve_all(value: Any, encoding: ValueEncoding, decoded: _Decoded) -> Any:
    def lookup(digest: str) -> Any:
        return _substitute(decoded.take(digest), encoding, lookup)

    return _substitute(value, encoding, lookup)


async def resolve_tuple(tup: CheckpointTuple, encoding: ValueEncoding, fetch: FetchBlobs) -> CheckpointTuple:
    """``tup`` with every reference in its checkpoint and its pending writes resolved."""
    thread_id = (tup.config.get("configurable") or {}).get("thread_id")
    checkpoint, writes = await resolve_references((tup.checkpoint, tup.pending_writes), encoding, fetch, thread_id)
    if checkpoint is tup.checkpoint and writes is tup.pending_writes:
        return tup
    return tup._replace(checkpoint=checkpoint, pending_writes=writes)
