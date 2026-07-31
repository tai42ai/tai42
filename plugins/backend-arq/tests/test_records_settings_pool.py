"""Schedule records/shape helpers, settings, serializers, and the pool manager."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import orjson
import pytest
from arq.jobs import DeserializationError, SerializationError, deserialize_result, serialize_job, serialize_result
from pydantic import BaseModel, ValidationError

from tai42_backend_arq import records
from tai42_backend_arq.pool import RedisPoolManager
from tai42_backend_arq.records import ScheduleRecord
from tai42_backend_arq.settings import (
    UNSERIALIZABLE_KEY,
    ArqSettings,
    TaskFailedError,
    arq_settings,
    job_deserializer,
    job_serializer,
)

# -- records ---------------------------------------------------------------------


def test_schedule_record_defaults_and_required_fields() -> None:
    record = ScheduleRecord(name="s", schedule={"__type__": "interval", "every": 60.0}, enabled=True)
    assert record.args == []
    assert record.kwargs == {}
    with pytest.raises(ValidationError):
        ScheduleRecord.model_validate({"name": "bad"})


def test_unknown_schedule_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="'interval' or 'crontab'"):
        ScheduleRecord(name="job", schedule={"__type__": "hourly"}, enabled=True)


def test_boolean_every_is_rejected() -> None:
    with pytest.raises(ValueError, match="numeric 'every'"):
        ScheduleRecord(name="job", schedule={"__type__": "interval", "every": True}, enabled=True)


def test_non_bool_relative_is_rejected() -> None:
    with pytest.raises(ValueError, match="'relative' must be a bool"):
        ScheduleRecord(name="job", schedule={"__type__": "interval", "every": 5, "relative": "yes"}, enabled=True)


def test_crontab_missing_fields_are_named() -> None:
    with pytest.raises(ValueError, match="missing required field"):
        ScheduleRecord(name="job", schedule={"__type__": "crontab", "minute": "0"}, enabled=True)


def test_derive_cron_or_interval() -> None:
    assert records.derive_cron_or_interval({"__type__": "interval", "every": 60.0}) == 60
    assert records.derive_cron_or_interval({"__type__": "interval", "every": 1.5}) == 1.5
    crontab = {
        "__type__": "crontab",
        "minute": "0",
        "hour": "9",
        "day_of_month": "*",
        "month_of_year": "*",
        "day_of_week": "1",
    }
    assert records.derive_cron_or_interval(crontab) == "0 9 * * 1"
    with pytest.raises(ValueError, match="Unsupported schedule type"):
        records.derive_cron_or_interval({"__type__": "solar"})


def test_parse_cron_or_interval() -> None:
    assert records.parse_cron_or_interval("60") == 60
    assert isinstance(records.parse_cron_or_interval("60"), int)
    assert records.parse_cron_or_interval("1.5") == 1.5
    assert records.parse_cron_or_interval("0 9 * * 1") == "0 9 * * 1"


def test_next_run_after_interval_and_crontab() -> None:
    now = datetime.now(UTC)
    defer_by, ts = records.next_run_after(60, now)
    assert defer_by == 60.0
    assert ts == now.timestamp() + 60

    defer_by, ts = records.next_run_after("0 9 * * 1", now)
    assert defer_by > 0
    assert ts > now.timestamp()

    with pytest.raises(ValueError, match="cron str or interval number"):
        records.next_run_after(object())  # type: ignore[arg-type]


# -- settings ----------------------------------------------------------------------


def test_settings_defaults_agree_with_host_glue() -> None:
    settings = ArqSettings()
    assert settings.manifest_key == "MANIFEST_KEY"
    assert settings.task_timeout == 300
    assert settings.tool_name_arg == "backend_tool_name"


def test_settings_key_helpers() -> None:
    settings = ArqSettings()
    assert settings.arq_schedule_key("x") == "arq:schedule:x"
    assert settings.arq_schedule_pattern == "arq:schedule:*"
    assert settings.arq_schedule_lock_key("x") == "arq:lock:schedule:x"
    assert settings.arq_schedule_recovery_lock_key("x") == "arq:schedule_recovery_lock:x"


def test_settings_redis_settings_from_url() -> None:
    settings = ArqSettings(redis_url="redis://somehost:6390/2", redis_max_connections=7)
    redis_settings = settings.redis_settings
    assert redis_settings.host == "somehost"
    assert redis_settings.port == 6390
    assert redis_settings.database == 2
    assert redis_settings.max_connections == 7
    override = settings.make_redis_settings("redis://other:6400/1")
    assert override.host == "other"
    assert override.max_connections == 7


def test_arq_settings_accessor_cached() -> None:
    assert arq_settings() is arq_settings()


def test_job_serializer_round_trip() -> None:
    class Payload(BaseModel):
        x: int

    data = job_serializer({"payload": Payload(x=1), "plain": [1, 2]})
    assert job_deserializer(data) == {"payload": {"x": 1}, "plain": [1, 2]}


def _serialize_result(success: bool, result: Any) -> bytes:
    """Store a job outcome exactly as arq's worker does: through
    ``arq.jobs.serialize_result`` with this backend's serializer."""
    data = serialize_result(
        function="tool_execution",
        args=(),
        kwargs={},
        job_try=1,
        enqueue_time_ms=1,
        success=success,
        result=result,
        start_ms=2,
        finished_ms=3,
        ref="j1:tool_execution",
        queue_name="arq:queue",
        job_id="j1",
        serializer=job_serializer,
    )
    assert data is not None
    return data


def test_failed_result_round_trip_revives_stored_failure() -> None:
    """A failed job's exception crosses the real serializer pair as a tagged
    description and reads back as ``TaskFailedError`` carrying the original
    type, repr, and traceback."""
    try:
        raise ValueError("task blew up")
    except ValueError as exc:
        stored = exc

    info = deserialize_result(_serialize_result(False, stored), deserializer=job_deserializer)

    assert info.success is False
    revived = info.result
    assert isinstance(revived, TaskFailedError)
    assert revived.error_type == "ValueError"
    assert revived.error_repr == "ValueError('task blew up')"
    assert revived.traceback_text is not None
    assert "ValueError: task blew up" in revived.traceback_text
    assert "Traceback (most recent call last):" in revived.traceback_text


def test_aborted_result_round_trip_revives_cancellation() -> None:
    """A stored abort (arq stores a ``CancelledError`` as the failed result)
    reads back as an ``asyncio.CancelledError`` — the outcome ``Job.abort``'s
    confirmed-abort verdict relies on."""
    info = deserialize_result(_serialize_result(False, asyncio.CancelledError()), deserializer=job_deserializer)

    assert info.success is False
    assert isinstance(info.result, asyncio.CancelledError)
    assert info.result.args == ("CancelledError()",)


def test_unserializable_success_result_stays_tagged_description() -> None:
    """A SUCCESSFUL job whose return value cannot cross the JSON wire keeps its
    success flag and reads back as the tagged description, never as arq's
    failed 'unable to serialize result' placeholder."""
    info = deserialize_result(_serialize_result(True, object()), deserializer=job_deserializer)

    assert info.success is True
    assert info.result[UNSERIALIZABLE_KEY] is True
    assert info.result["type"] == "object"
    assert info.result["repr"].startswith("<object object at ")
    assert info.result["cancelled"] is False
    assert info.result["traceback"] is None


def test_job_payload_stays_strict_for_unserializable_argument() -> None:
    """Enqueue-side payloads get no tagging fallback: an unserializable job
    argument still fails the enqueue loudly."""
    with pytest.raises(SerializationError, match='unable to serialize job "tool_execution"'):
        serialize_job("tool_execution", (object(),), {}, 1, 1, serializer=job_serializer)


def test_malformed_failure_tag_raises_on_deserialize() -> None:
    """A failed result whose tag lacks the description fields is neither normal
    JSON nor the tagged shape: it raises instead of degrading silently (arq
    wraps the raise as ``DeserializationError``)."""
    payload = orjson.loads(_serialize_result(False, asyncio.CancelledError()))
    payload["r"] = {UNSERIALIZABLE_KEY: True, "repr": "half a tag"}

    with pytest.raises(ValueError, match="missing fields \\['cancelled', 'traceback', 'type'\\]"):
        job_deserializer(orjson.dumps(payload))
    with pytest.raises(DeserializationError):
        deserialize_result(orjson.dumps(payload), deserializer=job_deserializer)


# -- pool ---------------------------------------------------------------------------


async def test_pool_created_once_and_closed(monkeypatch) -> None:
    created: list[Any] = []

    async def fake_create_pool(*args: Any, **kwargs: Any) -> Any:
        pool = AsyncMock()
        created.append(pool)
        return pool

    monkeypatch.setattr("tai42_backend_arq.pool.create_pool", fake_create_pool)
    monkeypatch.setattr(RedisPoolManager, "_pool", None)

    import asyncio

    first, second = await asyncio.gather(RedisPoolManager.get(), RedisPoolManager.get())
    assert first is second
    assert len(created) == 1

    await RedisPoolManager.close()
    created[0].aclose.assert_awaited_once()
    assert RedisPoolManager._pool is None

    # Closing again is a no-op.
    await RedisPoolManager.close()
