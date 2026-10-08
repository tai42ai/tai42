"""The value types, errors and value codec of the park index (:mod:`tai42_kit.interactions.park_index`)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final, Literal, cast, get_args

from pydantic import BaseModel
from tai42_contract.interactions import ResumeBuffered, RunFailed, SuspendedInteraction

# What a resolved super-step's record says it resolved as: ``terminal`` (the outermost outcome),
# ``suspended`` (the run parked again), ``aborted`` (torn down by an abort or a kill).
Resolution = Literal["terminal", "suspended", "aborted"]
_RESOLUTIONS: Final[frozenset[str]] = frozenset(get_args(Resolution))


class ParkIndexError(RuntimeError):
    """Base of every park index failure."""


class DriveInProgressError(ParkIndexError):
    """Another worker holds the super-step's drive lease.

    Raised (never a benign return) so the platform keeps the caller's durable retry ticket and
    redelivers until the live drive completes or its lease expires.
    """

    def __init__(self, thread_id: str, superstep: str) -> None:
        """Name the super-step."""
        self.thread_id = thread_id
        self.superstep = superstep
        super().__init__(f"a drive of thread {thread_id!r} super-step {superstep!r} is in progress elsewhere")


class LeaseLostError(ParkIndexError):
    """The caller no longer holds the drive lease its write is guarded by; another writer owns the super-step."""

    def __init__(self, thread_id: str, superstep: str) -> None:
        """Name the super-step."""
        self.thread_id = thread_id
        self.superstep = superstep
        super().__init__(f"the drive lease of thread {thread_id!r} super-step {superstep!r} was lost")


class BarrierNotFoundError(ParkIndexError):
    """The super-step barrier an answer converges on is gone (expired or never written)."""

    def __init__(self, thread_id: str, superstep: str) -> None:
        """Name the super-step."""
        self.thread_id = thread_id
        self.superstep = superstep
        super().__init__(f"no park barrier for thread {thread_id!r} super-step {superstep!r}")


class SuperstepAlreadyResolvedError(ParkIndexError):
    """The barrier was finalized while an answer was being buffered into it; the super-step already resolved."""

    def __init__(self, thread_id: str, superstep: str) -> None:
        """Name the super-step."""
        self.thread_id = thread_id
        self.superstep = superstep
        super().__init__(f"thread {thread_id!r} super-step {superstep!r} already resolved")


class ParkIndexCorruptError(ParkIndexError):
    """A stored park index value does not have the shape the index writes."""


class ParkValueCodecError(ParkIndexError):
    """A value cannot be stored in, or read back from, the park index."""


class ResolutionMissingError(ParkIndexError):
    """A resolved tombstone points at a resolution record that is gone."""

    def __init__(self, thread_id: str, superstep: str) -> None:
        """Name the super-step."""
        self.thread_id = thread_id
        self.superstep = superstep
        super().__init__(
            f"thread {thread_id!r} super-step {superstep!r} is tombstoned as resolved but has no resolution record"
        )


_CONTRACT_TYPES: Final[dict[str, type[BaseModel]]] = {
    "suspended_interaction": SuspendedInteraction,
    "resume_buffered": ResumeBuffered,
    "run_failed": RunFailed,
}
_PLAIN_TAG: Final[str] = "value"
_TAG_KEY: Final[str] = "__contract__"


def encode_value(value: Any) -> dict[str, Any]:
    """Encode a resolution value or a buffered answer as a tagged JSON mapping.

    A contract outcome type keeps its type across the store; any other pydantic model is refused,
    and a plain value must be JSON-serializable.

    Raises:
        ParkValueCodecError: The value is a non-contract model or is not JSON-serializable.
    """
    for tag, model in _CONTRACT_TYPES.items():
        if type(value) is model:
            return {_TAG_KEY: tag, "data": value.model_dump(mode="json")}
    if isinstance(value, BaseModel):
        raise ParkValueCodecError(
            f"park index values are JSON values or contract types; got {type(value).__qualname__}"
        )
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise ParkValueCodecError(f"park index value is not JSON-serializable: {exc}") from exc
    return {_TAG_KEY: _PLAIN_TAG, "data": value}


def decode_value(encoded: Any) -> Any:
    """Rebuild a value from its tagged mapping (:func:`encode_value`'s inverse).

    Raises:
        ParkValueCodecError: The mapping carries an unknown or missing tag, or no data.
    """
    tag = encoded.get(_TAG_KEY) if isinstance(encoded, dict) else None
    if not isinstance(encoded, dict) or "data" not in encoded or (tag != _PLAIN_TAG and tag not in _CONTRACT_TYPES):
        raise ParkValueCodecError(f"unknown park value tag {tag!r}")
    if tag == _PLAIN_TAG:
        return encoded["data"]
    return _CONTRACT_TYPES[tag].model_validate(encoded["data"])


def validated_resolution(word: Any) -> Resolution:
    """``word`` as a :data:`Resolution`.

    Raises:
        ParkIndexCorruptError: ``word`` is not a resolution word.
    """
    if word not in _RESOLUTIONS:
        raise ParkIndexCorruptError(f"unknown park resolution {word!r}; expected one of {sorted(_RESOLUTIONS)}")
    return cast("Resolution", word)


@dataclass(frozen=True)
class Barrier:
    """A super-step barrier: the consumer's ``expected`` map, the answers buffered so far, its own fields."""

    expected: dict[str, Any]
    outputs: dict[str, Any]
    fields: dict[str, str]


@dataclass(frozen=True)
class BufferResult:
    """Progress after an answer was buffered: answered members, all members, the still-open members in order."""

    present: int
    total: int
    remaining: list[str]


@dataclass(frozen=True)
class ResolutionRecord:
    """What a resolved super-step stored: how it resolved and the value a redelivery replays."""

    resolution: Resolution
    value: Any
