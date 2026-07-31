"""Tests for the uniform ``backend_*`` tool surface of the Celery backend."""

from __future__ import annotations

import json
from typing import Any

import pytest
from celery.exceptions import TimeoutError as CeleryTimeoutError
from redbeat.decoder import RedBeatJSONEncoder

import tai42_backend_celery.tools.tools as tools
from tests.conftest import FakeRedis

# Canonical task/worker tools every backend exposes.
TASK_WORKER_TOOLS = [
    "backend_task_status",
    "backend_task_result",
    "backend_cancel_task",
    "backend_active_tasks",
    "backend_reserved_tasks",
    "backend_scheduled_tasks",
    "backend_registered_tasks",
    "backend_worker_stats",
    "backend_worker_queues",
    "backend_ping_worker",
    "backend_list_active_workers",
    "backend_list_failed_tasks",
]

# Canonical schedule tools every backend exposes (the last four are the host's
# scheduling/backup marker tools; their names are load-bearing).
SCHEDULE_TOOLS = [
    "backend_schedule_exists",
    "backend_get_schedule",
    "backend_list_schedules",
    "backend_delete_schedule",
    "backend_enable_schedule",
    "backend_disable_schedule",
    "backend_run_schedule_now",
    "backend_update_schedule",
    "backend_export_schedules",
    "backend_import_schedules",
]


@pytest.mark.parametrize("name", TASK_WORKER_TOOLS + SCHEDULE_TOOLS)
def test_canonical_tool_is_registered(name, stub_app):
    assert callable(getattr(tools, name))
    assert name in stub_app.tools.registered


@pytest.mark.parametrize("name", TASK_WORKER_TOOLS + SCHEDULE_TOOLS)
def test_canonical_tool_is_tagged_backend(name, stub_app):
    assert stub_app.tools.tags[name] == {"backend"}


def test_list_failed_tasks_raises_not_implemented():
    with pytest.raises(NotImplementedError) as excinfo:
        tools.backend_list_failed_tasks()
    assert "backend 'celery' does not support backend_list_failed_tasks" in str(excinfo.value)


# --- backend_task_result timeout behaviour -------------------------------


class _FakeAsyncResult:
    """Stand-in for celery's AsyncResult with controllable readiness."""

    def __init__(self, task_id, *, ready, status, value=None, timeout_exc=None):
        self.task_id = task_id
        self._ready = ready
        self.status = status
        self._value = value
        self._timeout_exc = timeout_exc
        self.get_calls = []

    def ready(self):
        return self._ready

    def get(self, timeout=None):
        self.get_calls.append(timeout)
        if self._timeout_exc is not None:
            raise self._timeout_exc
        return self._value


def _patch_async_result(monkeypatch, fake):
    monkeypatch.setattr(tools, "AsyncResult", lambda task_id, app=None: fake)
    return fake


def test_task_result_no_timeout_snapshot_not_ready(monkeypatch):
    """timeout=None returns the snapshot 'not ready' value without blocking."""
    fake = _patch_async_result(monkeypatch, _FakeAsyncResult("t1", ready=False, status="PENDING"))
    out = tools.backend_task_result("t1")  # default timeout is None
    assert out == "Task t1 is not ready (status: PENDING)"
    assert fake.get_calls == []  # never blocked


def test_task_result_no_timeout_returns_result_when_ready(monkeypatch):
    fake = _patch_async_result(monkeypatch, _FakeAsyncResult("t2", ready=True, status="SUCCESS", value=42))
    out = tools.backend_task_result("t2", timeout=None)
    assert out == 42
    assert fake.get_calls == [None]


def test_task_result_with_timeout_waits_and_returns(monkeypatch):
    """A timeout must call get(timeout=...) even when not yet ready."""
    fake = _patch_async_result(monkeypatch, _FakeAsyncResult("t3", ready=False, status="STARTED", value="done"))
    out = tools.backend_task_result("t3", timeout=5)
    assert out == "done"
    assert fake.get_calls == [5]  # blocked up to the timeout


def test_task_result_timeout_elapsed_returns_not_ready(monkeypatch):
    """celery.exceptions.TimeoutError is the expected 'still running' case."""
    fake = _patch_async_result(
        monkeypatch,
        _FakeAsyncResult("t4", ready=False, status="STARTED", timeout_exc=CeleryTimeoutError("timed out")),
    )
    out = tools.backend_task_result("t4", timeout=1)
    assert out == "Task t4 is not ready (status: STARTED)"
    assert fake.get_calls == [1]


def test_task_result_propagates_task_failure(monkeypatch):
    """A real task failure must surface loudly, not be swallowed."""
    _patch_async_result(
        monkeypatch,
        _FakeAsyncResult("t5", ready=True, status="FAILURE", timeout_exc=ValueError("boom")),
    )
    with pytest.raises(ValueError, match="boom"):
        tools.backend_task_result("t5", timeout=1)


# --- narrowed schedule error handling ------------------------------------


class _FakeSettings:
    redbeat_redis_url = "redis://fake/0"
    redbeat_schedule_key = "redbeat::schedule"
    redbeat_key_prefix = "redbeat:"

    def redbeat_task_key(self, name):
        if name.startswith(self.redbeat_key_prefix):
            return name
        return f"{self.redbeat_key_prefix}{name}"


@pytest.fixture
def fake_settings(monkeypatch):
    monkeypatch.setattr(tools, "celery_settings", lambda: _FakeSettings())


async def test_get_schedule_malformed_json_falls_back_to_raw(fake_settings, stub_app):
    """Malformed definition/meta JSON is a tolerated case -> {_raw: ...}."""
    stub_app.clients.client = FakeRedis(
        hashes={"redbeat:job": {"definition": "{not json", "meta": "also not json"}},
        zset={"redbeat:job": 55.0},
    )
    out = await tools.backend_get_schedule("job")
    assert out["definition"] == {"_raw": "{not json"}
    assert out["meta"] == {"_raw": "also not json"}
    # The next-run score is read under the entry KEY (the real zset member).
    assert out["next_run_at_ts"] == 55.0


async def test_get_schedule_unexpected_error_propagates(fake_settings, stub_app, monkeypatch):
    """A non-JSON failure during parse must surface, not hide behind _raw."""

    def _boom(_txt):
        raise RuntimeError("unexpected parse failure")

    monkeypatch.setattr(tools.json, "loads", _boom)
    stub_app.clients.client = FakeRedis(hashes={"redbeat:job": {"definition": "{}"}})
    with pytest.raises(RuntimeError, match="unexpected parse failure"):
        await tools.backend_get_schedule("job")


async def test_get_schedule_not_found(fake_settings, stub_app):
    stub_app.clients.client = FakeRedis()
    out = await tools.backend_get_schedule("missing")
    assert out == {"status": "not_found", "name": "missing"}


async def test_update_schedule_malformed_json_returns_skipped(fake_settings, stub_app):
    """Malformed existing definition is a tolerated case -> skipped status."""
    stub_app.clients.client = FakeRedis(hashes={"redbeat:job": {"definition": "{not json"}})
    out = await tools.backend_update_schedule("job", new_schedule=10)
    assert out["status"] == "skipped"
    assert "not valid JSON" in out["message"]


async def test_update_schedule_unexpected_error_propagates(fake_settings, stub_app, monkeypatch):
    def _boom(_txt):
        raise RuntimeError("unexpected parse failure")

    monkeypatch.setattr(tools.json, "loads", _boom)
    stub_app.clients.client = FakeRedis(hashes={"redbeat:job": {"definition": "{}"}})
    with pytest.raises(RuntimeError, match="unexpected parse failure"):
        await tools.backend_update_schedule("job", new_schedule=10)


async def test_update_schedule_sets_schedule_and_next_run(fake_settings, stub_app):
    redis = FakeRedis(
        hashes={"redbeat:job": {"definition": json.dumps({"schedule": {"__type__": "interval", "every": 5.0}})}}
    )
    stub_app.clients.client = redis
    out = await tools.backend_update_schedule("job", new_schedule=30, next_run_at_ts=1000.0)
    assert out["status"] == "updated"
    assert out["new_schedule"] == {"__type__": "interval", "every": 30.0, "relative": False}
    assert out["next_run_at_ts"] == 1000.0
    # The zset member is the entry KEY (what Beat loads), never the bare name.
    assert redis.zset == {"redbeat:job": 1000.0}


async def test_update_schedule_nothing_to_do_is_skipped(fake_settings, stub_app):
    stub_app.clients.client = FakeRedis(hashes={"redbeat:job": {"definition": "{}"}})
    out = await tools.backend_update_schedule("job")
    assert out["status"] == "skipped"


async def test_enable_disable_and_delete_schedule(fake_settings, stub_app):
    # The zset member is the full entry key, exactly as RedBeat stores it.
    redis = FakeRedis(
        hashes={"redbeat:job": {"definition": json.dumps({"enabled": False})}},
        zset={"redbeat:job": 123.0},
    )
    stub_app.clients.client = redis

    out = await tools.backend_enable_schedule("job")
    assert out == {"status": "enabled", "name": "job"}
    assert json.loads(redis.hashes["redbeat:job"]["definition"])["enabled"] is True
    assert redis.zset == {"redbeat:job": 0}

    out = await tools.backend_disable_schedule("job")
    assert out == {"status": "disabled", "name": "job"}
    assert json.loads(redis.hashes["redbeat:job"]["definition"])["enabled"] is False
    assert redis.zset == {}

    redis.zset["redbeat:job"] = 123.0
    out = await tools.backend_delete_schedule("job")
    assert out == {"status": "deleted", "name": "job"}
    assert redis.hashes == {}
    assert redis.zset == {}  # the entry-key member left the zset with the hash


async def test_enable_disable_not_found(fake_settings, stub_app):
    stub_app.clients.client = FakeRedis()
    assert (await tools.backend_enable_schedule("nope"))["status"] == "not_found"
    assert (await tools.backend_disable_schedule("nope"))["status"] == "not_found"


async def test_schedule_exists_and_run_now(fake_settings, stub_app):
    redis = FakeRedis(hashes={"redbeat:job": {"definition": "{}"}})
    stub_app.clients.client = redis
    assert await tools.backend_schedule_exists("job") is True
    assert await tools.backend_schedule_exists("other") is False
    out = await tools.backend_run_schedule_now("job")
    assert out["status"] == "queued"
    # Queued under the entry KEY: Beat loads zset members as redis keys.
    assert redis.zset == {"redbeat:job": 0}


async def test_run_schedule_now_missing_entry_is_not_found(fake_settings, stub_app):
    """Queueing a nonexistent schedule must not report success: Beat would drop
    the dangling member without running anything."""
    redis = FakeRedis()
    stub_app.clients.client = redis
    out = await tools.backend_run_schedule_now("ghost")
    assert out == {"status": "not_found", "name": "ghost"}
    assert redis.zset == {}


async def test_list_schedules_reports_bare_names_scores_and_enabled(fake_settings, stub_app):
    """Members arrive as entry keys; the reported name is the bare schedule name
    and ``enabled`` is read from each entry's definition."""
    stub_app.clients.client = FakeRedis(
        hashes={
            "redbeat:a": {"definition": json.dumps({"name": "a", "enabled": True})},
            "redbeat:b": {"definition": json.dumps({"name": "b", "enabled": False})},
        },
        zset={"redbeat:a": 0.0, "redbeat:b": 1700000000.0},
    )
    out = await tools.backend_list_schedules()
    by_name = {row["name"]: row for row in out}
    assert set(by_name) == {"a", "b"}
    assert by_name["a"]["enabled"] is True
    assert by_name["a"]["next_run_at_iso"] is None  # score 0 means "no next run yet"
    assert by_name["b"]["enabled"] is False
    assert by_name["b"]["next_run_at_ts"] == 1700000000.0
    assert by_name["b"]["next_run_at_iso"].startswith("2023-")


async def test_list_schedules_dangling_zset_member_raises(fake_settings, stub_app):
    """A zset member with no backing entry hash must raise, not be listed
    half-shaped."""
    stub_app.clients.client = FakeRedis(zset={"redbeat:ghost": 0.0})
    with pytest.raises(KeyError, match="no 'definition'"):
        await tools.backend_list_schedules()


class _ConcurrentDeleteRedis(FakeRedis):
    """Simulates an entry deleted between the zrange snapshot and the atomic
    per-member read: just before the batch touching the victim executes, both
    its hash and its zset member are removed (a whole-entry delete landing
    mid-listing)."""

    def __init__(self, victim: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._victim = victim

    def on_pipeline_execute(self, commands: list[tuple[str, tuple[Any, ...]]]) -> None:
        if any(self._victim in args for _name, args in commands):
            self.hashes.pop(self._victim, None)
            self.zset.pop(self._victim, None)


async def test_list_schedules_omits_entry_deleted_after_snapshot(fake_settings, stub_app):
    """An entry legitimately deleted between the zset snapshot and the hash
    read (member and hash both gone) is omitted — a consistent read of its
    absence, not a spurious dangling-member error."""
    stub_app.clients.client = _ConcurrentDeleteRedis(
        "redbeat:victim",
        hashes={"redbeat:live": {"definition": json.dumps({"name": "live", "enabled": True})}},
        zset={"redbeat:live": 10.0, "redbeat:victim": 20.0},
    )
    out = await tools.backend_list_schedules()
    assert [row["name"] for row in out] == ["live"]


async def test_export_schedules_omits_entry_deleted_after_snapshot(fake_settings, stub_app):
    """Same concurrent-delete semantics for the export marker tool."""
    live_definition = {
        "name": "live",
        "args": [],
        "kwargs": {},
        "schedule": {"__type__": "interval", "every": 30.0, "relative": False},
        "enabled": True,
    }
    stub_app.clients.client = _ConcurrentDeleteRedis(
        "redbeat:victim",
        hashes={"redbeat:live": {"definition": json.dumps(live_definition)}},
        zset={"redbeat:live": 10.0, "redbeat:victim": 20.0},
    )
    exported = await tools.backend_export_schedules()
    assert [rec["name"] for rec in exported] == ["live"]


class _DeleteThenRecreateRedis(FakeRedis):
    """Simulates a whole-entry delete plus an atomic re-create (RedBeat's
    ``save``) straddling the listing's per-member read: the victim is already
    deleted when its row is read, and the re-create lands immediately after
    the first standalone read touching it. At no instant does a zset member
    exist without its backing hash, so the listing must omit the victim as a
    consistent deletion — reading ``definition`` and zset membership as two
    separate commands would instead pair the delete's missing hash with the
    re-create's zset member and raise a spurious dangling-member error."""

    def __init__(self, victim: str, recreate_definition: str, snapshot_score: float, **kwargs) -> None:
        super().__init__(**kwargs)
        self._victim = victim
        self._recreate_definition = recreate_definition
        self._snapshot_score = snapshot_score

    async def zrange(self, zset: str, start: int, end: int, withscores: bool = False) -> list[Any]:
        # The snapshot was taken before the delete: it still lists the victim.
        rows = await super().zrange(zset, start, end, withscores=withscores)
        victim = (self._victim.encode(), self._snapshot_score) if withscores else self._victim.encode()
        return [*rows, victim]

    async def hget(self, key: str, field: str) -> Any:
        value = await super().hget(key, field)
        if key == self._victim:
            # The atomic re-create (hash + member in one transaction) lands
            # right after this standalone read.
            self.hashes[self._victim] = {"definition": self._recreate_definition}
            self.zset[self._victim] = 0.0
        return value


async def test_list_schedules_tolerates_delete_then_recreate_around_the_read(fake_settings, stub_app):
    """A delete plus atomic re-create straddling the per-member read must not
    raise: the store never held a dangling member, and the atomic
    definition+membership read only ever observes consistent states."""
    stub_app.clients.client = _DeleteThenRecreateRedis(
        "redbeat:victim",
        json.dumps({"name": "victim", "enabled": True}),
        20.0,
        hashes={"redbeat:live": {"definition": json.dumps({"name": "live", "enabled": True})}},
        zset={"redbeat:live": 10.0},
    )
    out = await tools.backend_list_schedules()
    assert [row["name"] for row in out] == ["live"]


async def test_export_schedules_tolerates_delete_then_recreate_around_the_read(fake_settings, stub_app):
    """Same delete-plus-re-create semantics for the export marker tool."""
    live_definition = {
        "name": "live",
        "args": [],
        "kwargs": {},
        "schedule": {"__type__": "interval", "every": 30.0, "relative": False},
        "enabled": True,
    }
    stub_app.clients.client = _DeleteThenRecreateRedis(
        "redbeat:victim",
        json.dumps({"name": "victim", "enabled": True}),
        20.0,
        hashes={"redbeat:live": {"definition": json.dumps(live_definition)}},
        zset={"redbeat:live": 10.0},
    )
    exported = await tools.backend_export_schedules()
    assert [rec["name"] for rec in exported] == ["live"]


async def test_delete_schedule_removes_zset_member_before_hash(fake_settings, stub_app):
    """The zset member must leave before the entry hash (RedBeat's own delete
    order): the reverse would transiently create the dangling-member state the
    listing tools raise on."""
    ops: list[str] = []

    class _OrderRecordingRedis(FakeRedis):
        async def zrem(self, zset: str, member: str) -> int:
            ops.append("zrem")
            return await super().zrem(zset, member)

        async def delete(self, key: str) -> int:
            ops.append("delete")
            return await super().delete(key)

    redis = _OrderRecordingRedis(hashes={"redbeat:job": {"definition": "{}"}}, zset={"redbeat:job": 1.0})
    stub_app.clients.client = redis
    out = await tools.backend_delete_schedule("job")
    assert out == {"status": "deleted", "name": "job"}
    assert ops == ["zrem", "delete"]


# --- export / import round-trip ------------------------------------------


class _FakeRedisRoundTrip(FakeRedis):
    """A fake redis whose hashes/zset a fake RedBeat entry writes into.

    ``zrange`` returns members as bytes to exercise the tool's decode path, and
    zset members are the full ``redbeat:{name}`` keys just as real RedBeat
    stores them.
    """


def _fake_entry_cls(redis):
    """A RedBeatSchedulerEntry stand-in that persists like the real one does,
    serializing the celery schedule via RedBeat's own encoder."""

    class _FakeEntry:
        def __init__(self, *, name, task, schedule, args, kwargs, enabled=True, app):
            self.name = name
            self.task = task
            self.schedule = schedule
            self.args = args
            self.kwargs = kwargs
            self.enabled = enabled

        def save(self):
            key = f"redbeat:{self.name}"
            definition = {
                "name": self.name,
                "task": self.task,
                "args": self.args,
                "kwargs": self.kwargs,
                "options": None,
                "schedule": self.schedule,
                "enabled": self.enabled,
            }
            redis.hashes.setdefault(key, {})["definition"] = json.dumps(definition, cls=RedBeatJSONEncoder)
            redis.zset[key] = 0.0
            return self

    return _FakeEntry


@pytest.fixture
def roundtrip_redis(fake_settings, stub_app, monkeypatch):
    redis = _FakeRedisRoundTrip()
    stub_app.clients.client = redis
    monkeypatch.setattr(tools, "RedBeatSchedulerEntry", _fake_entry_cls(redis))
    return redis


_TOOL_ARG = "tool_name"

_INTERVAL_RECORD = {
    "name": "nightly-sync",
    "args": [],
    "kwargs": {_TOOL_ARG: "my_tool"},
    "schedule": {"__type__": "interval", "every": 30.0, "relative": False},
    "enabled": True,
}

_CRONTAB_RECORD = {
    "name": "weekday-report",
    "args": [],
    "kwargs": {_TOOL_ARG: "report_tool"},
    "schedule": {
        "__type__": "crontab",
        "minute": "0",
        "hour": "9",
        "day_of_month": "*",
        "month_of_year": "*",
        "day_of_week": "1",
    },
    "enabled": False,
}


async def test_import_then_export_round_trips_interval_and_crontab(roundtrip_redis):
    """Interval + crontab records survive an import/export cycle: kwargs
    (tool name), schedule, and enabled all come back unchanged."""
    result = await tools.backend_import_schedules([_INTERVAL_RECORD, _CRONTAB_RECORD])
    assert result == {"created": 2, "updated": 0, "skipped": 0, "errors": []}

    exported = await tools.backend_export_schedules()
    by_name = {rec["name"]: rec for rec in exported}
    assert by_name["nightly-sync"] == _INTERVAL_RECORD
    assert by_name["weekday-report"] == _CRONTAB_RECORD


async def test_import_upsert_overwrites_existing(roundtrip_redis):
    """Re-importing the same name overwrites it and is counted as 'updated'."""
    first = await tools.backend_import_schedules([_INTERVAL_RECORD])
    assert first == {"created": 1, "updated": 0, "skipped": 0, "errors": []}

    second = await tools.backend_import_schedules([_INTERVAL_RECORD])
    assert second == {"created": 0, "updated": 1, "skipped": 0, "errors": []}

    exported = await tools.backend_export_schedules()
    assert len(exported) == 1  # upsert, not a duplicate


async def test_import_malformed_record_reported_in_errors_not_silent(roundtrip_redis):
    """A malformed record lands in 'errors' loudly; valid siblings still import."""
    malformed = {
        "name": "broken",
        "schedule": {"__type__": "not-a-real-kind"},
        "enabled": True,
    }
    result = await tools.backend_import_schedules([_INTERVAL_RECORD, malformed])
    assert result["created"] == 1
    assert result["updated"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["index"] == 1
    assert result["errors"][0]["name"] == "broken"
    assert result["errors"][0]["error"]  # non-empty message, surfaced not swallowed


async def test_import_renormalizes_and_reports_broken_schedule(roundtrip_redis):
    """A record that passes ScheduleRecord validation but is not a valid
    normalized schedule (here a non-positive interval) is re-normalized on
    import, so the failure surfaces loudly in 'errors' at one consistent spot
    instead of persisting a broken entry. Valid siblings still import."""
    broken = {
        "name": "bad-interval",
        "args": [],
        "kwargs": {},
        # __type__/every are numeric so ScheduleRecord accepts it, but a
        # non-positive interval is not a valid normalized schedule.
        "schedule": {"__type__": "interval", "every": -5, "relative": False},
        "enabled": True,
    }
    result = await tools.backend_import_schedules([_INTERVAL_RECORD, broken])
    assert result["created"] == 1
    assert result["updated"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["index"] == 1
    assert result["errors"][0]["name"] == "bad-interval"
    assert "every" in result["errors"][0]["error"]
    # The broken schedule was not persisted.
    assert "redbeat:bad-interval" not in roundtrip_redis.hashes


async def test_export_raises_when_definition_missing(roundtrip_redis):
    """A zset member with no backing definition is an error, never skipped."""
    roundtrip_redis.zset["redbeat:ghost"] = 0.0  # listed but no hash written
    with pytest.raises(KeyError, match="ghost"):
        await tools.backend_export_schedules()


async def test_import_rejects_an_unknown_normalized_kind(roundtrip_redis, monkeypatch):
    """Defensive guard behind normalize_schedule: an unexpected kind lands in
    the per-row errors instead of persisting a broken entry."""
    monkeypatch.setattr(tools, "normalize_schedule", lambda s: {"__type__": "hourly"})
    result = await tools.backend_import_schedules([_INTERVAL_RECORD])
    assert result["created"] == 0
    assert "Unsupported schedule type" in result["errors"][0]["error"]


async def test_update_schedule_not_found(fake_settings, stub_app):
    stub_app.clients.client = FakeRedis()
    out = await tools.backend_update_schedule("missing", new_schedule=10)
    assert out["status"] == "not_found"


async def test_update_schedule_without_definition_is_skipped(fake_settings, stub_app):
    stub_app.clients.client = FakeRedis(hashes={"redbeat:job": {}})
    out = await tools.backend_update_schedule("job", new_schedule=10)
    assert out["status"] == "skipped"
    assert "no 'definition'" in out["message"]


async def test_update_schedule_next_run_in_ms_takes_precedence(fake_settings, stub_app):
    redis = FakeRedis(hashes={"redbeat:job": {"definition": "{}"}})
    stub_app.clients.client = redis
    out = await tools.backend_update_schedule("job", next_run_in_ms=0, next_run_at_ts=1.0)
    assert out["status"] == "updated"
    # next_run_in_ms wins over the absolute timestamp: the score is "now", not 1.0.
    assert redis.zset["redbeat:job"] > 1.0
