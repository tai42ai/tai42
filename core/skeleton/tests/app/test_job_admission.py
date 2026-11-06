"""A backend job running on the serving loop is admitted on the live serving generation as one unit of its work.

Under a backend whose jobs run on the serving loop (a green pool, or a worker that runs its jobs
on the host's loop), the kit's ``dispatched_job`` around a job's body, a callback run's tool call
included, enters the app's ``admitted_job`` door. The job is then counted in flight like a served
request: a reload's retire waits for it, within the drain budget, before it closes the
generation's loop-bound resources; a job that drives the reload gate or waits on a recorded lock
is recorded as such, as a request is. A job on any other event loop is not the serving
generation's work and records nothing.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest
from tai42_contract.app import tai42_app
from tai42_kit.backend.job_run import dispatched_job
from tai42_kit.clients.base import advance_client_epoch
from tai42_kit.loop_bound import LoopRegistryMap, ResourceRegistry

from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app import epoch_retire as epoch_retire_mod
from tai42_skeleton.app import instance
from tai42_skeleton.app.admission import current_admission, mark_current_request_drain_exempt
from tai42_skeleton.app.epoch import current_epoch
from tai42_skeleton.app.epoch_retire import retire_epoch
from tai42_skeleton.app.reload_gate import reload_gate

from .test_epoch import _install_boot, _reset_epoch_state  # noqa: F401 - _reset_epoch_state is the autouse fixture

pytestmark = pytest.mark.usefixtures("serving_here")


class _Job:
    """A backend job that caches a loop-bound resource on the serving loop and runs until told to end.

    ``events`` notes, in order, the resource's close and the job's end (with whether its resource
    was still open when it ended).
    """

    def __init__(self) -> None:
        self.events: list[str] = []
        self.registries: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry)
        self.cached = asyncio.Event()
        self.may_end = asyncio.Event()
        self._registry: ResourceRegistry | None = None

    async def cache_resource(self) -> None:
        async def _close() -> None:
            self.events.append("resource closed")

        async def _factory() -> tuple[object, Callable[[], Awaitable[None]]]:
            return object(), _close

        self._registry = self.registries.current()
        await self._registry._get_or_init_resource("saver", _factory)
        self.cached.set()

    def note_end(self) -> None:
        assert self._registry is not None
        state = "open" if self._registry.has_live_resources else "closed"
        self.events.append(f"job ended, its resource {state}")


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def test_a_retire_waits_for_a_job_on_the_serving_loop_before_closing_its_resources() -> None:
    _install_boot()
    old = current_epoch()
    job = _Job()

    async def _run() -> None:
        async with dispatched_job(budget=60):
            await job.cache_resource()
            await job.may_end.wait()
            job.note_end()

    running = asyncio.create_task(_run())
    await job.cached.wait()
    assert old.in_flight == 1

    retired = advance_client_epoch()
    retire = asyncio.create_task(retire_epoch(old, retired, 0.5))
    await asyncio.sleep(0.05)
    assert not retire.done(), "the retire returned under a running job"
    assert job.events == []

    job.may_end.set()
    async with asyncio.timeout(2.0):
        await retire
        await running
    assert job.events == ["job ended, its resource open", "resource closed"]


async def test_a_reload_the_job_drives_defers_the_close_until_the_job_has_ended() -> None:
    _install_boot()
    old = current_epoch()
    job = _Job()
    retire_returned = asyncio.Event()

    async def _run() -> None:
        with tai42_app.lifecycle.admitted_job():
            await job.cache_resource()
            async with reload_gate.driving():
                assert old.driving == {current_admission()}
                await retire_epoch(old, advance_client_epoch(), 0.5)
                retire_returned.set()
            await job.may_end.wait()
            job.note_end()

    running = asyncio.create_task(_run())
    async with asyncio.timeout(2.0):
        await retire_returned.wait()
    [tail] = epoch_retire_mod.deferred_retires()
    assert old.engaged
    await _settle()
    assert job.events == [], "the deferred tail closed under the job driving its reload"

    job.may_end.set()
    async with asyncio.timeout(2.0):
        await running
        await tail
    assert job.events == ["job ended, its resource open", "resource closed"]


async def test_a_job_waiting_on_fleet_work_is_not_counted_while_it_waits() -> None:
    _install_boot()
    old = current_epoch()
    job = _Job()
    in_wait = asyncio.Event()
    wait_ends = asyncio.Event()
    counts: list[int] = []
    records: list[object] = []

    async def _run() -> None:
        with tai42_app.lifecycle.admitted_job():
            records.append(current_admission())
            await job.cache_resource()
            counts.append(old.in_flight)
            async with tai42_app.lifecycle.waiting_on_fleet_work():
                counts.append(old.in_flight)
                in_wait.set()
                await wait_ends.wait()
            counts.append(old.in_flight)
            await job.may_end.wait()
            job.note_end()

    running = asyncio.create_task(_run())
    await in_wait.wait()
    assert old.waiting == set(records)
    assert records != [None]

    # The synchronous drain defers to the tail at once instead of spending its budget on the waiter.
    async with asyncio.timeout(1.0):
        await retire_epoch(old, advance_client_epoch(), 5.0)
    [tail] = epoch_retire_mod.deferred_retires()

    wait_ends.set()
    await _settle()
    assert counts == [1, 0, 1]
    assert job.events == []

    job.may_end.set()
    async with asyncio.timeout(2.0):
        await running
        await tail
    assert job.events == ["job ended, its resource open", "resource closed"]


async def test_a_job_stays_counted_when_it_asks_for_the_drain_exemption_of_a_served_stream() -> None:
    _install_boot()
    old = current_epoch()

    with tai42_app.lifecycle.admitted_job():
        record = current_admission()
        assert record is not None
        mark_current_request_drain_exempt()
        assert record.long_lived is False
        assert current_admission() is record
        assert old.in_flight == 1
    assert old.in_flight == 0


async def test_a_job_on_another_event_loop_records_nothing() -> None:
    _install_boot()
    old = current_epoch()
    seen: list[tuple[object, int]] = []

    async def _on_its_own_loop() -> None:
        with tai42_app.lifecycle.admitted_job():
            seen.append((current_admission(), old.in_flight))

    # A job loop of its own on a worker thread of its own, as a backend running each job off the serving loop has.
    thread = threading.Thread(target=lambda: asyncio.run(_on_its_own_loop()))
    thread.start()
    await asyncio.to_thread(thread.join, 5.0)
    assert seen == [(None, 0)]
    assert old.in_flight == 0


async def test_a_job_with_no_serving_loop_bound_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_boot()
    old = current_epoch()
    monkeypatch.setattr(instance, "_app", SimpleNamespace(serving_loop=lambda: None))

    with tai42_app.lifecycle.admitted_job():
        assert current_admission() is None
        assert old.in_flight == 0


async def test_a_job_past_the_drain_budget_is_logged_and_the_retire_proceeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _install_boot()
    old = current_epoch()
    job = _Job()

    async def _run() -> None:
        with tai42_app.lifecycle.admitted_job():
            await job.cache_resource()
            await job.may_end.wait()
            job.note_end()

    running = asyncio.create_task(_run())
    await job.cached.wait()
    with caplog.at_level(logging.ERROR, logger=epoch_mod.__name__):
        async with asyncio.timeout(2.0):
            await retire_epoch(old, advance_client_epoch(), 0.05)
    assert [r.getMessage() for r in caplog.records if r.name == epoch_mod.__name__] == [
        f"epoch {old.number} retire: 1 unit(s) of work still in flight after the drain budget"
    ]
    assert job.events == ["resource closed"]

    job.may_end.set()
    await running
    assert job.events == ["resource closed", "job ended, its resource closed"]
