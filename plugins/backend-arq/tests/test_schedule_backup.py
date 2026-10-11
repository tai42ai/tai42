"""Export/import round-trip of the portable schedule records."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any, Literal
from unittest.mock import patch

import orjson
import pytest
from arq.jobs import JobStatus

from tai42_backend_arq import pool, scheduler, tools

from .conftest import lease_of


class _FakeJob:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.job_id = args[0] if args else "job"

    async def status(self) -> JobStatus:
        return JobStatus.deferred

    async def abort(self, *, timeout: float | None = None, poll_delay: float = 0.5) -> bool:
        return True


class _FailingAbortJob(_FakeJob):
    async def abort(self, *, timeout: float | None = None, poll_delay: float = 0.5) -> bool:
        raise RuntimeError("abort failed")


class _PendingAbortJob(_FakeJob):
    """An abort whose confirmation is still pending (no live worker yet)."""

    async def abort(self, *, timeout: float | None = None, poll_delay: float = 0.5) -> bool:
        raise TimeoutError


def _seed_schedule(redis: Any, *, name: str, schedule: dict[str, Any], kwargs: dict[str, Any], enabled: bool) -> None:
    """Write a schedule hash the way a live create would leave it at rest."""
    key = f"arq:schedule:{name}"
    redis._store[key] = {
        b"target": b"tool_execution",
        b"args": orjson.dumps([]),
        b"kwargs": orjson.dumps(kwargs),
        b"schedule": orjson.dumps(schedule),
        b"cron_or_interval": b"ignored",
        b"enabled": b"true" if enabled else b"false",
        b"job_id": b"seed-job",
        b"last_scheduled_ts": b"12345.0",
        b"aborted": b"",
    }


@asynccontextmanager
async def _redis_bound(redis: Any, job_cls: type = _FakeJob) -> AsyncIterator[None]:
    with (
        patch.object(pool, "arq_connection", lease_of(redis)),
        patch.object(scheduler, "Job", job_cls),
    ):
        yield


# The backend's own tool-name key in these tests is NOT the platform default, so the round trip
# proves the backend splits and injects it itself.
_TOOL_ARG = "custom_target"

INTERVAL_CADENCE = {"kind": "interval", "every": 60.0, "relative": False}
CRONTAB_CADENCE = {
    "kind": "crontab",
    "minute": "0",
    "hour": "9",
    "day_of_month": "*",
    "month_of_year": "*",
    "day_of_week": "1",
}
INTERVAL_KWARGS = {_TOOL_ARG: "send_report", "payload": "x"}
CRONTAB_KWARGS = {_TOOL_ARG: "nightly_sync"}


@pytest.fixture(autouse=True)
def _custom_tool_name_arg(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("ARQ_TOOL_NAME_ARG", _TOOL_ARG)
    reset_all_settings()
    yield
    monkeypatch.delenv("ARQ_TOOL_NAME_ARG", raising=False)
    reset_all_settings()


def _record(name: str, tool: str = "send_report", **overrides: Any) -> dict[str, Any]:
    return {
        "name": name,
        "tool": tool,
        "args": [],
        "kwargs": {"payload": "x"},
        "cadence": INTERVAL_CADENCE,
        "enabled": True,
        **overrides,
    }


async def _export(redis: Any) -> list[dict[str, Any]]:
    async with _redis_bound(redis):
        return await tools.backend_export_schedules()


async def _import(
    redis: Any, records: list[dict[str, Any]], *, mode: Literal["skip", "overwrite"] | None = None
) -> dict[str, Any]:
    async with _redis_bound(redis):
        if mode is None:
            return await tools.backend_import_schedules(records)
        return await tools.backend_import_schedules(records, mode)


def _read_hash(redis: Any, name: str) -> dict[bytes, bytes]:
    return redis._store[f"arq:schedule:{name}"]


async def test_interval_schedule_round_trips(fake_redis) -> None:
    src = fake_redis
    _seed_schedule(src, name="interval_one", schedule=INTERVAL_CADENCE, kwargs=INTERVAL_KWARGS, enabled=True)

    exported = await _export(src)
    assert len(exported) == 1
    record = exported[0]
    assert record["name"] == "interval_one"
    assert record["tool"] == "send_report"
    assert record["cadence"] == INTERVAL_CADENCE
    assert record["kwargs"] == {"payload": "x"}
    assert record["enabled"] is True
    # Runtime-only fields must not leak into the portable record.
    assert set(record) == {"name", "tool", "args", "kwargs", "cadence", "enabled"}

    dst = type(src)()
    result = await _import(dst, exported)
    assert result == {"created": 1, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": []}

    stored = _read_hash(dst, "interval_one")
    assert orjson.loads(stored[b"schedule"]) == INTERVAL_CADENCE
    assert orjson.loads(stored[b"kwargs"]) == INTERVAL_KWARGS
    assert stored[b"enabled"] == b"true"
    assert stored[b"target"] == b"tool_execution"
    assert stored[b"cron_or_interval"] == b"60"
    # Next run is recomputed, not the stale exported timestamp.
    assert stored[b"last_scheduled_ts"] != b"12345.0"

    re_exported = await _export(dst)
    assert re_exported == exported


async def test_crontab_schedule_round_trips(fake_redis) -> None:
    src = fake_redis
    _seed_schedule(src, name="cron_one", schedule=CRONTAB_CADENCE, kwargs=CRONTAB_KWARGS, enabled=False)

    exported = await _export(src)
    record = exported[0]
    assert record["cadence"] == CRONTAB_CADENCE
    assert record["tool"] == "nightly_sync"
    assert record["kwargs"] == {}
    assert record["enabled"] is False

    dst = type(src)()
    result = await _import(dst, exported)
    assert result == {"created": 1, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": []}

    stored = _read_hash(dst, "cron_one")
    assert orjson.loads(stored[b"kwargs"]) == CRONTAB_KWARGS
    assert stored[b"enabled"] == b"false"
    assert stored[b"cron_or_interval"] == b"0 9 * * 1"

    re_exported = await _export(dst)
    assert re_exported == exported


async def test_export_missing_schedule_field_raises(fake_redis) -> None:
    """A schedule hash with no stored cadence must fail the export loudly
    -- an exported record without its cadence could never be re-imported."""
    from tai42_backend_arq.records import ScheduleStoreFormatError

    src = fake_redis
    _seed_schedule(src, name="broken", schedule=INTERVAL_CADENCE, kwargs=INTERVAL_KWARGS, enabled=True)
    del src._store["arq:schedule:broken"][b"schedule"]

    with pytest.raises(ScheduleStoreFormatError, match="schedule 'broken' is stored in a format"):
        await _export(src)


async def test_export_a_hash_without_the_tool_key_raises(fake_redis) -> None:
    src = fake_redis
    _seed_schedule(src, name="orphan", schedule=INTERVAL_CADENCE, kwargs={"payload": "x"}, enabled=True)
    with pytest.raises(ValueError, match="schedule 'orphan' carries no 'custom_target'"):
        await _export(src)


async def test_export_and_list_skip_schedule_deleted_mid_scan(fake_redis, bind_pool) -> None:
    """A hash that reads back empty was deleted between the scan and the read
    (an existing Redis hash is never empty): it is no schedule at all, so the
    export must not fail on it and the listing must not emit a phantom row."""
    src = fake_redis
    bind_pool(src)
    _seed_schedule(src, name="alive", schedule=INTERVAL_CADENCE, kwargs=INTERVAL_KWARGS, enabled=True)
    src._store["arq:schedule:vanished"] = {}

    exported = await _export(src)
    assert [record["name"] for record in exported] == ["alive"]

    listed = await tools.backend_list_schedules()
    assert [row["name"] for row in listed] == ["alive"]


async def test_export_an_old_format_hash_raises_the_store_format_error(fake_redis) -> None:
    """A hash still holding RedBeat-shaped ``__type__`` JSON (the store was not reset) fails
    the export loudly, naming the keys to delete."""
    from tai42_backend_arq.records import ScheduleStoreFormatError

    src = fake_redis
    old = {"__type__": "interval", "every": 60.0, "relative": False}
    _seed_schedule(src, name="corrupt", schedule=old, kwargs=INTERVAL_KWARGS, enabled=True)

    with pytest.raises(ScheduleStoreFormatError, match="re-create the schedules"):
        await _export(src)


async def test_import_upsert_overwrites_existing_name(fake_redis) -> None:
    dst = fake_redis
    record = _record("dup")

    first = await _import(dst, [record])
    assert first == {"created": 1, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": []}

    changed = dict(record, tool="changed")
    second = await _import(dst, [changed], mode="overwrite")
    assert second == {"created": 0, "updated": 1, "skipped": 0, "skipped_existing": 0, "errors": []}

    stored = _read_hash(dst, "dup")
    assert orjson.loads(stored[b"kwargs"]) == {"payload": "x", _TOOL_ARG: "changed"}


async def test_import_skip_leaves_existing_untouched(fake_redis) -> None:
    """Under ``skip`` (the default the host forwards) an existing name is left in
    place — not aborted, not re-applied — and counted in ``skipped_existing``. This
    is the mode the backup restore passes across the tool boundary, so the tool must
    accept and honor it on this backend exactly as every backend does."""
    dst = fake_redis
    record = _record("dup")

    first = await _import(dst, [record], mode="skip")
    assert first == {"created": 1, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": []}
    original_job_id = _read_hash(dst, "dup")[b"job_id"]

    changed = dict(record, tool="changed")
    second = await _import(dst, [changed], mode="skip")
    assert second == {"created": 0, "updated": 0, "skipped": 0, "skipped_existing": 1, "errors": []}

    # The original schedule is untouched — the changed re-import never applied.
    stored = _read_hash(dst, "dup")
    assert orjson.loads(stored[b"kwargs"]) == INTERVAL_KWARGS
    assert stored[b"job_id"] == original_job_id


async def test_malformed_record_reported_not_silently_dropped(fake_redis) -> None:
    dst = fake_redis
    good = _record("good")
    bad = {"name": "bad", "kwargs": INTERVAL_KWARGS, "schedule": {"__type__": "interval", "every": 60.0}}

    result = await _import(dst, [good, bad])
    assert result["created"] == 1
    assert result["updated"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["name"] == "bad"
    assert result["errors"][0]["index"] == 1
    # The good record still landed; the bad one did not.
    assert "arq:schedule:good" in dst._store
    assert "arq:schedule:bad" not in dst._store


async def test_failed_abort_of_replaced_job_surfaces_not_swallowed(fake_redis) -> None:
    """Importing over an existing schedule aborts the job it replaces. When that
    abort fails, the error must surface in 'errors' (never swallowed), the
    replacement must not be enqueued, and the old job id must stay untouched --
    otherwise the old job would run on alongside a duplicate replacement."""
    dst = fake_redis
    _seed_schedule(dst, name="dup", schedule=INTERVAL_CADENCE, kwargs=INTERVAL_KWARGS, enabled=True)
    old_job_id = dst._store["arq:schedule:dup"][b"job_id"]

    record = _record("dup")

    async with _redis_bound(dst, job_cls=_FailingAbortJob):
        result = await tools.backend_import_schedules([record], "overwrite")

    assert result["created"] == 0
    assert result["updated"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["name"] == "dup"
    assert "abort failed" in result["errors"][0]["error"]
    # The transition stopped before enqueue/hset: old job id is untouched.
    assert dst._store["arq:schedule:dup"][b"job_id"] == old_job_id


async def test_pending_abort_confirmation_does_not_block_import(fake_redis) -> None:
    """An abort whose cancellation is not yet confirmed (the worker confirms it
    at pick-up) is the expected asynchronous outcome: the import proceeds and
    the schedule is overwritten."""
    dst = fake_redis
    _seed_schedule(dst, name="dup", schedule=INTERVAL_CADENCE, kwargs=INTERVAL_KWARGS, enabled=True)

    record = _record("dup")

    async with _redis_bound(dst, job_cls=_PendingAbortJob):
        result = await tools.backend_import_schedules([record], "overwrite")

    assert result == {"created": 0, "updated": 1, "skipped": 0, "skipped_existing": 0, "errors": []}
    assert _read_hash(dst, "dup")[b"job_id"] != b"seed-job"


async def test_import_refuses_a_relative_interval_per_row(fake_redis) -> None:
    dst = fake_redis
    relative = _record("rel", cadence={"kind": "interval", "every": 60.0, "relative": True})
    result = await _import(dst, [_record("good"), relative])
    assert result["created"] == 1
    (error,) = result["errors"]
    assert error["name"] == "rel"
    assert "does not support relative interval schedules" in error["error"]
    assert "arq:schedule:rel" not in dst._store
