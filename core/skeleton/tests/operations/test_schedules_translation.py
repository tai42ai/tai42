"""The schedule door against a fake scheduling backend, driven through the REAL
``run_tool`` binding.

Every case boots an ``app_context`` carrying a base tool (``demo_report``, with the fake
backend's ``schedule_task`` branch), a branch-less tool (``plain_task``), and the fake
backend's protocol tools (list/delete/export/import), then calls the operations directly.
The dispatch runs the real binding, so the typed cadence is VALIDATED against the branch
tool's signature. The fake backend stores each job under its OWN tool-name key (not the
platform default), so the platform reads a schedule's tool only from the contract's
``tool`` field. The base tool records every invocation, so a guard that must never reach
it is proved by an empty record.

The app re-imports the fixture modules for each ``app_context``, so the live
capture lists are read back through :func:`importlib.import_module` inside the
context rather than from a module reference bound at collection time.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest
from tai42_contract.backend import SCHEDULE_CADENCE_KEY, CrontabCadence, IntervalCadence, ScheduleRecord
from tai42_contract.tools import ReferenceHolder

from tai42_skeleton.app.instance import app
from tai42_skeleton.backup.sections import _export_schedules, _import_schedules
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import NotFoundError, OperationFailedError
from tai42_skeleton.operations import schedules as schedules_ops
from tai42_skeleton.operations import tools as tools_ops
from tai42_skeleton.tools import holder_kinds, platform_referees

_TOOLS_MODULE = "tests.app._fixtures.schedule_tools"
_EXT_MODULE = "tests.app._fixtures.schedule_ext"


def _captured() -> list[dict]:
    return importlib.import_module(_EXT_MODULE).captured_schedules


def _demo_calls() -> list[dict]:
    return importlib.import_module(_TOOLS_MODULE).demo_calls


def _backend() -> Any:
    return importlib.import_module(_TOOLS_MODULE)


@pytest.fixture(autouse=True)
def _clean_server():
    """The singleton FastMCP server outlives one ``app_context``; clear it around each
    test so a prior bind cannot collide."""

    async def _clear() -> None:
        provider = app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    yield
    asyncio.run(_clear())


def _manifest() -> Manifest:
    return Manifest.model_validate(
        {
            "extensions_modules": [_EXT_MODULE],
            "tools": [
                {
                    "title": "sched-fxt",
                    "module": _TOOLS_MODULE,
                    "include": [
                        "demo_report",
                        "plain_task",
                        "backend_list_schedules",
                        "backend_delete_schedule",
                        "backend_export_schedules",
                        "backend_import_schedules",
                    ],
                    "extensions": {"demo_report": [["schedule_task"]]},
                }
            ],
        }
    )


_CRON = CrontabCadence(minute="0", hour="9")


async def test_a_typed_crontab_dispatches_the_vehicle_with_the_cadence_key():
    async with app.app_context(_manifest()):
        result = await schedules_ops.create_schedule("demo_report", {"payload": "x"}, cadence=_CRON)
        captured = list(_captured())
        calls = list(_demo_calls())
        rows = await schedules_ops.list_schedules()
    assert len(captured) == 1
    assert captured[0]["cadence"] == _CRON
    assert captured[0]["name"].startswith("demo_report_")
    assert captured[0]["kwargs"] == {"payload": "x"}
    assert result == {"scheduled": captured[0]["name"]}
    assert calls == []
    assert [(row["name"], row["tool"], row["cadence"]) for row in rows] == [
        (captured[0]["name"], "demo_report", _CRON.model_dump(mode="json"))
    ]


async def test_a_typed_interval_dispatches_the_vehicle():
    async with app.app_context(_manifest()):
        await schedules_ops.create_schedule("demo_report", {}, cadence=IntervalCadence(every=30))
        captured = list(_captured())
    assert captured[0]["cadence"] == IntervalCadence(every=30.0)


async def test_the_branch_receives_the_dumped_cadence_under_the_contract_key():
    async with app.app_context(_manifest()):
        dispatch, arguments = await schedules_ops._resolve_schedule_dispatch("demo_report", {}, _CRON, "morning")
    assert dispatch == "demo_report_schedule_task"
    assert arguments[SCHEDULE_CADENCE_KEY] == _CRON.model_dump(mode="json")


async def test_explicit_schedule_name_is_honored():
    async with app.app_context(_manifest()):
        await schedules_ops.create_schedule("demo_report", {}, cadence=_CRON, schedule_name="morning")
        captured = list(_captured())
    assert captured[0]["name"] == "morning"


async def test_the_derived_name_fingerprints_the_cadence():
    a = schedules_ops._derive_schedule_name("demo_report", {}, _CRON)
    b = schedules_ops._derive_schedule_name("demo_report", {}, CrontabCadence(minute="5", hour="9"))
    assert a.startswith("demo_report_")
    assert a != b
    assert a == schedules_ops._derive_schedule_name("demo_report", {}, CrontabCadence(minute="0", hour="9"))


async def test_missing_branch_raises_the_extension_error():
    # ``plain_task`` carries no ``schedule_task`` branch; scheduling it on a cadence names
    # the extension, and never runs the tool.
    async with app.app_context(_manifest()):
        with pytest.raises(NotFoundError) as caught:
            await schedules_ops.create_schedule("plain_task", {}, cadence=_CRON)
        captured = list(_captured())
    assert caught.value.message == (
        "tool 'plain_task' cannot be scheduled: its schedule vehicle is not registered — "
        "the tool must carry the backend's 'schedule_task' extension"
    )
    assert captured == []


async def test_no_cadence_dispatches_the_named_tool_once():
    # No cadence on a base tool dispatches the named tool once, unchanged, and registers
    # no schedule.
    async with app.app_context(_manifest()):
        result = await schedules_ops.create_schedule("demo_report", {"payload": "y"})
        calls = list(_demo_calls())
        captured = list(_captured())
    assert result == "y"
    assert calls == [{"payload": "y"}]
    assert captured == []


async def test_list_refuses_rows_that_are_not_schedule_list_rows():
    async with app.app_context(_manifest()):
        _backend().list_rows_override = [
            {"name": "n", "tool": "t", "cadence": {"kind": "interval", "every": 5}, "enabled": True, "kwargs": {}}
        ]
        try:
            with pytest.raises(OperationFailedError) as caught:
                await schedules_ops.list_schedules()
        finally:
            _backend().list_rows_override = None
    assert caught.value.message.startswith(
        "the scheduling backend's backend_list_schedules returned rows that are not ScheduleListRow: 0.kwargs"
    )


async def test_export_reads_the_tool_the_backend_splits_from_its_own_key():
    async with app.app_context(_manifest()):
        await schedules_ops.create_schedule("demo_report", {"payload": "x"}, cadence=_CRON, schedule_name="m")
        records = await schedules_ops.export_schedules_raw()
        holders = await platform_referees._schedule_referee("demo_report")
        none = await platform_referees._schedule_referee("plain_task")
    assert records == [
        ScheduleRecord(name="m", tool="demo_report", kwargs={"payload": "x"}, cadence=_CRON, enabled=True)
    ]
    assert holders == [ReferenceHolder(kind=holder_kinds.SCHEDULE, name="m", summary="schedule 'm'")]
    assert none == []


async def test_the_backup_section_round_trips_through_the_fake_backend():
    async with app.app_context(_manifest()):
        await schedules_ops.create_schedule("demo_report", {}, cadence=_CRON, schedule_name="m")
        exported = await _export_schedules()
        _backend().store.clear()
        report = await _import_schedules(exported)
        again = await _import_schedules(exported)
        listed = await schedules_ops.list_schedules()
    assert exported[0]["tool"] == "demo_report"
    assert (report.created, report.updated, report.skipped) == (1, 0, 0)
    assert again.details == {"skipped_existing": 1}
    assert [row["name"] for row in listed] == ["m"]


async def test_a_tool_is_schedulable_when_its_vehicle_is_registered():
    async with app.app_context(_manifest()):
        rows = {row["name"]: row["schedulable"] for row in await tools_ops.tool_tags()}
    assert rows["demo_report"] is True
    assert rows["plain_task"] is False
    assert rows["demo_report_schedule_task"] is False


_SUBJECT = {"target_kind": "agent", "target_name": "a-42", "kind": "job", "key": "j1"}


async def test_a_subject_inside_tool_kwargs_fails_for_a_tool_without_a_subject_parameter():
    # A client that sends the schedule's subject inside ``tool_kwargs`` hands it to the tool as an
    # argument; ``demo_report`` has no ``subject`` parameter, so the real binding refuses the create.
    async with app.app_context(_manifest()):
        with pytest.raises(OperationFailedError):
            await schedules_ops.create_schedule("demo_report", {"subject": _SUBJECT}, cadence=_CRON)
        captured = list(_captured())
    assert captured == []


async def test_a_top_level_subject_is_stamped_and_never_reaches_the_tool_arguments():
    from tai42_contract.states import StateSubject
    from tai42_kit.utils.schedule_subject import SCHEDULE_SUBJECT_ARG

    async with app.app_context(_manifest()):
        await schedules_ops.create_schedule(
            "demo_report", {"payload": "x"}, cadence=_CRON, subject=StateSubject.model_validate(_SUBJECT)
        )
        captured = list(_captured())
    assert captured[0]["kwargs"] == {"payload": "x", SCHEDULE_SUBJECT_ARG: _SUBJECT}
