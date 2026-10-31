"""A job's profile apply and another job's config write that waits for it, both run in the worker process.

A backend job reaches both doors through a projected operation tool: the ``apply_profile``
operation (``ConfigService.apply_replace_env``) and the ``write_env`` operation
(``ConfigService.apply_env_change``), run by name by the job (a schedule fire, an
``async_task`` branch or a callback's follow-up tool). An in-process pool runs each job inside
a fork-gate job span. The profile apply rebuilds under the write exclusion; the write that
arrives meanwhile waits on that exclusion with its own job span open.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest
from tai42_kit.fork_gate import fork_gate

from .fake_pipeline import _reset_settings_after  # noqa: F401  — autouse fixture, active on import
from .test_config_write_lock import _reached, _settle, _World

_JOB_SPAN_WAIT = 60.0


async def test_a_jobs_profile_apply_does_not_wait_on_a_job_queued_behind_its_write_exclusion(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The profile apply's rebuild starts at once, with no quiesce error, while the other job's
    write waits for the apply's write exclusion; the write then runs after the apply."""
    world = _World(monkeypatch)
    holds_the_exclusion, write_queued = asyncio.Event(), asyncio.Event()
    build_entered = asyncio.Event()
    timings: dict[str, float] = {}

    async def inside_the_exclusion() -> None:
        world.during = None
        holds_the_exclusion.set()
        await write_queued.wait()

    world.during = inside_the_exclusion

    async def profile_job() -> Any:
        async with fork_gate.job_span_async(timeout=_JOB_SPAN_WAIT):
            return await world.apply_profile(world.service(), {"M_PICK": "m_b"}, world.build(entered=build_entered))

    async def write_job() -> Any:
        async with fork_gate.job_span_async(timeout=_JOB_SPAN_WAIT):
            return await world.service().apply_env_change({"OTHER": "v"})

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        profile = asyncio.create_task(profile_job())
        await _reached(holds_the_exclusion, "the profile apply's write exclusion")
        write = asyncio.create_task(write_job())
        await _settle()
        queued_at = time.monotonic()
        write_queued.set()

        await asyncio.wait_for(build_entered.wait(), 90.0)
        timings["build"] = time.monotonic() - queued_at
        await asyncio.wait_for(asyncio.gather(profile, write), 90.0)

    assert world.log == ["replace_env", "write_env"]
    assert timings["build"] < 3.0
    assert not caplog.records
    assert not fork_gate.blocked
