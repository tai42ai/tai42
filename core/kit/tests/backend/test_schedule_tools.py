"""The ten schedule tools the kit registers over a backend's schedule store: arguments and replies."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.backend import CrontabCadence, IntervalCadence, ScheduleRowError

from tai42_kit.backend import SCHEDULE_TOOLS, UnsupportedBackendTool, register_schedule_tools
from tai42_kit.backend.schedule_tools import NO_CHANGES, split_fired_tool
from tai42_kit.utils.lc.signature_util import resolved_type_hints

from .surface_fakes import OWNER, TOOL_ARG, FakeStore, SurfaceApp, SurfaceTools, surface_settings


def _register(store: FakeStore) -> SurfaceTools:
    app = SurfaceApp()
    with tai42_app.bound(app):
        register_schedule_tools(store, surface_settings, "fake", OWNER)
    return app.tools


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def tools(store: FakeStore) -> SurfaceTools:
    return _register(store)


def test_the_ten_tools_are_registered_as_the_owning_backends_tagged_backend(tools: SurfaceTools) -> None:
    assert set(tools.registered) == set(SCHEDULE_TOOLS)
    for name, registered in tools.registered.items():
        assert registered.tags == {"backend"}, name
        assert registered.module == OWNER, name
        # The annotations are evaluated, so they resolve in the module the tool names.
        resolved_type_hints(registered.func)


async def test_list_replies_a_row_per_schedule_without_its_arguments(tools: SurfaceTools, store: FakeStore) -> None:
    store.seed("s1", text="hi")
    store.schedules["s1"] = store.schedules["s1"].model_copy(
        update={"error": ScheduleRowError(message="probe failed", at_iso="2026-01-01T00:00:00+00:00")}
    )
    assert await tools["backend_list_schedules"]() == [
        {
            "name": "s1",
            "tool": "sample_tool",
            "cadence": {"kind": "interval", "every": 60.0, "relative": False},
            "enabled": True,
            "next_run_at_ts": 100.0,
            "next_run_at_iso": "1970-01-01T00:01:40+00:00",
            "error": {"message": "probe failed", "at_iso": "2026-01-01T00:00:00+00:00"},
        }
    ]


async def test_get_replies_the_detail_with_the_kwargs_less_the_tool_name(tools: SurfaceTools, store: FakeStore) -> None:
    store.seed("s1", text="hi")
    detail = await tools["backend_get_schedule"]("s1")
    assert detail["tool"] == "sample_tool"
    assert detail["args"] == [1]
    assert detail["kwargs"] == {"text": "hi"}
    assert detail["next_run_at_iso"] == "1970-01-01T00:01:40+00:00"
    assert await tools["backend_get_schedule"]("nope") == {"status": "not_found", "name": "nope"}


async def test_a_stored_schedule_without_the_tool_name_raises(tools: SurfaceTools, store: FakeStore) -> None:
    store.seed("s1")
    del store.schedules["s1"].kwargs[TOOL_ARG]
    with pytest.raises(ValueError, match="carries no 'backend_tool_name'"):
        await tools["backend_list_schedules"]()


def test_split_fired_tool_leaves_the_kwargs_untouched() -> None:
    kwargs = {TOOL_ARG: "t", "a": 1}
    assert split_fired_tool("s1", kwargs, TOOL_ARG) == ("t", {"a": 1})
    assert kwargs == {TOOL_ARG: "t", "a": 1}


async def test_update_of_a_new_cadence_replies_both_cadences(tools: SurfaceTools, store: FakeStore) -> None:
    store.seed("s1")
    reply = await tools["backend_update_schedule"]("s1", new_schedule="0 9 * * 1")
    assert reply == {
        "status": "updated",
        "name": "s1",
        "previous_schedule": {"kind": "interval", "every": 60.0, "relative": False},
        "new_schedule": {
            "kind": "crontab",
            "minute": "0",
            "hour": "9",
            "day_of_month": "*",
            "month_of_year": "*",
            "day_of_week": "1",
        },
        "next_run_at_ts": 500.0,
        "next_run_at_iso": "1970-01-01T00:08:20+00:00",
    }
    assert store.schedules["s1"].cadence == CrontabCadence(minute="0", hour="9", day_of_week="1")


async def test_update_next_run_in_ms_wins_over_next_run_at_ts(tools: SurfaceTools, store: FakeStore) -> None:
    store.seed("s1")
    reply = await tools["backend_update_schedule"]("s1", next_run_in_ms=5000, next_run_at_ts=1.0)
    assert reply["next_run_at_ts"] == pytest.approx(datetime.now(UTC).timestamp() + 5, abs=5)
    assert store.schedules["s1"].cadence == IntervalCadence(every=60)


async def test_update_next_run_at_ts_alone_moves_the_next_run(tools: SurfaceTools, store: FakeStore) -> None:
    store.seed("s1")
    reply = await tools["backend_update_schedule"]("s1", next_run_at_ts=2_000_000_000.0)
    assert reply["next_run_at_ts"] == 2_000_000_000.0


async def test_update_without_a_change_is_skipped(tools: SurfaceTools, store: FakeStore) -> None:
    store.seed("s1")
    assert await tools["backend_update_schedule"]("s1") == {"status": "skipped", "name": "s1", "message": NO_CHANGES}


async def test_update_of_an_unknown_schedule_is_not_found(tools: SurfaceTools) -> None:
    assert await tools["backend_update_schedule"]("nope", new_schedule=30) == {"status": "not_found", "name": "nope"}


@pytest.mark.parametrize("flag", ["vanish_on_update", "vanish_after_update"])
async def test_update_of_a_schedule_deleted_meanwhile_is_not_found(
    tools: SurfaceTools, store: FakeStore, flag: str
) -> None:
    store.seed("s1")
    setattr(store, flag, True)
    assert await tools["backend_update_schedule"]("s1", new_schedule=30) == {"status": "not_found", "name": "s1"}


async def test_update_to_a_relative_interval_the_store_does_not_honour_is_refused(
    tools: SurfaceTools, store: FakeStore
) -> None:
    store.seed("s1")
    with pytest.raises(ValueError, match="does not support relative interval schedules"):
        await tools["backend_update_schedule"]("s1", new_schedule={"kind": "interval", "every": 30, "relative": True})
    assert store.schedules["s1"].cadence == IntervalCadence(every=60)


async def test_enable_and_disable_write_the_flag(tools: SurfaceTools, store: FakeStore) -> None:
    store.seed("s1", enabled=False)
    assert await tools["backend_enable_schedule"]("s1") == {"status": "enabled", "name": "s1"}
    assert store.schedules["s1"].enabled is True
    assert await tools["backend_disable_schedule"]("s1") == {"status": "disabled", "name": "s1"}
    assert store.schedules["s1"].enabled is False
    assert await tools["backend_enable_schedule"]("nope") == {"status": "not_found", "name": "nope"}
    assert await tools["backend_disable_schedule"]("nope") == {"status": "not_found", "name": "nope"}


async def test_a_store_holding_no_disabled_schedule_enables_without_a_write_and_refuses_a_disable() -> None:
    store = FakeStore(holds_disabled=False)
    tools = _register(store)
    store.seed("s1")
    assert await tools["backend_enable_schedule"]("s1") == {"status": "enabled", "name": "s1"}
    assert await tools["backend_enable_schedule"]("nope") == {"status": "not_found", "name": "nope"}
    with pytest.raises(UnsupportedBackendTool, match=r"backend 'fake' does not support backend_disable_schedule$"):
        await tools["backend_disable_schedule"]("s1")
    assert store.schedules["s1"].enabled is True


async def test_run_now_exists_and_delete(tools: SurfaceTools, store: FakeStore) -> None:
    store.seed("s1")
    assert await tools["backend_run_schedule_now"]("s1") == {"status": "queued", "name": "s1"}
    assert store.ran == ["s1"]
    assert await tools["backend_run_schedule_now"]("nope") == {"status": "not_found", "name": "nope"}
    assert await tools["backend_schedule_exists"]("s1") is True
    assert await tools["backend_delete_schedule"]("s1") == {"status": "deleted", "name": "s1"}
    assert await tools["backend_schedule_exists"]("s1") is False
    assert await tools["backend_delete_schedule"]("s1") == {"status": "not_found", "name": "s1"}


async def test_export_replies_records_import_takes_back(tools: SurfaceTools, store: FakeStore) -> None:
    store.seed("s1", text="hi")
    exported = await tools["backend_export_schedules"]()
    assert exported == [
        {
            "name": "s1",
            "tool": "sample_tool",
            "args": [1],
            "kwargs": {"text": "hi"},
            "cadence": {"kind": "interval", "every": 60.0, "relative": False},
            "enabled": True,
        }
    ]
    store.schedules.clear()
    report = await tools["backend_import_schedules"](exported)
    assert report == {"created": 1, "updated": 0, "skipped": 0, "skipped_existing": 0, "errors": []}
    assert store.schedules["s1"].kwargs == {TOOL_ARG: "sample_tool", "text": "hi"}


async def test_import_skips_or_overwrites_an_existing_name(tools: SurfaceTools, store: FakeStore) -> None:
    store.seed("s1")
    (record,) = await tools["backend_export_schedules"]()
    skipped = await tools["backend_import_schedules"]([record])
    assert skipped == {"created": 0, "updated": 0, "skipped": 0, "skipped_existing": 1, "errors": []}
    assert store.writes == []
    overwritten = await tools["backend_import_schedules"]([record], "overwrite")
    assert overwritten == {"created": 0, "updated": 1, "skipped": 0, "skipped_existing": 0, "errors": []}


async def test_import_reports_each_bad_record_and_imports_the_rest(tools: SurfaceTools, store: FakeStore) -> None:
    good = {"name": "ok", "tool": "t", "cadence": {"kind": "interval", "every": 60}, "enabled": True}
    relative = {**good, "name": "rel", "cadence": {"kind": "interval", "every": 60, "relative": True}}
    store.fail_write = "boom"
    failing = {**good, "name": "boom"}
    report = await tools["backend_import_schedules"](["not-a-dict", {"name": "bad"}, relative, failing, good])
    assert report["created"] == 1
    assert [(row["index"], row["name"]) for row in report["errors"]] == [
        (0, None),
        (1, "bad"),
        (2, "rel"),
        (3, "boom"),
    ]
    assert "write of boom failed" in report["errors"][3]["error"]
    assert list(store.schedules) == ["ok"]


async def test_import_refuses_a_disabled_record_on_a_store_holding_none() -> None:
    store = FakeStore(holds_disabled=False)
    tools = _register(store)
    record = {"name": "off", "tool": "t", "cadence": {"kind": "interval", "every": 60}, "enabled": False}
    report = await tools["backend_import_schedules"]([record])
    assert report["skipped"] == 1
    assert report["errors"] == [
        {
            "index": 0,
            "name": "off",
            "error": "schedule 'off' has enabled=False; the fake backend has no disabled-schedule "
            "representation, so it was not created as an active schedule",
        }
    ]
    assert store.writes == []
