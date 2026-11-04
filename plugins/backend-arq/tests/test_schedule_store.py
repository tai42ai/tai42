"""The arq schedule store: a record written as a Redis hash plus the pending job that fires it."""

from __future__ import annotations

from typing import Any

import orjson
import pytest
from tai42_contract.backend import CrontabCadence, IntervalCadence, ScheduleRecord

from tai42_backend_arq import scheduler
from tai42_backend_arq.schedule_store import ArqScheduleStore

from .conftest import handle_of


class _NoJob:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def abort(self, **kwargs: Any) -> bool:
        return True


@pytest.fixture
def redis(fake_redis: Any, bind_pool: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    bind_pool(fake_redis)
    monkeypatch.setattr(scheduler, "job_handle", handle_of(_NoJob))
    return fake_redis


def _record(cadence: Any, *, enabled: bool = True, **kwargs: Any) -> ScheduleRecord:
    return ScheduleRecord(
        name="every-min", tool="sample_tool", args=[], kwargs={"text": "hi", **kwargs}, cadence=cadence, enabled=enabled
    )


def test_the_store_holds_no_relative_interval() -> None:
    assert ArqScheduleStore().relative_intervals is False


async def test_write_stores_an_interval_schedule_naming_the_tool(redis: Any) -> None:
    await ArqScheduleStore().write(_record(IntervalCadence(every=60)))

    stored = redis._store["arq:schedule:every-min"]
    assert stored[b"target"] == b"tool_execution"
    assert stored[b"cron_or_interval"] == b"60"
    assert stored[b"enabled"] == b"true"
    assert orjson.loads(stored[b"schedule"]) == {"kind": "interval", "every": 60.0, "relative": False}
    assert orjson.loads(stored[b"args"]) == []
    assert orjson.loads(stored[b"kwargs"]) == {"text": "hi", "backend_tool_name": "sample_tool"}


async def test_write_stores_a_crontab_schedule(redis: Any) -> None:
    await ArqScheduleStore().write(_record(CrontabCadence(minute="0", hour="9", day_of_week="1")))

    stored = redis._store["arq:schedule:every-min"]
    assert stored[b"cron_or_interval"] == b"0 9 * * 1"
    assert orjson.loads(stored[b"schedule"])["kind"] == "crontab"


async def test_write_stores_a_disabled_record_as_disabled(redis: Any) -> None:
    await ArqScheduleStore().write(_record(IntervalCadence(every=60), enabled=False))

    assert redis._store["arq:schedule:every-min"][b"enabled"] == b"false"


async def test_write_keeps_the_stamped_keys_in_the_fired_kwargs(redis: Any) -> None:
    from tai42_kit.utils.schedule_subject import SCHEDULE_CONTRACT_ARG, SCHEDULE_EXECUTION_KEY_ARG

    contract = {"resume_expr": {"content": "."}}
    await ArqScheduleStore().write(
        _record(IntervalCadence(every=60), **{SCHEDULE_EXECUTION_KEY_ARG: "svc-key", SCHEDULE_CONTRACT_ARG: contract})
    )

    stored = orjson.loads(redis._store["arq:schedule:every-min"][b"kwargs"])
    assert stored[SCHEDULE_EXECUTION_KEY_ARG] == "svc-key"
    assert stored[SCHEDULE_CONTRACT_ARG] == contract


async def test_read_all_keeps_a_name_holding_a_colon_whole(redis: Any) -> None:
    store = ArqScheduleStore()
    await store.write(_record(IntervalCadence(every=60)).model_copy(update={"name": "team:nightly"}))

    (stored,) = await store.read_all()
    assert stored.name == "team:nightly"
    assert await store.read("team:nightly") == stored


async def test_an_update_of_a_schedule_not_stored_is_false(redis: Any) -> None:
    store = ArqScheduleStore()
    assert await store.update("nope", cadence=IntervalCadence(every=120), next_run_at_ts=None) is False
    assert redis._store == {}
