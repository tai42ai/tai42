"""The create door's friendly-cadence translation, driven through the REAL
``run_tool`` binding.

Every case boots an ``app_context`` carrying a base tool (``demo_report``, with a
``schedule_task`` branch), a branch-less tool (``plain_task``), and the two
scheduling markers, then calls ``create_schedule`` directly. The dispatch runs the
real binding, so a translated cadence is VALIDATED against the branch tool's
signature — the seam the fake accept-anything stand-ins could not exercise. The
branch captures the schedule it received; the base tool records every invocation,
so a guard that must never reach the base tool is proved by an empty record.

The app re-imports the fixture modules for each ``app_context``, so the live
capture lists are read back through :func:`importlib.import_module` inside the
context rather than from a module reference bound at collection time.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import BadRequestError, NotFoundError
from tai42_skeleton.operations import schedules as schedules_ops

_TOOLS_MODULE = "tests.app._fixtures.schedule_tools"
_EXT_MODULE = "tests.app._fixtures.schedule_ext"


def _captured() -> list[dict]:
    return importlib.import_module(_EXT_MODULE).captured_schedules


def _demo_calls() -> list[dict]:
    return importlib.import_module(_TOOLS_MODULE).demo_calls


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
                    "include": ["demo_report", "plain_task", "backend_list_schedules", "backend_delete_schedule"],
                    "extensions": {"demo_report": [["schedule_task"]]},
                }
            ],
        }
    )


async def test_documented_cron_form_translates_and_dispatches_the_branch():
    # ``tai schedules add demo_report --schedule-kw cron='0 9 * * *'`` verbatim: the
    # cron STRING is translated onto the branch's ``backend_schedule`` and the base tool
    # is never run once.
    async with app.app_context(_manifest()):
        result = await schedules_ops.create_schedule("demo_report", {}, {"cron": "0 9 * * *"})
        captured = list(_captured())
        calls = list(_demo_calls())
    assert len(captured) == 1
    assert captured[0]["backend_schedule"] == "0 9 * * *"
    assert captured[0]["name"].startswith("demo_report_")
    assert result == {"scheduled": captured[0]["name"], "backend_schedule": "0 9 * * *"}
    assert calls == []


async def test_structured_crontab_fields_translate_to_a_crontab_dict():
    # Structured hour/minute become ``{"type": "crontab", ...}`` — the shape
    # ``normalize_schedule``'s crontab branch accepts — and the tool kwargs ride along.
    async with app.app_context(_manifest()):
        await schedules_ops.create_schedule("demo_report", {"payload": "x"}, {"hour": "9", "minute": "0"})
        captured = list(_captured())
        calls = list(_demo_calls())
    assert captured[0]["backend_schedule"] == {"type": "crontab", "hour": "9", "minute": "0"}
    assert captured[0]["kwargs"] == {"payload": "x"}
    assert calls == []


async def test_explicit_schedule_name_is_honored():
    async with app.app_context(_manifest()):
        await schedules_ops.create_schedule(
            "demo_report", {}, {"cron": "0 9 * * *", "backend_schedule_name": "morning"}
        )
        captured = list(_captured())
    assert captured[0]["name"] == "morning"


async def test_missing_branch_raises_the_extension_error():
    # ``plain_task`` carries no ``schedule_task`` branch; scheduling it on a cadence names
    # the required vehicle and the extension, and never runs the tool.
    async with app.app_context(_manifest()):
        with pytest.raises(NotFoundError) as caught:
            await schedules_ops.create_schedule("plain_task", {}, {"cron": "0 9 * * *"})
        captured = list(_captured())
    assert "plain_task_schedule_task" in caught.value.message
    assert "schedule_task" in caught.value.message
    assert captured == []


async def test_stray_key_is_rejected_and_the_tool_is_never_invoked():
    # An unrecognized schedule kwarg on a base tool is a loud 400 — the door never merges
    # it into the arguments and never dispatches, so the base tool runs zero times.
    async with app.app_context(_manifest()):
        with pytest.raises(BadRequestError) as caught:
            await schedules_ops.create_schedule("demo_report", {}, {"bogus": 1})
        calls = list(_demo_calls())
        captured = list(_captured())
    assert "bogus" in caught.value.message
    assert calls == []
    assert captured == []


async def test_numeric_cron_is_rejected_and_the_tool_is_never_invoked():
    # A non-string ``cron`` on a base tool is a loud 400 — left unguarded a numeric value
    # slips through ``normalize_schedule``'s integer branch as an INTERVAL of that many
    # seconds, a silent reinterpretation of a crontab field. The door never translates it
    # and never dispatches, so neither the branch nor the base tool runs.
    async with app.app_context(_manifest()):
        with pytest.raises(BadRequestError) as caught:
            await schedules_ops.create_schedule("demo_report", {}, {"cron": 300})
        calls = list(_demo_calls())
        captured = list(_captured())
    assert "cron" in caught.value.message
    assert calls == []
    assert captured == []


async def test_both_cadence_forms_given_is_ambiguous():
    async with app.app_context(_manifest()):
        with pytest.raises(BadRequestError, match="ambiguous"):
            await schedules_ops.create_schedule("demo_report", {}, {"cron": "* * * * *", "hour": "9"})
        calls = list(_demo_calls())
        captured = list(_captured())
    assert calls == []
    assert captured == []


async def test_no_cadence_dispatches_the_named_tool_once():
    # An empty ``schedule_kwargs`` on a base tool keeps today's behavior: the named tool
    # is dispatched once, unchanged, and no schedule is registered.
    async with app.app_context(_manifest()):
        result = await schedules_ops.create_schedule("demo_report", {"payload": "y"}, {})
        calls = list(_demo_calls())
        captured = list(_captured())
    assert result == "y"
    assert calls == [{"payload": "y"}]
    assert captured == []


async def test_expert_shape_passes_through_unchanged():
    # Naming the branch tool with the expert ``backend_schedule`` key dispatches it
    # byte-unchanged: no translation, the schedule is taken as given.
    async with app.app_context(_manifest()):
        result = await schedules_ops.create_schedule(
            "demo_report_schedule_task",
            {"payload": "x"},
            {"backend_schedule_name": "nm", "backend_schedule": 5},
        )
        captured = list(_captured())
    assert captured[0] == {"name": "nm", "backend_schedule": 5, "kwargs": {"payload": "x"}}
    assert result == {"scheduled": "nm", "backend_schedule": 5}
