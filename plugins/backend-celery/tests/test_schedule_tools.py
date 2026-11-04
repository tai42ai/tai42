"""The Celery backend's schedule tools as the registry serves them, over the RedBeat store."""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from typing import Any

import pytest
from redbeat.decoder import RedBeatJSONEncoder

import tai42_backend_celery.core.schedule_store as schedule_store
from tai42_backend_celery.core.settings import celery_settings

from .conftest import FakeRedis


@pytest.fixture
def tools(stub_app: Any) -> SimpleNamespace:
    """The registered tools, by name."""
    return SimpleNamespace(**stub_app.tools.registered)


# --- narrowed schedule error handling ------------------------------------

_TOOL_ARG = celery_settings().tool_name_arg
_INTERVAL_5 = {"__type__": "interval", "every": 5.0, "relative": False}
_DEF_5 = json.dumps({"name": "job", "kwargs": {_TOOL_ARG: "echo"}, "schedule": _INTERVAL_5, "enabled": True})


def _definition(name: str, *, enabled: bool = True, tool: str = "echo") -> str:
    return json.dumps({"name": name, "kwargs": {_TOOL_ARG: tool}, "schedule": _INTERVAL_5, "enabled": enabled})


@pytest.mark.parametrize("name", ["backend_get_schedule", "backend_update_schedule"])
async def test_a_malformed_definition_raises(stub_app, tools, name):
    """A definition that is not JSON is no schedule the tools can read: they raise, never reply a guess."""
    stub_app.clients.client = FakeRedis(hashes={"redbeat:job": {"definition": "{not json"}}, zset={"redbeat:job": 55.0})
    with pytest.raises(json.JSONDecodeError):
        await getattr(tools, name)("job", **({"new_schedule": 10} if name == "backend_update_schedule" else {}))


async def test_get_schedule_reads_the_score_under_the_entry_key(stub_app, tools):
    stub_app.clients.client = FakeRedis(
        hashes={"redbeat:job": {"definition": _definition("job")}}, zset={"redbeat:job": 55.0}
    )
    out = await tools.backend_get_schedule("job")
    assert out["next_run_at_ts"] == 55.0
    assert out["tool"] == "echo"


async def test_get_schedule_takes_the_prefixed_name(stub_app, tools):
    stub_app.clients.client = FakeRedis(hashes={"redbeat:job": {"definition": _definition("job")}})
    out = await tools.backend_get_schedule("redbeat:job")
    assert out["name"] == "job"
    assert out["next_run_at_ts"] is None


async def test_get_schedule_not_found(stub_app, tools):
    stub_app.clients.client = FakeRedis()
    out = await tools.backend_get_schedule("missing")
    assert out == {"status": "not_found", "name": "missing"}


async def test_update_schedule_sets_schedule_and_next_run(stub_app, tools):
    redis = FakeRedis(hashes={"redbeat:job": {"definition": _definition("job")}})
    stub_app.clients.client = redis
    out = await tools.backend_update_schedule("job", new_schedule=30, next_run_at_ts=1000.0)
    assert out["status"] == "updated"
    assert out["previous_schedule"] == {"kind": "interval", "every": 5.0, "relative": False}
    assert out["new_schedule"] == {"kind": "interval", "every": 30.0, "relative": False}
    # The stored definition carries RedBeat's own shape, written by the mapper.
    stored = json.loads(redis.hashes["redbeat:job"]["definition"])
    assert stored["schedule"] == {"__type__": "interval", "every": 30.0, "relative": False}
    assert out["next_run_at_ts"] == 1000.0
    # The zset member is the entry KEY (what Beat loads), never the bare name.
    assert redis.zset == {"redbeat:job": 1000.0}


async def test_update_schedule_nothing_to_do_is_skipped(stub_app, tools):
    stub_app.clients.client = FakeRedis(hashes={"redbeat:job": {"definition": _DEF_5}})
    out = await tools.backend_update_schedule("job")
    assert out["status"] == "skipped"


async def test_enable_disable_and_delete_schedule(stub_app, tools):
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


async def test_enable_disable_not_found(stub_app, tools):
    stub_app.clients.client = FakeRedis()
    assert (await tools.backend_enable_schedule("nope"))["status"] == "not_found"
    assert (await tools.backend_disable_schedule("nope"))["status"] == "not_found"


async def test_schedule_exists_and_run_now(stub_app, tools):
    redis = FakeRedis(hashes={"redbeat:job": {"definition": "{}"}})
    stub_app.clients.client = redis
    assert await tools.backend_schedule_exists("job") is True
    assert await tools.backend_schedule_exists("other") is False
    out = await tools.backend_run_schedule_now("job")
    assert out["status"] == "queued"
    # Queued under the entry KEY: Beat loads zset members as redis keys.
    assert redis.zset == {"redbeat:job": 0}


async def test_run_schedule_now_missing_entry_is_not_found(stub_app, tools):
    """Queueing a nonexistent schedule must not report success: Beat would drop
    the dangling member without running anything."""
    redis = FakeRedis()
    stub_app.clients.client = redis
    out = await tools.backend_run_schedule_now("ghost")
    assert out == {"status": "not_found", "name": "ghost"}
    assert redis.zset == {}


async def test_list_schedules_reports_bare_names_scores_and_enabled(stub_app, tools):
    """Members arrive as entry keys; the reported name is the bare schedule name
    and ``enabled`` is read from each entry's definition."""
    stub_app.clients.client = FakeRedis(
        hashes={
            "redbeat:a": {"definition": _definition("a")},
            "redbeat:b": {"definition": _definition("b", enabled=False, tool="alerts")},
        },
        zset={"redbeat:a": 0.0, "redbeat:b": 1700000000.0},
    )
    out = await tools.backend_list_schedules()
    by_name = {row["name"]: row for row in out}
    assert set(by_name) == {"a", "b"}
    assert by_name["a"]["enabled"] is True
    assert by_name["a"]["next_run_at_ts"] == 0.0  # score 0 is "due at once" (run now), a timestamp
    assert by_name["a"]["next_run_at_iso"] == "1970-01-01T00:00:00+00:00"
    assert by_name["b"]["enabled"] is False
    assert by_name["b"]["next_run_at_ts"] == 1700000000.0
    assert by_name["b"]["next_run_at_iso"].startswith("2023-")
    # Typed rows: the fired tool and the cadence, never the stored kwargs.
    assert by_name["a"]["tool"] == "echo"
    assert by_name["b"]["tool"] == "alerts"
    assert by_name["a"]["cadence"] == {"kind": "interval", "every": 5.0, "relative": False}
    assert set(by_name["a"]) == {"name", "tool", "cadence", "enabled", "next_run_at_ts", "next_run_at_iso", "error"}


async def test_list_schedules_entry_without_the_tool_key_raises(stub_app, tools):
    stub_app.clients.client = FakeRedis(
        hashes={"redbeat:a": {"definition": json.dumps({"name": "a", "kwargs": {}, "schedule": _INTERVAL_5})}},
        zset={"redbeat:a": 0.0},
    )
    with pytest.raises(ValueError, match="carries no 'backend_tool_name'"):
        await tools.backend_list_schedules()


async def test_list_schedules_dangling_zset_member_raises(stub_app, tools):
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


async def test_list_schedules_omits_entry_deleted_after_snapshot(stub_app, tools):
    """An entry legitimately deleted between the zset snapshot and the hash
    read (member and hash both gone) is omitted — a consistent read of its
    absence, not a spurious dangling-member error."""
    stub_app.clients.client = _ConcurrentDeleteRedis(
        "redbeat:victim",
        hashes={"redbeat:live": {"definition": _definition("live")}},
        zset={"redbeat:live": 10.0, "redbeat:victim": 20.0},
    )
    out = await tools.backend_list_schedules()
    assert [row["name"] for row in out] == ["live"]


async def test_export_schedules_omits_entry_deleted_after_snapshot(stub_app, tools):
    """Same concurrent-delete semantics for the export marker tool."""
    live_definition = {
        "name": "live",
        "args": [],
        "kwargs": {_TOOL_ARG: "echo"},
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


async def test_list_schedules_tolerates_delete_then_recreate_around_the_read(stub_app, tools):
    """A delete plus atomic re-create straddling the per-member read must not
    raise: the store never held a dangling member, and the atomic
    definition+membership read only ever observes consistent states."""
    stub_app.clients.client = _DeleteThenRecreateRedis(
        "redbeat:victim",
        _definition("victim"),
        20.0,
        hashes={"redbeat:live": {"definition": _definition("live")}},
        zset={"redbeat:live": 10.0},
    )
    out = await tools.backend_list_schedules()
    assert [row["name"] for row in out] == ["live"]


async def test_export_includes_disabled_entry_absent_from_zset(stub_app, tools):
    """A disabled schedule keeps its ``redbeat:{name}`` hash but leaves the zset.
    Export must still emit it — it is
    re-enable-able, a live schedule — so the pattern-scan union covers what a
    zset-only walk would silently drop."""
    enabled_def = {
        "name": "enabled-one",
        "args": [],
        "kwargs": {_TOOL_ARG: "echo"},
        "schedule": {"__type__": "interval", "every": 30.0, "relative": False},
        "enabled": True,
    }
    disabled_def = {
        "name": "disabled-one",
        "args": [],
        "kwargs": {_TOOL_ARG: "alerts"},
        "schedule": {"__type__": "interval", "every": 60.0, "relative": False},
        "enabled": False,
    }
    stub_app.clients.client = FakeRedis(
        hashes={
            "redbeat:enabled-one": {"definition": json.dumps(enabled_def)},
            "redbeat:disabled-one": {"definition": json.dumps(disabled_def)},
        },
        # The disabled entry is NOT a zset member (removed on disable); only the
        # enabled one is.
        zset={"redbeat:enabled-one": 100.0},
    )
    exported = await tools.backend_export_schedules()
    by_name = {rec["name"]: rec for rec in exported}
    assert set(by_name) == {"enabled-one", "disabled-one"}
    assert by_name["disabled-one"]["enabled"] is False
    assert by_name["disabled-one"]["tool"] == "alerts"
    assert by_name["disabled-one"]["kwargs"] == {}


async def test_export_schedules_tolerates_delete_then_recreate_around_the_read(stub_app, tools):
    """Same delete-plus-re-create semantics for the export marker tool."""
    live_definition = {
        "name": "live",
        "args": [],
        "kwargs": {_TOOL_ARG: "echo"},
        "schedule": {"__type__": "interval", "every": 30.0, "relative": False},
        "enabled": True,
    }
    stub_app.clients.client = _DeleteThenRecreateRedis(
        "redbeat:victim",
        _definition("victim"),
        20.0,
        hashes={"redbeat:live": {"definition": json.dumps(live_definition)}},
        zset={"redbeat:live": 10.0},
    )
    exported = await tools.backend_export_schedules()
    assert [rec["name"] for rec in exported] == ["live"]


async def test_delete_schedule_removes_zset_member_before_hash(stub_app, tools):
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
def roundtrip_redis(stub_app, monkeypatch):
    redis = _FakeRedisRoundTrip()
    stub_app.clients.client = redis
    monkeypatch.setattr(schedule_store, "RedBeatSchedulerEntry", _fake_entry_cls(redis))
    return redis


_INTERVAL_RECORD = {
    "name": "nightly-sync",
    "tool": "my_tool",
    "args": [],
    "kwargs": {"payload": "x"},
    "cadence": {"kind": "interval", "every": 30.0, "relative": False},
    "enabled": True,
}

_CRONTAB_RECORD = {
    "name": "weekday-report",
    "tool": "report_tool",
    "args": [],
    "kwargs": {},
    "cadence": {
        "kind": "crontab",
        "minute": "0",
        "hour": "9",
        "day_of_month": "*",
        "month_of_year": "*",
        "day_of_week": "1",
    },
    "enabled": False,
}

_EMPTY_ERRORS: list = []


async def test_import_then_export_round_trips_interval_and_crontab(roundtrip_redis, tools):
    """Interval + crontab records survive an import/export cycle through ``ScheduleRecord``:
    the tool (stored under the backend's own non-default key), kwargs, cadence, and enabled
    all come back unchanged."""
    result = await tools.backend_import_schedules([_INTERVAL_RECORD, _CRONTAB_RECORD])
    assert result == {"created": 2, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": _EMPTY_ERRORS}

    stored = json.loads(roundtrip_redis.hashes["redbeat:nightly-sync"]["definition"])
    assert stored["kwargs"] == {"payload": "x", _TOOL_ARG: "my_tool"}

    exported = await tools.backend_export_schedules()
    by_name = {rec["name"]: rec for rec in exported}
    assert by_name["nightly-sync"] == _INTERVAL_RECORD
    assert by_name["weekday-report"] == _CRONTAB_RECORD


async def test_import_overwrite_replaces_existing(roundtrip_redis, tools):
    """Under ``overwrite`` a re-import of the same name replaces it, counted 'updated'."""
    first = await tools.backend_import_schedules([_INTERVAL_RECORD])
    assert first == {"created": 1, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": _EMPTY_ERRORS}

    changed = dict(_INTERVAL_RECORD, cadence={"kind": "interval", "every": 99.0, "relative": False})
    second = await tools.backend_import_schedules([changed], "overwrite")
    assert second == {"created": 0, "updated": 1, "skipped": 0, "skipped_existing": 0, "errors": _EMPTY_ERRORS}

    exported = await tools.backend_export_schedules()
    assert len(exported) == 1  # upsert, not a duplicate
    assert exported[0]["cadence"]["every"] == 99.0  # the replacement applied


async def test_import_skip_leaves_existing_untouched(roundtrip_redis, tools):
    """Under ``skip`` (the default the host forwards) an existing name is left in
    place — not re-applied — and counted in ``skipped_existing``."""
    first = await tools.backend_import_schedules([_INTERVAL_RECORD], "skip")
    assert first == {"created": 1, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": _EMPTY_ERRORS}

    changed = dict(_INTERVAL_RECORD, cadence={"kind": "interval", "every": 99.0, "relative": False})
    second = await tools.backend_import_schedules([changed], "skip")
    assert second == {"created": 0, "updated": 0, "skipped": 0, "skipped_existing": 1, "errors": _EMPTY_ERRORS}

    exported = await tools.backend_export_schedules()
    assert len(exported) == 1
    assert exported[0]["cadence"]["every"] == 30.0  # the original is untouched


async def test_import_an_old_format_record_is_a_per_row_error(roundtrip_redis, tools):
    """A row in the old ``schedule``/no-``tool`` shape fails ``ScheduleRecord`` and lands in
    'errors' loudly; valid siblings still import."""
    old = {
        "name": "broken",
        "kwargs": {_TOOL_ARG: "x"},
        "schedule": {"__type__": "interval", "every": 30.0, "relative": False},
        "enabled": True,
    }
    result = await tools.backend_import_schedules([_INTERVAL_RECORD, old])
    assert result["created"] == 1
    assert result["updated"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["index"] == 1
    assert result["errors"][0]["name"] == "broken"
    assert result["errors"][0]["error"]
    assert "redbeat:broken" not in roundtrip_redis.hashes


async def test_import_a_non_positive_interval_is_a_per_row_error(roundtrip_redis, tools):
    broken = dict(_INTERVAL_RECORD, name="bad-interval", cadence={"kind": "interval", "every": -5})
    result = await tools.backend_import_schedules([_INTERVAL_RECORD, broken])
    assert result["created"] == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["name"] == "bad-interval"
    assert "every" in result["errors"][0]["error"]
    assert "redbeat:bad-interval" not in roundtrip_redis.hashes


async def test_import_keeps_a_relative_interval(roundtrip_redis, tools):
    relative = dict(_INTERVAL_RECORD, cadence={"kind": "interval", "every": 30.0, "relative": True})
    result = await tools.backend_import_schedules([relative])
    assert result["errors"] == []
    stored = json.loads(roundtrip_redis.hashes["redbeat:nightly-sync"]["definition"])
    assert stored["schedule"] == {"__type__": "interval", "every": 30.0, "relative": True}
    (exported,) = await tools.backend_export_schedules()
    assert exported["cadence"]["relative"] is True


async def test_export_raises_when_definition_missing(roundtrip_redis, tools):
    """A zset member with no backing definition is an error, never skipped."""
    roundtrip_redis.zset["redbeat:ghost"] = 0.0  # listed but no hash written
    with pytest.raises(KeyError, match="ghost"):
        await tools.backend_export_schedules()


async def test_export_raises_on_an_entry_without_the_tool_key(roundtrip_redis, tools):
    definition = {"name": "orphan", "args": [], "kwargs": {}, "schedule": _INTERVAL_5, "enabled": True}
    roundtrip_redis.hashes["redbeat:orphan"] = {"definition": json.dumps(definition)}
    roundtrip_redis.zset["redbeat:orphan"] = 0.0
    with pytest.raises(ValueError, match="schedule 'orphan' carries no 'backend_tool_name'"):
        await tools.backend_export_schedules()


async def test_update_schedule_not_found(stub_app, tools):
    stub_app.clients.client = FakeRedis()
    out = await tools.backend_update_schedule("missing", new_schedule=10)
    assert out["status"] == "not_found"


async def test_update_schedule_cadence_alone_keeps_the_next_run(stub_app, tools):
    redis = FakeRedis(hashes={"redbeat:job": {"definition": _definition("job")}}, zset={"redbeat:job": 70.0})
    stub_app.clients.client = redis
    out = await tools.backend_update_schedule("job", new_schedule="0 9 * * 1")
    assert out["new_schedule"]["kind"] == "crontab"
    assert out["next_run_at_ts"] == 70.0
    assert redis.zset == {"redbeat:job": 70.0}


async def test_update_schedule_next_run_in_ms_takes_precedence(stub_app, tools):
    redis = FakeRedis(hashes={"redbeat:job": {"definition": _DEF_5}})
    stub_app.clients.client = redis
    out = await tools.backend_update_schedule("job", next_run_in_ms=0, next_run_at_ts=1.0)
    assert out["status"] == "updated"
    # next_run_in_ms wins over the absolute timestamp: the score is "now", not 1.0.
    assert redis.zset["redbeat:job"] > 1.0


def test_the_redbeat_store_client_takes_the_full_connection_settings(monkeypatch, stub_app):
    """The schedule store leases the RedBeat store with every ``CELERY_REDBEAT_*`` connection setting, read as bytes."""
    from tai42_kit.clients.impl.redis import RedisClient

    from tai42_backend_celery.core.settings import CelerySettings

    for key in ("CELERY_REDBEAT_REDIS_URL", "TAI_DEFAULT_REDIS_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CELERY_REDBEAT_REDIS_URL", "redis://beat:6379/5")
    monkeypatch.setenv("CELERY_REDBEAT_REDIS_MAX_CONNECTIONS", "7")
    monkeypatch.setenv("CELERY_REDBEAT_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("CELERY_REDBEAT_SOCKET_TIMEOUT", "3")
    monkeypatch.setattr(schedule_store, "celery_settings", CelerySettings)
    seen: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(
        stub_app.clients, "client_ctx", lambda client_cls, **kwargs: seen.append((client_cls, kwargs)) or object()
    )

    schedule_store._redbeat_redis()

    assert seen == [
        (
            RedisClient,
            {
                "url": "redis://beat:6379/5",
                "max_connections": 7,
                "decode_responses": False,
                "env_prefix": "CELERY_REDBEAT_",
                "retry_attempts": 2,
                "socket_timeout": 3.0,
            },
        )
    ]


# --- the replies, one shape on every backend ----------------------------


def _reply_definition(name: str, *, enabled: bool = True) -> str:
    return json.dumps(
        {
            "name": name,
            "task": "celery.tool_execution",
            "args": [],
            "kwargs": {_TOOL_ARG: "echo", "x": 1},
            "schedule": _INTERVAL_5,
            "enabled": enabled,
        }
    )


def _key(name: str) -> str:
    return celery_settings().redbeat_task_key(name)


def _tool(stub_app: Any, name: str) -> Any:
    return stub_app.tools.registered[name]


def test_disable_schedule_takes_only_the_name(stub_app):
    assert list(inspect.signature(_tool(stub_app, "backend_disable_schedule")).parameters) == ["name"]


async def test_run_schedule_now_replies_status_and_name(stub_app):
    stub_app.clients.client = FakeRedis(hashes={_key("job"): {"definition": _reply_definition("job")}})
    assert await _tool(stub_app, "backend_run_schedule_now")("job") == {"status": "queued", "name": "job"}


async def test_update_without_a_change_replies_the_one_skipped_text(stub_app):
    stub_app.clients.client = FakeRedis(hashes={_key("job"): {"definition": _reply_definition("job")}})
    assert await _tool(stub_app, "backend_update_schedule")("job") == {
        "status": "skipped",
        "name": "job",
        "message": "No changes provided; nothing updated.",
    }


async def test_update_of_an_absent_schedule_replies_status_and_name(stub_app):
    stub_app.clients.client = FakeRedis()
    assert await _tool(stub_app, "backend_update_schedule")("job", new_schedule=30) == {
        "status": "not_found",
        "name": "job",
    }


async def test_update_reply_carries_the_one_updated_shape(stub_app):
    stub_app.clients.client = FakeRedis(hashes={_key("job"): {"definition": _reply_definition("job")}})
    out = await _tool(stub_app, "backend_update_schedule")("job", next_run_at_ts=1000.0)
    assert set(out) == {"status", "name", "previous_schedule", "new_schedule", "next_run_at_ts", "next_run_at_iso"}


async def test_delete_of_an_absent_schedule_is_not_found(stub_app):
    stub_app.clients.client = FakeRedis()
    assert await _tool(stub_app, "backend_delete_schedule")("ghost") == {"status": "not_found", "name": "ghost"}


async def test_list_includes_a_disabled_schedule(stub_app):
    stub_app.clients.client = FakeRedis(
        hashes={
            _key("on"): {"definition": _reply_definition("on")},
            _key("off"): {"definition": _reply_definition("off", enabled=False)},
        },
        zset={_key("on"): 100.0},
    )
    rows = await _tool(stub_app, "backend_list_schedules")()
    assert {row["name"]: row["enabled"] for row in rows} == {"on": True, "off": False}
    assert {row["name"]: row["next_run_at_ts"] for row in rows} == {"on": 100.0, "off": None}


async def test_get_replies_the_schedule_detail(stub_app):
    stub_app.clients.client = FakeRedis(
        hashes={_key("job"): {"definition": _reply_definition("job")}}, zset={_key("job"): 0.0}
    )
    assert await _tool(stub_app, "backend_get_schedule")("job") == {
        "name": "job",
        "tool": "echo",
        "args": [],
        "kwargs": {"x": 1},
        "cadence": {"kind": "interval", "every": 5.0, "relative": False},
        "enabled": True,
        "next_run_at_ts": 0.0,
        "next_run_at_iso": "1970-01-01T00:00:00+00:00",
        "error": None,
    }
