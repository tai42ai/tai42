"""The arq schedule store: each schedule a Redis hash plus the pending job that fires it.

A schedule is the hash ``arq:schedule:{name}``: the fired target, its args and kwargs (JSON),
the cadence dump (``schedule``), its compact ``cron_or_interval`` form, the ``enabled`` flag,
and the pending job (``job_id``, its due time ``last_scheduled_ts``). A disabled schedule keeps
its hash and its pending job, which fires without effect. Every state change runs under the
per-schedule lock (:func:`~tai42_backend_arq.scheduler.schedule_lock`), so an existence check
made under it stays true for the write that follows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import orjson
from pydantic_core import to_jsonable_python
from tai42_contract.backend import (
    CrontabCadence,
    IntervalCadence,
    ScheduleRecord,
    ScheduleRowError,
    StoredSchedule,
)
from tai42_kit.backend import fired_kwargs

from tai42_backend_arq import pool
from tai42_backend_arq.records import derive_cron_or_interval, next_run_after, read_stored_cadence
from tai42_backend_arq.scheduler import abort_schedule_task, safe_schedule_transition, schedule_lock
from tai42_backend_arq.settings import arq_settings


def _decode_optional(value: bytes | None) -> str | None:
    """Decode an optional schedule-hash field, ``None`` when the field is absent."""
    return value.decode() if value else None


def _row_error(data: dict[bytes, bytes]) -> ScheduleRowError | None:
    """The startup watchdog's terminal recovery failure recorded on a schedule hash, if any."""
    message = _decode_optional(data.get(b"recovery_error"))
    if message is None:
        return None
    return ScheduleRowError(message=message, at_iso=_decode_optional(data.get(b"recovery_failed_at")))


def _stored(name: str, data: dict[bytes, bytes]) -> StoredSchedule:
    """The schedule a hash holds; a hash in a format this backend does not read raises."""
    next_run_raw = data.get(b"last_scheduled_ts")
    return StoredSchedule(
        name=name,
        args=orjson.loads(data.get(b"args", b"[]")),
        kwargs=orjson.loads(data.get(b"kwargs", b"{}")),
        cadence=read_stored_cadence(name, data.get(b"schedule"), arq_settings().arq_schedule_pattern),
        enabled=data.get(b"enabled", b"true").decode() == "true",
        next_run_at_ts=float(next_run_raw) if next_run_raw else None,
        error=_row_error(data),
    )


class ArqScheduleStore:
    """The Redis-hash store of arq's schedules."""

    @property
    def relative_intervals(self) -> bool:
        """``False``: the arq scheduler re-arms an interval on its own clock only."""
        return False

    @property
    def holds_disabled(self) -> bool:
        """``True``: a disabled schedule keeps its hash, its ``enabled`` flag ``false``."""
        return True

    async def write(self, record: ScheduleRecord) -> None:
        """Write ``record``'s hash and replace its pending job, due at the cadence's next run from now."""
        cron_or_interval = derive_cron_or_interval(record.cadence)
        defer_by, last_scheduled_ts = next_run_after(cron_or_interval, datetime.now(UTC))
        mapping_updates = {
            "target": "tool_execution",
            "args": orjson.dumps(to_jsonable_python(record.args)),
            "kwargs": orjson.dumps(to_jsonable_python(fired_kwargs(record, arq_settings().tool_name_arg))),
            "schedule": record.cadence.model_dump_json(),
            "cron_or_interval": str(cron_or_interval),
            "enabled": "true" if record.enabled else "false",
        }
        async with pool.arq_connection() as arq_redis:
            await safe_schedule_transition(
                arq_redis,
                record.name,
                defer_by=defer_by,
                last_scheduled_ts=last_scheduled_ts,
                mapping_updates=mapping_updates,
                enforce_job_id=None,
            )

    async def exists(self, name: str) -> bool:
        """Whether the schedule hash of ``name`` exists."""
        async with pool.arq_connection() as arq_redis:
            return bool(await arq_redis.exists(arq_settings().arq_schedule_key(name)))

    async def read(self, name: str) -> StoredSchedule | None:
        """The schedule hash of ``name``, its next run the pending job's due time."""
        async with pool.arq_connection() as arq_redis:
            data = await arq_redis.hgetall(arq_settings().arq_schedule_key(name))
        return _stored(name, data) if data else None

    async def read_all(self) -> list[StoredSchedule]:
        """Every schedule hash a key scan finds; one emptied between the scan and its read is left out."""
        settings = arq_settings()
        stored: list[StoredSchedule] = []
        async with pool.arq_connection() as arq_redis:
            keys = [key async for key in arq_redis.scan_iter(match=settings.arq_schedule_pattern.encode())]
            for key in keys:
                raw = key.decode() if isinstance(key, bytes) else key
                data = await arq_redis.hgetall(raw)
                if data:
                    stored.append(_stored(raw.removeprefix(settings.arq_schedule_hash), data))
        return stored

    async def delete(self, name: str) -> bool:
        """Abort the pending job, then remove the hash."""
        key = arq_settings().arq_schedule_key(name)
        async with pool.arq_connection() as arq_redis, schedule_lock(arq_redis, name):
            if not await arq_redis.exists(key):
                return False
            await abort_schedule_task(key)
            await arq_redis.delete(key)
        return True

    async def set_enabled(self, name: str, enabled: bool) -> bool:
        """Write the hash's ``enabled`` flag; the pending job reads it when it fires."""
        key = arq_settings().arq_schedule_key(name)
        async with pool.arq_connection() as arq_redis, schedule_lock(arq_redis, name):
            if not await arq_redis.exists(key):
                return False
            await arq_redis.hset(key, "enabled", "true" if enabled else "false")
        return True

    async def run_now(self, name: str) -> bool:
        """Enqueue the schedule's target once, now; its pending job and cadence are kept."""
        settings = arq_settings()
        async with pool.arq_connection() as arq_redis:
            data = await arq_redis.hgetall(settings.arq_schedule_key(name))
            if not data:
                return False
            args = orjson.loads(data.get(b"args", b"[]"))
            kwargs = orjson.loads(data.get(b"kwargs", b"{}"))
            await arq_redis.enqueue_job(
                data.get(b"target", b"").decode(), *args, _queue_name=settings.queue_name, **kwargs
            )
        return True

    async def update(
        self,
        name: str,
        *,
        cadence: IntervalCadence | CrontabCadence | None,
        next_run_at_ts: float | None,
    ) -> bool:
        """Rewrite the cadence, the next run, or both, replacing the pending job in one transition.

        A new cadence without a next run is due at its first run after now. ``False`` when no
        hash is stored, or when a concurrent delete removed it before the transition.
        """
        async with pool.arq_connection() as arq_redis:
            if not await arq_redis.exists(arq_settings().arq_schedule_key(name)):
                return False
            mapping_updates: dict[str, Any] = {}
            now = datetime.now(UTC)
            if cadence is not None:
                cron_or_interval = derive_cron_or_interval(cadence)
                mapping_updates["schedule"] = cadence.model_dump_json()
                mapping_updates["cron_or_interval"] = str(cron_or_interval)
                if next_run_at_ts is None:
                    _, next_run_at_ts = next_run_after(cron_or_interval, now)
            assert next_run_at_ts is not None  # noqa: S101 the contract gives at least one change
            next_job = await safe_schedule_transition(
                arq_redis,
                name,
                defer_by=next_run_at_ts - now.timestamp(),
                last_scheduled_ts=next_run_at_ts,
                mapping_updates=mapping_updates,
                enforce_job_id=None,
            )
        return next_job is not None
