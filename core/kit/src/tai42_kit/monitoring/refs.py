"""Store-once references between monitoring records, and their resolution through a reader.

A producer builds a reference with :func:`payload_ref` (and the "produced while nothing
recorded" statement with :func:`unrecorded`); the writer's encoder renders them as the
contract's one-key marker objects. A hand-built marker dict is user data to the encoder and
is refused, so a decoded marker was always written by the writer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import ValidationError
from tai42_contract.monitoring import (
    PAYLOAD_REF_KEY,
    ObservationNotFoundError,
    PayloadRef,
    PayloadRefUnresolvedError,
    TraceNotFoundError,
)

if TYPE_CHECKING:
    from tai42_contract.monitoring import MonitoringObservation, MonitoringReader

__all__ = [
    "PayloadRefValue",
    "UnrecordedValue",
    "escape_pointer_token",
    "is_payload_ref",
    "payload_ref",
    "resolve_refs",
    "unrecorded",
]

_UNAVAILABLE = "(not yet available or lost)"


class PayloadRefValue:
    """The producer-side reference the encoder renders as ``{"$tai42_ref": {…}}``.

    A plain class, not a dataclass: orjson encodes a dataclass itself and would never hand
    it to the encoder's ``default``. Immutable by convention.
    """

    __slots__ = ("ref",)

    def __init__(self, ref: PayloadRef) -> None:
        """Wrap ``ref``."""
        self.ref = ref

    def __eq__(self, other: object) -> bool:
        """Equal when the wrapped references are equal."""
        return isinstance(other, PayloadRefValue) and other.ref == self.ref

    def __hash__(self) -> int:
        """Hash of the wrapped reference."""
        return hash(self.ref)

    def __repr__(self) -> str:
        """Name the wrapped reference."""
        return f"PayloadRefValue({self.ref!r})"


class UnrecordedValue:
    """The producer-side marker the encoder renders as ``{"$tai42_unrecorded": true}``."""

    __slots__ = ()

    def __repr__(self) -> str:
        """Name the marker."""
        return "UnrecordedValue()"


_UNRECORDED = UnrecordedValue()


def escape_pointer_token(token: str) -> str:
    """Escape one RFC 6901 pointer token: ``~`` → ``~0``, ``/`` → ``~1``."""
    return token.replace("~", "~0").replace("/", "~1")


def payload_ref(
    span_id: str,
    field: Literal["input", "output", "metadata"],
    pointer: str = "",
    *,
    trace_id: str | None = None,
) -> PayloadRefValue:
    """A reference to ``pointer`` inside ``field`` of record ``span_id`` (of ``trace_id``, else the carrier's trace)."""
    return PayloadRefValue(PayloadRef(trace_id=trace_id, span_id=span_id, field=field, pointer=pointer))


def unrecorded() -> UnrecordedValue:
    """The marker for a value that exists but was produced while nothing recorded."""
    return _UNRECORDED


def is_payload_ref(value: Any) -> bool:
    """Whether ``value`` is a reference: a :class:`PayloadRefValue`, or a decoded one-key ``$tai42_ref`` object."""
    if isinstance(value, PayloadRefValue):
        return True
    return isinstance(value, dict) and len(value) == 1 and PAYLOAD_REF_KEY in value  # pyright: ignore[reportUnknownArgumentType]


async def resolve_refs(value: Any, reader: MonitoringReader, *, trace_id: str, max_depth: int = 32) -> Any:
    """Return ``value`` with every reference replaced by the value it names, recursively.

    ``trace_id`` is the trace of the record that carries ``value``. Each record is fetched
    once per call. A reference the backend cannot satisfy, a malformed reference and a
    chain deeper than ``max_depth`` raise ``PayloadRefUnresolvedError``; the unrecorded
    marker and every other value are returned unchanged.
    """
    return await _Resolver(reader, max_depth).resolve(value, trace_id, 0)


def _parse_ref(value: Any) -> PayloadRef:
    if isinstance(value, PayloadRefValue):
        ref = value.ref
    else:
        try:
            ref = PayloadRef.model_validate(value[PAYLOAD_REF_KEY])
        except ValidationError as exc:
            raise PayloadRefUnresolvedError(f"malformed reference: {exc}") from exc
    if ref.pointer and not ref.pointer.startswith("/"):
        raise PayloadRefUnresolvedError(f"malformed reference: pointer {ref.pointer!r} must be empty or start with '/'")
    return ref


def _pointer_tokens(pointer: str) -> list[str]:
    if not pointer:
        return []
    return [t.replace("~1", "/").replace("~0", "~") for t in pointer[1:].split("/")]


_MISSING = object()


def _step(current: Any, part: str) -> Any:
    if isinstance(current, dict):
        return current.get(part, _MISSING)  # pyright: ignore[reportUnknownMemberType]
    if isinstance(current, list) and part.isdigit() and (part == "0" or not part.startswith("0")):
        index = int(part)
        items: list[Any] = current  # pyright: ignore[reportUnknownVariableType]
        return items[index] if index < len(items) else _MISSING
    return _MISSING


class _Resolver:
    def __init__(self, reader: MonitoringReader, max_depth: int) -> None:
        self._reader = reader
        self._max_depth = max_depth
        self._cache: dict[tuple[str, str], MonitoringObservation] = {}

    async def resolve(self, value: Any, trace_id: str, depth: int) -> Any:
        if is_payload_ref(value):
            target, target_trace = await self._deref(value, trace_id, depth)
            return await self.resolve(target, target_trace, depth + 1)
        if isinstance(value, dict):
            return {k: await self.resolve(v, trace_id, depth) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
        if isinstance(value, list):
            return [await self.resolve(v, trace_id, depth) for v in value]  # pyright: ignore[reportUnknownVariableType]
        return value

    async def _deref(self, value: Any, trace_id: str, depth: int) -> tuple[Any, str]:
        """The raw value one reference names (its own references unresolved) and the trace it lives in."""
        if depth >= self._max_depth:
            raise PayloadRefUnresolvedError(f"reference chain deeper than {self._max_depth}")
        ref = _parse_ref(value)
        target_trace = ref.trace_id or trace_id
        observation = await self._fetch(target_trace, ref.span_id)
        current: Any = getattr(observation, ref.field)
        for token in _pointer_tokens(ref.pointer):
            while is_payload_ref(current):
                current, target_trace = await self._deref(current, target_trace, depth + 1)
            current = _step(current, token)
            if current is _MISSING:
                raise PayloadRefUnresolvedError(
                    f"reference {target_trace}/{ref.span_id}#{ref.field}{ref.pointer} names a path the record does "
                    f"not hold {_UNAVAILABLE}"
                )
        return current, target_trace

    async def _fetch(self, trace_id: str, span_id: str) -> MonitoringObservation:
        key = (trace_id, span_id)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        try:
            observation = await self._reader.get_observation(trace_id, span_id)
        except (ObservationNotFoundError, TraceNotFoundError) as exc:
            raise PayloadRefUnresolvedError(
                f"reference {trace_id}/{span_id} names a record the backend does not hold {_UNAVAILABLE}"
            ) from exc
        self._cache[key] = observation
        return observation
