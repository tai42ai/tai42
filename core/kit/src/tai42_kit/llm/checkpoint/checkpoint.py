"""Checkpoint resource and saver creation: one resource per store, a guard and the shared values around every saver.

Every saver a graph checkpoints through is built here from its store's registration
(:mod:`tai42_kit.llm.checkpoint.providers`): the registration opens the store and builds its saver,
and the kit installs the serialization guard and wraps the saver with the shared values
(:mod:`tai42_kit.llm.checkpoint.shared_values`), the same way for every store.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import ormsgpack
from langgraph.checkpoint.base import BaseCheckpointSaver

from tai42_kit.llm.checkpoint.providers import (
    CheckpointProvider,
    checkpoint_provider,
    checkpoint_provider_facts,
    shared_value_threshold,
)
from tai42_kit.llm.checkpoint.store_format import ensure_store_format
from tai42_kit.llm.settings import llm_provider_settings
from tai42_kit.utils.data.json_schema_util import MSGPACK_INT_MAX, MSGPACK_INT_MIN, find_oversized_int

if TYPE_CHECKING:
    from tai42_kit.llm.checkpoint.envelope import CheckpointCodec
    from tai42_kit.llm.checkpoint.ledger import FinishedThreadLedger
    from tai42_kit.llm.checkpoint.retention import ThreadRetention
    from tai42_kit.llm.checkpoint.value_store import CheckpointValueStore

logger = logging.getLogger(__name__)

CleanupFn = Callable[[], Awaitable[None]]


class CheckpointSerializationError(Exception):
    """A checkpoint value could not be stored faithfully, or a stored value could not be read back.

    The guard converts ormsgpack's ``MsgpackEncodeError`` into this typed error so a value
    the serializer cannot encode fails loudly — never a silent pickle fallback, never an
    opaque engine crash. The checkpoint guard owns the storage integer range: an integer
    outside the native msgpack range ``[-2**63, 2**64-1]`` has no encoding, and the message
    NAMES the offending path. A store's codec raises it for a value it cannot store
    faithfully and for a stored value it cannot decode.
    """


class CheckpointDeleteError(Exception):
    """A checkpoint thread still holds stored documents after a delete round that removed none of them."""


def _serialization_error(obj: Any, cause: ormsgpack.MsgpackEncodeError) -> CheckpointSerializationError:
    # The reactive guard fires only after msgpack aborts, so it names the true
    # encode-failure culprit: an integer outside the native msgpack range. A value
    # in (INT64_MAX, MSGPACK_INT_MAX] encoded fine and is not the culprit.
    overflow = find_oversized_int(obj, minimum=MSGPACK_INT_MIN, maximum=MSGPACK_INT_MAX)
    if overflow is not None:
        path, value = overflow
        return CheckpointSerializationError(
            f"checkpoint value at {path} = {value} exceeds the native msgpack integer range "
            f"[{MSGPACK_INT_MIN}, {MSGPACK_INT_MAX}] and has no msgpack integer encoding"
        )
    return CheckpointSerializationError(f"checkpoint value could not be msgpack-encoded: {cause}")


# The suffix of a stored value's type tag when the value holds a reference to a shared value.
REFERENCING_TYPE_SUFFIX: Final = "+ref"


class PreEncodedValue:
    """A checkpoint value the kit already encoded: the saver's serializer returns ``encoded`` as it is.

    ``references``: the value holds a reference to a shared value, so its type tag carries
    :data:`REFERENCING_TYPE_SUFFIX`.
    """

    __slots__ = ("encoded", "references")

    def __init__(self, encoded: tuple[str, bytes], *, references: bool = False) -> None:
        """``encoded``: the ``(type, bytes)`` pair the serializer returns for this value."""
        self.encoded = encoded
        self.references = references


class PreEncodedCheckpoint(dict[str, Any]):
    """A checkpoint the kit already encoded whole, carrying the checkpoint's keys for the library saver.

    The library saver reads the checkpoint's id and copies it before handing it to its serializer;
    the copy keeps the encoding, and the serializer returns ``encoded`` as it is. ``references``: as
    :class:`PreEncodedValue`.
    """

    def __init__(self, checkpoint: Mapping[str, Any], encoded: tuple[str, bytes], *, references: bool = False) -> None:
        """``encoded``: the ``(type, bytes)`` pair the serializer returns for this checkpoint."""
        super().__init__(checkpoint)
        self.encoded = encoded
        self.references = references

    def copy(self) -> PreEncodedCheckpoint:  # pyright: ignore[reportIncompatibleMethodOverride]
        """A shallow copy that keeps the encoding."""
        return PreEncodedCheckpoint(self, self.encoded, references=self.references)


def _referencing(encoded: tuple[str, bytes]) -> tuple[str, bytes]:
    type_, data = encoded
    return type_ + REFERENCING_TYPE_SUFFIX, data


class _GuardedSerializer:
    """Wrap a saver's serializer: its store's codec (when it has one), the encode-failure guard and the reference tag.

    A ``MsgpackEncodeError`` becomes a :class:`CheckpointSerializationError` naming the offending path.
    A :class:`PreEncodedValue` or :class:`PreEncodedCheckpoint` is returned as the kit encoded it. A
    value that holds a reference to a shared value is stored under its type tag with
    :data:`REFERENCING_TYPE_SUFFIX`; reading such a value strips the suffix and announces the
    reference to the read in progress. ``dumps_plain_typed`` and ``dumps_metadata_typed`` encode
    through the inner serializer alone (no codec). All other serializer methods delegate unchanged.
    """

    def __init__(self, inner: Any, codec: CheckpointCodec | None = None) -> None:
        from tai42_kit.llm.checkpoint.chunking import SharedSnapshot
        from tai42_kit.llm.checkpoint.reads import record_reference

        self._inner = inner
        self._codec = codec
        self._shared_snapshot = SharedSnapshot
        self._record_reference = record_reference

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        if isinstance(obj, (PreEncodedValue, PreEncodedCheckpoint)):
            return _referencing(obj.encoded) if obj.references else obj.encoded
        try:
            encoded = (
                self._codec.dumps_typed(self._inner, obj) if self._codec is not None else self._inner.dumps_typed(obj)
            )
        except ormsgpack.MsgpackEncodeError as exc:
            raise _serialization_error(obj, exc) from exc
        return _referencing(encoded) if isinstance(obj, self._shared_snapshot) else encoded

    def dumps_plain_typed(self, obj: Any) -> tuple[str, bytes]:
        try:
            return self._inner.dumps_typed(obj)
        except ormsgpack.MsgpackEncodeError as exc:
            raise _serialization_error(obj, exc) from exc

    def dumps_metadata_typed(self, obj: Any) -> tuple[str, bytes]:
        return self.dumps_plain_typed(obj)

    def dumps(self, obj: Any) -> bytes:
        try:
            return self._inner.dumps(obj)
        except ormsgpack.MsgpackEncodeError as exc:
            raise _serialization_error(obj, exc) from exc

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        type_, payload = data
        if type_.endswith(REFERENCING_TYPE_SUFFIX):
            type_ = type_.removesuffix(REFERENCING_TYPE_SUFFIX)
            self._record_reference(type_)
            data = (type_, payload)
        if self._codec is not None:
            return self._codec.loads_typed(self._inner, data)
        return self._inner.loads_typed(data)

    def loads(self, data: bytes) -> Any:
        return self._inner.loads(data)

    def _revive_if_needed(self, obj: Any) -> Any:
        return self._inner._revive_if_needed(obj)


def _guard_saver_serialization(
    saver: BaseCheckpointSaver, registration: CheckpointProvider, compress_threshold: int | None
) -> BaseCheckpointSaver:
    """Install the store's serializer and codec and the serialization guard on ``saver.serde`` (idempotent).

    Every saver a graph checkpoints through is built through here, so no checkpoint write can reach
    the serializer unguarded.
    """
    if not isinstance(saver.serde, _GuardedSerializer):
        serde, codec = registration.serializer(saver, compress_threshold)
        saver.serde = _GuardedSerializer(serde, codec)
    return saver


@dataclass(frozen=True)
class CheckpointResource:
    """The long-lived resource behind one ``provider::conn_string`` checkpoint store.

    ``handle`` is what the store's registration builds its saver on. ``ledger`` is the store's
    finished-thread ledger and ``values`` its per-thread value store, both kept in the same store as
    the checkpoints.
    """

    provider: str
    handle: Any
    ledger: FinishedThreadLedger
    values: CheckpointValueStore


async def create_checkpoint_resource(
    provider: str,
    conn_string: str | None = None,
) -> tuple[CheckpointResource, CleanupFn]:
    """Open the store ``provider`` names, and check its store format.

    A ``None`` conn string is the store's default connection (its registration raises, naming the
    setting, when it has none). A store holding data written in an older format raises
    :class:`~tai42_kit.llm.checkpoint.store_format.CheckpointStoreFormatError` and is closed.

    The resource is cached for the life of the app by the registry; its connections stay open.
    """
    registration = checkpoint_provider(provider)
    resource, cleanup = await registration.open(conn_string)
    try:
        _same_provider(resource, registration.name)
        await ensure_store_format(registration, resource)
    except BaseException:
        await cleanup()
        raise
    return resource, cleanup


def _same_provider(resource: CheckpointResource, provider: str) -> None:
    if resource.provider != provider:
        raise ValueError(f"checkpoint resource of provider {resource.provider!r} cannot serve provider {provider!r}")


def get_saver_from_resource(provider: str, resource: CheckpointResource) -> BaseCheckpointSaver:
    """The saver a graph checkpoints through on ``resource``: the store's saver, guarded and wrapped.

    This is the one place a saver is built for a store's own retention, so every saver serializes
    through the guard and stores its large values once where sharing is on.
    """
    registration = checkpoint_provider(provider)
    _same_provider(resource, provider)
    return _apply_features(registration, registration.saver(resource), resource.values)


def saver_view_waiting(provider: str, retention: ThreadRetention | None) -> int | None:
    """The waiting minutes of the saver view ``retention`` needs on ``provider``.

    ``None`` when the store's own saver serves it.

    Only a store whose saver stamps the waiting retention on every key it writes (``native_ttl``)
    needs a view, and only for a waiting other than the platform's. Every other store applies a
    declared retention through its finished-thread ledger.
    """
    if retention is None or checkpoint_provider_facts(provider).retention != "native_ttl":
        return None
    if retention.waiting_minutes == llm_provider_settings().checkpoint_retention_waiting_minutes:
        return None
    return retention.waiting_minutes


async def create_saver_with_waiting(
    provider: str, resource: CheckpointResource, waiting_minutes: int
) -> BaseCheckpointSaver:
    """A saver over ``resource`` whose writes carry ``waiting_minutes`` as their key TTL, guarded and wrapped.

    It reads, writes and deletes what the resource's own saver does, its shared values included; only
    the TTL it stamps differs.
    """
    registration = checkpoint_provider(provider)
    _same_provider(resource, provider)
    saver, values = await registration.saver_with_waiting(resource, waiting_minutes)
    return _apply_features(registration, saver, values)


def _apply_features(
    registration: CheckpointProvider, saver: BaseCheckpointSaver, values: CheckpointValueStore
) -> BaseCheckpointSaver:
    from tai42_kit.llm.checkpoint.shared_values import SharedValuesSaver

    compress = llm_provider_settings().checkpoint_inline_compress_bytes
    _guard_saver_serialization(saver, registration, compress)
    return SharedValuesSaver(
        saver,
        registration,
        values,
        shared_value_threshold(registration.name),
        registration.value_encoding(saver, compress),
    )
