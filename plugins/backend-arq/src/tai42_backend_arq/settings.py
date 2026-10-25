"""Settings and job (de)serializers for the arq backend.

``ArqSettings`` reads the ``ARQ_`` env group, carries its Redis connection as
:class:`ArqRedisSettings` (the kit's connection settings under ``ARQ_``), and mixes in
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
from typing import Any

import orjson
from arq.constants import default_queue_name
from pydantic import Field
from pydantic_core import to_jsonable_python
from pydantic_settings import SettingsConfigDict
from tai42_contract.secrets import SecretValue
from tai42_kit.backend import BackendDispatchSettings
from tai42_kit.clients import RedisConnectionSettingsBase
from tai42_kit.settings import TaiBaseSettings, settings_cache

# Marker key of the tagged in-place description an unserializable value serializes to.
UNSERIALIZABLE_KEY = "__tai_unserializable__"

# The fields a tagged description always carries besides the marker key.
_TAG_FIELDS = frozenset({"type", "repr", "cancelled", "traceback"})

# The full key set of arq's ``serialize_result`` payload dict; a job payload
# carries only a subset, so the full set identifies the result-store path.
_RESULT_PAYLOAD_KEYS = frozenset({"t", "f", "a", "k", "et", "s", "r", "st", "ft", "q", "id"})


class TaskFailedError(Exception):
    """A task's stored failure, revived from the tagged description its exception serialized to.

    Carries the original exception's type name, ``repr``, and formatted traceback text when one
    was attached.
    """

    def __init__(self, error_type: str, error_repr: str, traceback_text: str | None) -> None:
        """Record the revived failure's ``error_type``, ``error_repr`` and optional ``traceback_text``."""
        super().__init__(error_repr if traceback_text is None else f"{error_repr}\n{traceback_text}")
        self.error_type = error_type
        self.error_repr = error_repr
        self.traceback_text = traceback_text


def _describe_unserializable(value: Any) -> dict[str, Any]:
    """The tagged in-place description of a value JSON cannot encode.

    A ``SecretValue`` refuses every transport by design: tagging it would leak a
    secret into the result store as a normal-looking value, so it raises instead.
    """
    if isinstance(value, SecretValue):
        raise TypeError("SecretValue refuses serialization transport")
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
    an unserializable argument raises (arq's ``SerializationError`` at enqueue).
    """
    if _is_result_payload(payload):
        return orjson.dumps(to_jsonable_python(payload, fallback=_describe_unserializable))
    return orjson.dumps(to_jsonable_python(payload))


def job_deserializer(data: bytes) -> Any:
    """Deserialize JSON job bytes produced by :func:`job_serializer`.

    A failed result whose stored result is a tagged description is revived into
    an exception (``asyncio.CancelledError`` for a stored abort,
    :class:`TaskFailedError` otherwise); a malformed tag raises. A tagged
    description inside a successful result is left as the mapping itself.
    """
    payload = orjson.loads(data)
    if _is_result_payload(payload) and not payload["s"] and _is_tagged(payload["r"]):
        payload["r"] = _revive_failure(payload["r"])
    return payload


class ArqRedisSettings(RedisConnectionSettingsBase):
    """The arq backend's Redis connection, composed from the kit connection shape under ``ARQ_``.

    ``ARQ_REDIS_URL`` / ``ARQ_REDIS_MAX_CONNECTIONS`` fall back to the shared
    ``TAI_DEFAULT_*`` namespace when unset; the retry and the socket timeouts
    (``ARQ_RETRY_ATTEMPTS``, ``ARQ_RETRY_ON_TIMEOUT``, ``ARQ_SOCKET_TIMEOUT``,
    ``ARQ_SOCKET_CONNECT_TIMEOUT``) are read from ``ARQ_*`` alone.
    """

    model_config = SettingsConfigDict(env_prefix="ARQ_")

    redis_url: str | None = "redis://localhost:6379/0"
    # arq's own connect timeout (seconds), kept as this backend's default.
    socket_connect_timeout: float | None = Field(default=1, gt=0)


class ArqSettings(BackendDispatchSettings, TaiBaseSettings):
    """The ``ARQ_*`` backend config: the Redis connection, queue name, drain timeouts and key namespace."""

    model_config = SettingsConfigDict(
        env_prefix="ARQ_",
    )

    # The Redis connection both sides use, composed from the kit (a field, not a base).
    redis: ArqRedisSettings = Field(default_factory=ArqRedisSettings)
    # The arq queue key both sides bind: the enqueue pool's default target and the
    # consume target of a worker started without a label; a labelled queue is
    # ``<queue_name>.<label>``. It is a full Redis key (arq's own default is the
    # literal ``arq:queue``), so co-tenant deployments on one logical DB set
    # distinct values, none another's value followed by ``.<label>``, to keep
    # each worker consuming only its own jobs.
    queue_name: str = default_queue_name
    # How long a callback job waits for its predecessor to complete.
    callback_timeout: int = 5

    # Seconds a graceful SIGTERM lets in-flight jobs finish before the worker
    # exits (arq's warm drain). arq CANCELS running jobs on signal unless this is
    # non-zero, so it is what gives arq the warm-drain behavior celery/rq have by
    # default. Must cover the longest job, and the deployment's termination grace
    # period must be >= this, or a drain still severs running work.
    job_completion_wait: int = 300

    @property
    def arq_prefix(self) -> str:
        """The Redis key prefix arq itself uses."""
        return "arq:"

    @property
    def arq_queue_registry_key(self) -> str:
        """The Redis set of derived queues this deployment has sent tasks to; the settings queue is implied."""
        return f"{self.queue_name}:queues"

    # -- own schedule namespace ------------------------------------------------

    @property
    def arq_schedule_hash(self) -> str:
        """The Redis hash key prefix for this plugin's schedule entries."""
        return f"{self.arq_prefix}schedule:"

    def arq_schedule_key(self, name: str) -> str:
        """The Redis key for the schedule entry named ``name``."""
        return f"{self.arq_schedule_hash}{name}"

    @property
    def arq_schedule_pattern(self) -> str:
        """The Redis ``SCAN`` glob matching every schedule entry key."""
        return f"{self.arq_schedule_hash}*"

    def arq_schedule_lock_key(self, name: str) -> str:
        """The Redis lock key serializing writers of the schedule named ``name``."""
        return f"{self.arq_prefix}lock:schedule:{name}"

    def arq_schedule_recovery_lock_key(self, name: str) -> str:
        """The Redis lock key serializing recovery of the schedule named ``name``."""
        return f"{self.arq_prefix}schedule_recovery_lock:{name}"


@settings_cache
def arq_settings() -> ArqSettings:
    """The process-cached :class:`ArqSettings`."""
    return ArqSettings()
