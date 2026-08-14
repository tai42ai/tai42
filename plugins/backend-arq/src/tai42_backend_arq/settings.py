"""Settings and job (de)serializers for the arq backend.

``ArqSettings`` reads the ``ARQ_`` env group and mixes in
:class:`~tai42_kit.backend.BackendDispatchSettings`, the surface the host and
every execution backend must agree on for tool dispatch to meet
(``manifest_key`` / ``task_timeout`` / ``tool_name_arg``): the names, defaults
and reload classes are declared once there, under this group's own prefix.

The job (de)serializers define this backend's JSON wire format. A result payload
may carry values JSON cannot encode (above all the exception arq stores as a
failed job's result), so the serializer describes such a value in place as a
tagged mapping and the deserializer revives a failed result's tag into an
exception (``asyncio.CancelledError`` for a stored abort, :class:`TaskFailedError`
otherwise). Job payloads stay strict: an unserializable argument raises.

The key helpers cover only this backend's own Redis namespace (schedule hashes
and locks); arq's own keys use its public constants and ``Job`` API.
"""

from __future__ import annotations

import asyncio
import traceback
from collections.abc import Mapping
from typing import Any, ClassVar

import orjson
from pydantic_core import to_jsonable_python
from pydantic_settings import SettingsConfigDict
from tai42_kit.backend import BackendDispatchSettings
from tai42_kit.settings import DefaultNamespaceMixin, TaiBaseSettings, settings_cache

# Marker key of the tagged in-place description an unserializable value serializes to.
UNSERIALIZABLE_KEY = "__tai_unserializable__"

# The fields a tagged description always carries besides the marker key.
_TAG_FIELDS = frozenset({"type", "repr", "cancelled", "traceback"})

# The full key set of arq's ``serialize_result`` payload dict; a job payload
# carries only a subset, so the full set identifies the result-store path.
_RESULT_PAYLOAD_KEYS = frozenset({"t", "f", "a", "k", "et", "s", "r", "st", "ft", "q", "id"})


class TaskFailedError(Exception):
    """A task's stored failure, revived from the tagged description its exception
    serialized to. Carries the original exception's type name, ``repr``, and
    formatted traceback text when one was attached."""

    def __init__(self, error_type: str, error_repr: str, traceback_text: str | None) -> None:
        super().__init__(error_repr if traceback_text is None else f"{error_repr}\n{traceback_text}")
        self.error_type = error_type
        self.error_repr = error_repr
        self.traceback_text = traceback_text


def _describe_unserializable(value: Any) -> dict[str, Any]:
    """The tagged in-place description of a value JSON cannot encode."""
    is_exception = isinstance(value, BaseException)
    return {
        UNSERIALIZABLE_KEY: True,
        "type": type(value).__qualname__,
        "repr": repr(value),
        "cancelled": isinstance(value, asyncio.CancelledError),
        "traceback": (
            "".join(traceback.format_exception(value)) if is_exception and value.__traceback__ is not None else None
        ),
    }


def _is_result_payload(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.keys() >= _RESULT_PAYLOAD_KEYS


def _is_tagged(value: Any) -> bool:
    return isinstance(value, dict) and value.get(UNSERIALIZABLE_KEY) is True


def _revive_failure(tag: dict[str, Any]) -> BaseException:
    """Revive a failed result's tagged description into an exception instance."""
    missing = _TAG_FIELDS - tag.keys()
    if missing:
        raise ValueError(f"tagged unserializable result is missing fields {sorted(missing)}: {tag}")
    if tag["cancelled"]:
        return asyncio.CancelledError(tag["repr"])
    return TaskFailedError(tag["type"], tag["repr"], tag["traceback"])


def job_serializer(payload: Any) -> bytes:
    """Serialize a job payload to JSON bytes (pydantic-aware, via orjson).

    In a result payload, a value JSON cannot encode serializes to its tagged
    description instead of failing the whole payload. Job payloads stay strict:
    an unserializable argument raises (arq's ``SerializationError`` at enqueue)."""
    if _is_result_payload(payload):
        return orjson.dumps(to_jsonable_python(payload, fallback=_describe_unserializable))
    return orjson.dumps(to_jsonable_python(payload))


def job_deserializer(data: bytes) -> Any:
    """Deserialize JSON job bytes produced by :func:`job_serializer`.

    A failed result whose stored result is a tagged description is revived into
    an exception (``asyncio.CancelledError`` for a stored abort,
    :class:`TaskFailedError` otherwise); a malformed tag raises. A tagged
    description inside a successful result is left as the mapping itself."""
    payload = orjson.loads(data)
    if _is_result_payload(payload) and not payload["s"] and _is_tagged(payload["r"]):
        payload["r"] = _revive_failure(payload["r"])
    return payload


class ArqSettings(BackendDispatchSettings, DefaultNamespaceMixin, TaiBaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARQ_",
    )

    # Connection identity falls back to the shared ``TAI_DEFAULT_*`` namespace
    # when ``ARQ_*`` leaves it unset; a set ``ARQ_`` value always wins.
    tai_default_fields: ClassVar[Mapping[str, str]] = {
        "redis_url": "redis_url",
        "redis_max_connections": "redis_max_connections",
    }

    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int | None = None
    # How long a callback job waits for its predecessor to complete.
    callback_timeout: int = 5

    # Seconds a graceful SIGTERM lets in-flight jobs finish before the worker
    # exits (arq's warm drain). arq CANCELS running jobs on signal unless this is
    # non-zero, so it is what gives arq the warm-drain behavior celery/rq have by
    # default. Must cover the longest job, and the deployment's termination grace
    # period must be >= this, or a drain still severs running work.
    job_completion_wait: int = 300

    def make_redis_settings(self, url: str | None = None) -> Any:
        """Build the arq ``RedisSettings`` for ``url`` (default: ``redis_url``),
        applying the configured connection cap."""
        from arq.connections import RedisSettings

        settings = RedisSettings.from_dsn(url or self.redis_url)
        if self.redis_max_connections:
            settings.max_connections = self.redis_max_connections
        return settings

    @property
    def redis_settings(self) -> Any:
        return self.make_redis_settings()

    @property
    def arq_prefix(self) -> str:
        return "arq:"

    # -- own schedule namespace ------------------------------------------------

    @property
    def arq_schedule_hash(self) -> str:
        return f"{self.arq_prefix}schedule:"

    def arq_schedule_key(self, name: str) -> str:
        return f"{self.arq_schedule_hash}{name}"

    @property
    def arq_schedule_pattern(self) -> str:
        return f"{self.arq_schedule_hash}*"

    def arq_schedule_lock_key(self, name: str) -> str:
        return f"{self.arq_prefix}lock:schedule:{name}"

    def arq_schedule_recovery_lock_key(self, name: str) -> str:
        return f"{self.arq_prefix}schedule_recovery_lock:{name}"


@settings_cache
def arq_settings() -> ArqSettings:
    return ArqSettings()
