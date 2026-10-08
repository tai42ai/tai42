"""Checkpoint codecs: the faithful Redis JSON codec and the compressed Postgres blob codec.

The Redis checkpoint saver stores checkpoints as JSON. JSON has no encoding for most Python values a
graph state holds (a ``Decimal``, a ``timedelta``, an int-keyed dict, a tuple, a pydantic model, an
exception, ...), and the saver's own serializer degrades them silently. :class:`RedisFaithfulCodec`
rewrites every such value into a kit-owned envelope before the saver's JSON encoding, and
:class:`FaithfulRedisSerializer` decodes those envelopes on every read path. A value the codec cannot
store faithfully is refused at write; an envelope that cannot be decoded raises at read.

One envelope shape carries every kind::

    {"lc": 2, "type": "constructor", "id": KIT_ENVELOPE_ID, "args": [kind, payload]}

A large value is stored as an ``inflate`` envelope: the JSON text of the rewritten value, zlib
compressed and base64 encoded, so the compressed value lives and expires with its checkpoint.

The Postgres saver stores channel values as msgpack blobs; :class:`BlobCompressCodec` compresses a
blob above the threshold under its own type tag.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import enum
import importlib
import math
import uuid
import zlib
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from contextvars import ContextVar, Token
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

import pydantic
from langchain_core.load.serializable import Serializable
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.checkpoint.redis.jsonplus_redis import JsonPlusRedisSerializer
from langgraph.checkpoint.redis.util import to_storage_safe_id
from langgraph.checkpoint.serde.jsonplus import LC_REVIVER, _is_safe_json_type
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.types import Interrupt, Send
from redisvl.query import CountQuery
from redisvl.query.filter import Tag

from tai42_kit.llm.checkpoint.checkpoint import CheckpointDeleteError, CheckpointSerializationError

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import CheckpointMetadata, CheckpointTuple

KIT_ENVELOPE_ID: Final[tuple[str, ...]] = ("tai42_kit", "llm", "checkpoint", "codec", "value")

CODEC_KINDS: Final[frozenset[str]] = frozenset(
    {
        "int_key_dict",
        "datetime",
        "date",
        "time",
        "timedelta",
        "timezone",
        "decimal",
        "uuid",
        "tuple",
        "set",
        "frozenset",
        "bytes",
        "enum",
        "model",
        "dataclass",
        "exception",
        "inflate",
    }
)

# The type tag of a compressed Postgres blob.
COMPRESSED_BLOB_TYPE: Final = "msgpack+zlib"

_ZLIB_LEVEL: Final = 1

# The decode failures of the checkpoint read in progress. The Redis saver catches every exception
# raised while it revives a checkpoint document and returns the raw value, so the kit records each
# failure here and the saver's read method raises it once the read finishes.
_decode_failures: ContextVar[list[CheckpointSerializationError] | None] = ContextVar(
    "tai42_checkpoint_decode_failures", default=None
)


def _envelope(kind: str, payload: Any) -> dict[str, Any]:
    return {"lc": 2, "type": "constructor", "id": list(KIT_ENVELOPE_ID), "args": [kind, payload]}


def _is_kit_envelope(value: dict[str, Any]) -> bool:
    return value.get("lc") == 2 and value.get("type") == "constructor" and value.get("id") == list(KIT_ENVELOPE_ID)


# --------------------------------------------------------------------------- #
# Exceptions: stored as a readable record, revived as CheckpointedException
# --------------------------------------------------------------------------- #
class CheckpointedException(Exception):  # noqa: N818 -- an exception revived from a checkpoint, not an error kind
    """An exception revived from a checkpoint: its class name, message, attributes and cause chain as data.

    ``record`` is the stored record (plain JSON): ``id`` (module path parts and class name),
    ``message``, ``kwargs`` (the exception's attributes; a value JSON cannot hold is its ``repr()``
    and its name is listed in ``repr_kwargs``), and ``cause`` (the record of the exception it was
    raised from, or ``None``). Storing a ``CheckpointedException`` again stores its record unchanged.
    """

    def __init__(self, record: dict[str, Any]) -> None:
        """Revive the exception from its stored ``record`` (its cause chain revived with it)."""
        super().__init__(record["message"])
        self.record: dict[str, Any] = record
        self.exc_type: str = ".".join(record["id"])
        cause = record.get("cause")
        self.cause: CheckpointedException | None = CheckpointedException(cause) if cause is not None else None
        self.__cause__ = self.cause

    def __str__(self) -> str:
        """The stored message."""
        return self.record["message"]

    def __reduce__(self) -> tuple[Any, ...]:
        """Rebuild from the record (the default reduction passes the message as the record)."""
        return (CheckpointedException, (self.record,))


def _is_json_native(value: Any) -> bool:
    if value is None or type(value) in (bool, int, str):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is dict:
        return all(type(k) is str and _is_json_native(v) for k, v in value.items())
    if type(value) is list:
        return all(_is_json_native(v) for v in value)
    return False


def exception_record(exc: BaseException) -> dict[str, Any]:
    """The JSON record a checkpoint stores for ``exc`` (its revived form is :class:`CheckpointedException`)."""
    return _exception_record(exc, set())


def _exception_record(exc: BaseException, chain: set[int]) -> dict[str, Any]:
    if isinstance(exc, CheckpointedException):
        return exc.record
    chain.add(id(exc))
    kwargs: dict[str, Any] = {}
    repr_kwargs: list[str] = []
    for name, value in vars(exc).items():
        if _is_json_native(value):
            kwargs[name] = value
        elif isinstance(value, BaseException) and id(value) not in chain:
            kwargs[name] = _exception_record(value, chain)
        else:
            kwargs[name] = repr(value)
            repr_kwargs.append(name)
    record: dict[str, Any] = {
        "id": [*type(exc).__module__.split("."), type(exc).__name__],
        "message": str(exc),
        "kwargs": kwargs,
        "repr_kwargs": repr_kwargs,
        "cause": None,
    }
    nxt = exc.__cause__ if exc.__cause__ is not None else (None if exc.__suppress_context__ else exc.__context__)
    if nxt is not None:
        if id(nxt) in chain:
            record["cause_cycle"] = True
        else:
            record["cause"] = _exception_record(nxt, chain)
    return record


def _validated_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise TypeError("an exception record must be an object")
    ident = record.get("id")
    if not isinstance(ident, list) or not ident or not all(isinstance(part, str) for part in ident):
        raise TypeError("an exception record needs an 'id' list of names")
    if not isinstance(record.get("message"), str):
        raise TypeError("an exception record needs a 'message' text")
    if not isinstance(record.get("kwargs"), dict) or not isinstance(record.get("repr_kwargs"), list):
        raise TypeError("an exception record needs 'kwargs' and 'repr_kwargs'")
    cause = record.get("cause")
    if cause is not None:
        _validated_record(cause)
    return record


# --------------------------------------------------------------------------- #
# The write side: one copy-on-write walk
# --------------------------------------------------------------------------- #
def _class_ref(cls: type, path: str) -> list[str]:
    if "<locals>" in cls.__qualname__:
        raise CheckpointSerializationError(
            f"checkpoint value at {path} of type {cls.__qualname__} cannot be stored faithfully on the redis "
            "checkpointer: a class defined inside a function cannot be imported to read it back"
        )
    return [cls.__module__, cls.__qualname__]


def _refuse(value: Any, path: str, reason: str | None = None) -> CheckpointSerializationError:
    message = (
        f"checkpoint value at {path} of type {type(value).__qualname__} cannot be stored faithfully on the redis "
        "checkpointer"
    )
    return CheckpointSerializationError(message if reason is None else f"{message}: {reason}")


def _finite(value: float, path: str) -> float:
    # JSON has no NaN or infinity: the saver's encoder would store either as null.
    if not math.isfinite(value):
        raise _refuse(value, path, f"{value!r} has no JSON form")
    return float.__float__(value)


def _walk_items(items: Any, path: str) -> list[Any]:
    return [_walk(item, f"{path}[{index}]") for index, item in enumerate(items)]


def _walk_dict(value: dict[Any, Any], path: str) -> Any:
    if all(type(key) is str for key in value):
        return {key: _walk(item, f"{path}.{key}") for key, item in value.items()}
    return _envelope(
        "int_key_dict",
        [[_walk(key, f"{path}<key>"), _walk(item, f"{path}[{key!r}]")] for key, item in value.items()],
    )


def _walk_langchain(value: Serializable, path: str) -> Any:
    data = value.to_json()
    if data.get("type") != "constructor":
        raise _refuse(value, path)
    kwargs = data.get("kwargs", {})
    # Read back, the value is revived by LangChain's reviver; refuse here what it would not revive.
    ident = tuple(data["id"])
    allowed = LC_REVIVER.allowed_class_paths
    if allowed is not None and ident not in allowed:
        raise _refuse(value, path, f"LangChain will not revive {ident!r} from a checkpoint")
    if LC_REVIVER.init_validator is not None:
        try:
            LC_REVIVER.init_validator(ident, kwargs)
        except Exception as exc:
            raise _refuse(value, path, f"LangChain will not revive {ident!r} from a checkpoint: {exc}") from exc
    return {**data, "kwargs": {name: _walk(item, f"{path}.{name}") for name, item in kwargs.items()}}


def _walk_scalar(value: Any) -> Any:
    """The envelope of a datetime-family, decimal or uuid value; ``None`` when ``value`` is none of those."""
    if isinstance(value, datetime):
        return _envelope("datetime", value.isoformat())
    if isinstance(value, date):
        return _envelope("date", value.isoformat())
    if isinstance(value, time):
        return _envelope("time", value.isoformat())
    if isinstance(value, timedelta):
        return _envelope("timedelta", [value.days, value.seconds, value.microseconds])
    if isinstance(value, timezone):
        offset = value.utcoffset(None)
        default_name = timezone(offset).tzname(None)
        name = value.tzname(None)
        return _envelope("timezone", [_walk_scalar(offset), None if name == default_name else name])
    if isinstance(value, Decimal):
        return _envelope("decimal", str(value))
    if isinstance(value, uuid.UUID):
        return _envelope("uuid", str(value))
    return None


def _walk(value: Any, path: str) -> Any:  # noqa: C901, PLR0912 -- one ordered dispatch over every kind
    kind = type(value)
    if value is None or kind in (bool, int, str):
        return value
    if kind is float:
        return _finite(value, path)
    if isinstance(value, BaseException):
        return _envelope("exception", exception_record(value))
    if isinstance(value, enum.Enum):
        return _envelope("enum", [*_class_ref(kind, path), _walk(value.value, f"{path}.value")])
    if isinstance(value, Interrupt):
        return Interrupt(value=_walk(value.value, f"{path}.value"), id=value.id, response_schema=value.response_schema)
    if isinstance(value, Send):
        return Send(value.node, _walk(value.arg, f"{path}.arg"))
    if isinstance(value, _DeltaSnapshot):
        return _DeltaSnapshot(_walk(value.value, f"{path}.value"))
    if isinstance(value, Serializable) and value.is_lc_serializable():
        return _walk_langchain(value, path)
    if isinstance(value, pydantic.BaseModel):
        dumped = value.model_dump(by_alias=True, round_trip=True)
        return _envelope("model", [*_class_ref(kind, path), _walk(dumped, path)])
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = {
            field.name: _walk(getattr(value, field.name), f"{path}.{field.name}") for field in dataclasses.fields(value)
        }
        return _envelope("dataclass", [*_class_ref(kind, path), fields])
    scalar = _walk_scalar(value)
    if scalar is not None:
        return scalar
    if kind is tuple:
        return _envelope("tuple", _walk_items(value, path))
    if isinstance(value, frozenset):
        return _envelope("frozenset", _walk_items(value, path))
    if isinstance(value, set):
        return _envelope("set", _walk_items(value, path))
    if isinstance(value, (bytes, bytearray)):
        name = "bytearray" if isinstance(value, bytearray) else "bytes"
        return _envelope("bytes", [name, base64.b64encode(bytes(value)).decode("ascii")])
    if isinstance(value, dict):
        return _walk_dict(value, path)
    if isinstance(value, list):
        return _walk_items(value, path)
    # Both checkpointers' own encoders store every str/int/float subclass as its base value.
    if isinstance(value, str):
        return str.__str__(value)
    if isinstance(value, int):
        return int.__index__(value)
    if isinstance(value, float):
        return _finite(value, path)
    raise _refuse(value, path)


def _is_checkpoint(obj: Any) -> bool:
    return isinstance(obj, dict) and "channel_values" in obj and "channel_versions" in obj


class RedisFaithfulCodec:
    """The write side of the faithful Redis codec, with the inline compressed envelope.

    ``encode`` receives a whole checkpoint (the saver's ``_dump_checkpoint``) or one pending-write
    value (``aput_writes``), never checkpoint metadata.
    """

    def __init__(self, threshold: int | None) -> None:
        """``threshold``: the encoded size in bytes above which a value is compressed; ``None`` never compresses."""
        self._threshold = threshold

    def _compress_level(self, inner: FaithfulRedisSerializer, walked: Any) -> Any:
        if self._threshold is None:
            return walked
        type_, json_bytes = inner.dumps_typed(walked)
        if type_ != "json" or len(json_bytes) <= self._threshold:
            return walked
        text = base64.b64encode(zlib.compress(json_bytes, _ZLIB_LEVEL)).decode("ascii")
        return _envelope("inflate", ["json", text])

    def dumps_typed(self, inner: FaithfulRedisSerializer, obj: Any) -> tuple[str, bytes]:
        """Rewrite ``obj`` into JSON-faithful form and encode it with the Redis serializer."""
        if isinstance(obj, (bytes, bytearray)):
            # A top-level write of bytes is stored by the saver's own bytes types.
            return inner.dumps_typed(obj)
        if _is_checkpoint(obj):
            rewritten = {key: _walk(value, f"$.{key}") for key, value in obj.items() if key != "channel_values"}
            rewritten["channel_values"] = {
                name: self._compress_level(inner, _walk(value, f"$.channel_values.{name}"))
                for name, value in obj["channel_values"].items()
            }
        else:
            rewritten = self._compress_level(inner, _walk(obj, "$"))
        type_, data = inner.dumps_typed(rewritten)
        if type_ not in ("json", "null"):
            raise CheckpointSerializationError("checkpoint value could not be JSON-encoded after the faithful rewrite")
        return type_, data

    def loads_typed(self, inner: FaithfulRedisSerializer, data: tuple[str, bytes]) -> Any:
        """Decode through the Redis serializer, whose reviver decodes every kit envelope."""
        return inner.loads_typed(data)


class BlobCompressCodec:
    """Compress a msgpack blob above the threshold under its own type tag (Postgres)."""

    def __init__(self, threshold: int | None) -> None:
        """``threshold``: the blob size in bytes above which it is compressed; ``None`` never compresses."""
        self._threshold = threshold

    def dumps_typed(self, inner: Any, obj: Any) -> tuple[str, bytes]:
        """Encode ``obj`` with the saver's serializer; a large msgpack blob is compressed."""
        type_, data = inner.dumps_typed(obj)
        if type_ == "msgpack" and self._threshold is not None and len(data) > self._threshold:
            return COMPRESSED_BLOB_TYPE, zlib.compress(data, _ZLIB_LEVEL)
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


class CheckpointCodec(Protocol):
    """A codec the serialization guard installs around a saver's serializer."""

    def dumps_typed(self, inner: Any, obj: Any) -> tuple[str, bytes]:
        """Encode ``obj`` through ``inner``."""
        ...

    def loads_typed(self, inner: Any, data: tuple[str, bytes]) -> Any:
        """Decode ``data`` through ``inner``."""
        ...


# --------------------------------------------------------------------------- #
# The read side: the kit reviver
# --------------------------------------------------------------------------- #
def _import_class(module: Any, qualname: Any) -> Any:
    if not isinstance(module, str) or not isinstance(qualname, str):
        raise TypeError("a class reference needs a module and a qualified name")
    target: Any = importlib.import_module(module)
    for part in qualname.split("."):
        target = getattr(target, part)
    return target


def _subclass_of(module: Any, qualname: Any, base: type) -> Any:
    cls = _import_class(module, qualname)
    if not (isinstance(cls, type) and issubclass(cls, base)):
        raise TypeError(f"{module}.{qualname} is not a {base.__name__}")
    return cls


def _decode_dataclass(payload: Any) -> Any:
    module, qualname, values = payload
    cls = _import_class(module, qualname)
    if not (isinstance(cls, type) and dataclasses.is_dataclass(cls)):
        raise TypeError(f"{module}.{qualname} is not a dataclass")
    if not isinstance(values, dict):
        raise TypeError("dataclass fields must be an object")
    fields = dataclasses.fields(cls)
    obj = cls(**{field.name: values[field.name] for field in fields if field.init})
    for field in fields:
        if not field.init:
            object.__setattr__(obj, field.name, values[field.name])
    return obj


def _expect(payload: Any, kind: type) -> Any:
    if not isinstance(payload, kind):
        raise TypeError(f"payload must be a {kind.__name__}")
    return payload


def _decode_bytes(payload: Any) -> bytes | bytearray:
    name, text = payload
    raw = base64.b64decode(_expect(text, str), validate=True)
    if name == "bytes":
        return raw
    if name == "bytearray":
        return bytearray(raw)
    raise ValueError(f"unknown bytes type {name!r}")


def _decode_timezone(payload: Any) -> timezone:
    offset, name = payload
    _expect(offset, timedelta)
    return timezone(offset) if name is None else timezone(offset, _expect(name, str))


def _decode_timedelta(payload: Any) -> timedelta:
    days, seconds, microseconds = payload
    return timedelta(days=_expect(days, int), seconds=_expect(seconds, int), microseconds=_expect(microseconds, int))


_SIMPLE_DECODERS: Final[dict[str, Callable[[Any], Any]]] = {
    "int_key_dict": lambda p: dict(_expect(p, list)),
    "datetime": lambda p: datetime.fromisoformat(_expect(p, str)),
    "date": lambda p: date.fromisoformat(_expect(p, str)),
    "time": lambda p: time.fromisoformat(_expect(p, str)),
    "timedelta": _decode_timedelta,
    "timezone": _decode_timezone,
    "decimal": lambda p: Decimal(_expect(p, str)),
    "uuid": lambda p: uuid.UUID(_expect(p, str)),
    "tuple": lambda p: tuple(_expect(p, list)),
    "set": lambda p: set(_expect(p, list)),
    "frozenset": lambda p: frozenset(_expect(p, list)),
    "bytes": _decode_bytes,
    "enum": lambda p: _subclass_of(p[0], p[1], enum.Enum)(p[2]),
    "model": lambda p: _subclass_of(p[0], p[1], pydantic.BaseModel).model_validate(p[2]),
    "dataclass": _decode_dataclass,
    "exception": lambda p: CheckpointedException(_validated_record(p)),
}


def _record_failure(error: CheckpointSerializationError) -> None:
    failures = _decode_failures.get()
    if failures is not None:
        failures.append(error)


class FaithfulRedisSerializer(JsonPlusRedisSerializer):
    """The Redis saver's serializer with the kit reviver: every kit envelope is decoded by the kit.

    The write side is the library serializer unchanged. On read, a kit envelope is decoded by the kit
    table; an envelope that cannot be decoded, and any other lc:2 constructor envelope that is not a
    type the library itself marks safe (the kit writes none), raises
    :class:`CheckpointSerializationError` and is recorded for the read in progress.
    """

    def _decode_kit(self, value: dict[str, Any]) -> Any:
        args = value.get("args")
        if not isinstance(args, list) or len(args) != 2:
            raise TypeError("a kit envelope carries [kind, payload]")
        kind, payload = args
        if kind == "inflate":
            tag, text = payload
            if tag != "json":
                raise ValueError(f"unknown inflate encoding {tag!r}")
            return self.loads_typed(("json", zlib.decompress(base64.b64decode(_expect(text, str), validate=True))))
        decoder = _SIMPLE_DECODERS.get(kind) if isinstance(kind, str) else None
        if decoder is None:
            raise ValueError("unknown envelope kind")
        return decoder(payload)

    def _reviver(self, value: dict[str, Any]) -> Any:
        if _is_kit_envelope(value):
            args = value.get("args")
            kind = args[0] if isinstance(args, list) and args else None
            try:
                return self._decode_kit(value)
            except CheckpointSerializationError:
                # A nested envelope inside an inflated value has already recorded its own failure.
                raise
            except (Exception, binascii.Error, zlib.error) as exc:
                error = CheckpointSerializationError(f"checkpoint envelope {kind!r} cannot be decoded: {exc}")
                _record_failure(error)
                raise error from exc
        ident = value.get("id")
        if (
            value.get("lc") == 2
            and value.get("type") == "constructor"
            and not (isinstance(ident, list) and _is_safe_json_type(ident))
        ):
            error = CheckpointSerializationError(
                f"checkpoint envelope {ident!r} cannot be decoded: not a value the kit codec wrote"
            )
            _record_failure(error)
            raise error
        try:
            return super()._reviver(value)
        except Exception as exc:
            error = CheckpointSerializationError(f"checkpoint envelope {ident!r} cannot be decoded: {exc}")
            _record_failure(error)
            raise error from exc

    def _reconstruct_dataclass_constructor(self, obj: dict[str, Any], cls: type[Any]) -> Any:
        # The library rebuilds an lc:2 dataclass envelope before the reviver sees it. The kit writes
        # dataclasses as its own envelope, so a library dataclass envelope goes to the reviver,
        # which refuses it.
        raise TypeError(f"dataclass envelope {obj.get('id')!r} is not a value the kit codec wrote")


# --------------------------------------------------------------------------- #
# The Redis saver: metadata stored as the library stores it; decode failures raise
# --------------------------------------------------------------------------- #
def _open_failures() -> tuple[list[CheckpointSerializationError], Token[Any]]:
    failures: list[CheckpointSerializationError] = []
    return failures, _decode_failures.set(failures)


def _raise_failures(failures: Sequence[CheckpointSerializationError], thread_id: Any) -> None:
    if failures:
        raise CheckpointSerializationError(
            f"checkpoint read for thread {thread_id!r} found {len(failures)} undecodable value(s): {failures[0]}"
        ) from failures[0]


def _thread_of(config: RunnableConfig) -> Any:
    return (config.get("configurable") or {}).get("thread_id")


class GuardedAsyncRedisSaver(AsyncRedisSaver):
    """The Redis checkpoint saver the kit builds.

    Checkpoint metadata is stored exactly as the library stores it: the saver reads metadata back with
    a plain JSON parse and no reviver, so no codec envelope may reach it. Every read raises
    :class:`CheckpointSerializationError` when a stored value could not be decoded — the library
    returns such a value raw on its checkpoint-document path. Deleting a thread removes every
    document it holds.
    """

    async def _thread_documents(self, thread_id: str) -> tuple[int, int]:
        """The number of checkpoint and pending-write documents ``thread_id`` holds, across namespaces."""
        query = CountQuery(filter_expression=Tag("thread_id") == to_storage_safe_id(thread_id))
        checkpoints = await self.checkpoints_index.query(query)
        writes = await self.checkpoint_writes_index.query(query)
        return cast("int", checkpoints), cast("int", writes)

    async def adelete_thread(self, thread_id: str) -> None:
        """Delete every checkpoint and pending write of ``thread_id``.

        One library delete removes at most one search page of checkpoints and one of writes, so the
        delete repeats until the thread holds nothing. A round that does not reduce what remains
        raises :class:`CheckpointDeleteError`.
        """
        previous: int | None = None
        while True:
            await super().adelete_thread(thread_id)
            checkpoints, writes = await self._thread_documents(thread_id)
            remaining = checkpoints + writes
            if remaining == 0:
                return
            if previous is not None and remaining >= previous:
                raise CheckpointDeleteError(
                    f"checkpoint thread {thread_id!r} still holds {checkpoints} checkpoint(s) and {writes} write(s) "
                    "after a delete round that did not reduce them"
                )
            previous = remaining

    def _dump_metadata(self, metadata: CheckpointMetadata) -> str:
        _type, data = self.serde.dumps_metadata_typed(metadata)  # pyright: ignore[reportAttributeAccessIssue]
        return data.decode().replace("\\u0000", "")

    def _recursive_deserialize(self, obj: Any) -> Any:
        # The library's checkpoint-document walk revives lc envelopes and Send markers but returns an
        # Interrupt marker as a plain dict; the serializer's own reviver rebuilds it.
        if isinstance(obj, dict) and obj.get("__interrupt__") is True and "value" in obj:
            return self.serde._revive_if_needed(obj)  # pyright: ignore[reportAttributeAccessIssue]
        return super()._recursive_deserialize(obj)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Read a checkpoint tuple; an undecodable stored value raises."""
        failures, token = _open_failures()
        try:
            result = await super().aget_tuple(config)
        finally:
            _decode_failures.reset(token)
        _raise_failures(failures, _thread_of(config))
        return result

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 -- the library's parameter name
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """List checkpoint tuples; a tuple holding an undecodable stored value raises before it is yielded."""
        inner = cast(
            "AsyncGenerator[CheckpointTuple]", super().alist(config, filter=filter, before=before, limit=limit)
        )
        try:
            while True:
                failures, token = _open_failures()
                try:
                    item = await anext(inner)
                except StopAsyncIteration:
                    return
                finally:
                    _decode_failures.reset(token)
                _raise_failures(failures, _thread_of(item.config))
                yield item
        finally:
            await inner.aclose()
