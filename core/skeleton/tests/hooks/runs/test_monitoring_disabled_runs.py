"""An emitter's ``monitoring_disabled`` holds in every run of its event, not only at the accept.

The job carries the option, and each attempt enters the writer's ``disable()`` scope from it, so
the first attempt, a sweep retry, a reclaim, a replayed start, an operator retry and the loud
path all run with monitoring off. The fake writer keeps its flag in a ``ContextVar``, as a real
writer does, so a task that started from a fresh context would read it False.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import pytest
from tai42_contract.monitoring import MonitoringExportHealth

from tai42_skeleton.hooks.runs import runtime
from tai42_skeleton.hooks.runs.loud import HOOK_RUN_FAILED_EVENT_TOPIC
from tai42_skeleton.hooks.runs.sweep import sweep_tick
from tai42_skeleton.monitoring import NoOpWriter, init_monitoring, registry, reset_monitoring

from .conftest import as_process, get_run, held_run, hook, metric, operator

_DISABLED: ContextVar[bool] = ContextVar("test_monitoring_disabled", default=False)


class _Writer(NoOpWriter):
    """A recording writer that logs the disabled flag at each recording call."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str, bool]] = []
        self.health = MonitoringExportHealth()

    def is_recording(self) -> bool:
        return True

    def export_health(self) -> MonitoringExportHealth:
        return self.health

    def grow(self) -> None:
        data = self.health.model_dump()
        data["export_failures"] += 1
        data["last_error"] = "collector unreachable"
        self.health = MonitoringExportHealth(**data)
        if self._listener is not None:
            self._listener(self.health)

    @contextmanager
    def disable(self) -> Iterator[None]:
        token = _DISABLED.set(True)
        try:
            yield
        finally:
            _DISABLED.reset(token)

    def open_span(self, *, name, **kwargs):
        self.calls.append(("open_span", name, _DISABLED.get()))
        return super().open_span(name=name, **kwargs)

    @contextmanager
    def start_span(self, *, name, **kwargs):
        self.calls.append(("start_span", name, _DISABLED.get()))
        with super().start_span(name=name, **kwargs) as span:
            yield span

    def record_span(self, *, name, **kwargs):
        self.calls.append(("record_span", name, _DISABLED.get()))
        return super().record_span(name=name, **kwargs)

    def create_event(self, *, name, **kwargs):
        self.calls.append(("create_event", name, _DISABLED.get()))
        return super().create_event(name=name, **kwargs)

    def flags(self, name: str) -> list[bool]:
        return [flag for _call, span, flag in self.calls if span == name]


class _Backend:
    def __init__(self, writer: _Writer) -> None:
        self.writer = writer
        self.reader = registry.NoOpMonitoring().reader


@pytest.fixture
def writer() -> Iterator[_Writer]:
    reset_monitoring()
    fake = _Writer()
    init_monitoring(_Backend(fake))  # type: ignore[arg-type]
    yield fake
    reset_monitoring()


def _probe(world, name: str = "probe", gate: asyncio.Event | None = None) -> list[bool]:
    """Make ``name`` record the writer's disabled flag each time it runs."""
    seen: list[bool] = []

    async def _run(call: Any) -> Any:
        seen.append(_DISABLED.get())
        if gate is not None:
            await gate.wait()
        return {"ok": True}

    world.tools.behave[name] = _run
    return seen


async def _accept(manager, **kwargs) -> str:
    [run_id] = (await manager.on_event("t", {"n": 1}, **kwargs)).run_ids
    return run_id


# -- the first attempt ------------------------------------------------------------------


@pytest.mark.parametrize("store", ["on", "off"])
async def test_the_first_attempt_runs_with_monitoring_off(store, request, backend, world, hook_runs, writer) -> None:
    if store == "on":
        request.getfixturevalue("tool_runs_store")
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    gate = asyncio.Event()
    seen = _probe(world, gate=gate)

    assert _DISABLED.get() is False
    run_id = await _accept(manager, monitoring_disabled=True)
    while not seen:
        await asyncio.sleep(0)

    stored = await get_run(manager, run_id)
    assert stored is not None
    assert stored.monitoring_disabled is True
    assert stored.to_fields()["monitoring_disabled"] == "1"
    gate.set()
    await hook_runs.drain()

    assert seen == [True]
    assert writer.flags("hook_on_event") == [True]
    assert writer.flags("hook_run_attempt") == [True]
    assert _DISABLED.get() is False


# -- the later attempts -----------------------------------------------------------------


async def test_a_sweep_retry_runs_with_monitoring_off(backend, world, hook_runs, clock, writer) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    world.interactions.parked_faults = [ConnectionError("store down")]
    seen = _probe(world)

    run_id = await _accept(manager, monitoring_disabled=True)
    await hook_runs.drain()
    run = await get_run(manager, run_id)
    assert run is not None
    assert (run.status, seen) == ("pending", [])

    clock.advance(run.next_attempt_at - clock.t + 1)
    await sweep_tick()
    await hook_runs.drain()

    assert seen == [True]
    assert writer.flags("hook_run_attempt") == [True, True]


async def test_a_reclaim_runs_with_monitoring_off(backend, world, hook_runs, clock, monkeypatch, writer) -> None:
    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    seen = _probe(world)
    spawn = runtime.spawn_attempt
    as_process(monkeypatch, "dead-host:1:aaaa")
    monkeypatch.setattr(runtime, "spawn_attempt", lambda job: None)
    run_id = await _accept(manager, monitoring_disabled=True)
    as_process(monkeypatch, "live-host:2:bbbb")
    monkeypatch.setattr(runtime, "spawn_attempt", spawn)

    hook_runs.use(backend.peer(manager))
    clock.advance(manager.settings.run_lease_seconds + 1)
    await sweep_tick()
    await hook_runs.drain()

    assert seen == [True]
    assert await get_run(manager, run_id) is None


async def test_a_start_replayed_after_a_drain_runs_with_monitoring_off(
    backend, world, tool_runs_store, hook_runs, clock, writer
) -> None:
    from tai42_skeleton.operations.tool_runs.supervisor import drain_supervisors

    manager = hook_runs.use(backend.manager())
    await manager.register(hook("a"))
    world.tools.flagged = {"probe"}
    gate = asyncio.Event()
    seen = _probe(world, gate=gate)

    run_id = await _accept(manager, monitoring_disabled=True)
    while not seen:
        await asyncio.sleep(0)
    await drain_supervisors(reason="the epoch retires")
    await hook_runs.drain()
    assert (await held_run(manager, run_id)).status == "pending"

    gate.set()
    await sweep_tick()
    await hook_runs.drain()

    assert seen == [True, True]
    assert await get_run(manager, run_id) is None


async def test_an_operator_retry_runs_with_monitoring_off(
    backend, world, hook_runs, clock, monkeypatch, writer
) -> None:
    manager = hook_runs.use(backend.manager(run_max_attempts=1))
    await manager.register(hook("a"))
    world.interactions.parked_faults = [ConnectionError("store down")]
    seen = _probe(world)
    run_id = await _accept(manager, monitoring_disabled=True)
    await hook_runs.drain()
    assert (await held_run(manager, run_id)).status == "failed"

    await operator(monkeypatch, manager).retry_hook_run(run_id)
    await sweep_tick()
    await hook_runs.drain()

    assert seen == [True]
    assert await get_run(manager, run_id) is None


# -- the loud path ----------------------------------------------------------------------


def _spy_failed_accepts(monkeypatch, manager) -> list[dict[str, Any]]:
    """Record the keyword arguments of every ``hooks_run_failed`` accept."""
    accepts: list[dict[str, Any]] = []
    on_event = manager.on_event

    async def _on_event(topic, payload, **kwargs):
        if topic == HOOK_RUN_FAILED_EVENT_TOPIC:
            accepts.append(kwargs)
        return await on_event(topic, payload, **kwargs)

    monkeypatch.setattr(manager, "on_event", _on_event)
    return accepts


def _spy_notification_flag(monkeypatch) -> list[bool]:
    from tai42_skeleton.channels import notifications_sink

    flags: list[bool] = []
    record = notifications_sink.record_notification

    async def _record(message, *args, **kwargs):
        flags.append(_DISABLED.get())
        return await record(message, *args, **kwargs)

    monkeypatch.setattr(notifications_sink, "record_notification", _record)
    return flags


async def _dead_letter(manager, world, hook_runs, **kwargs) -> tuple[str, str | None, list[bool]]:
    """Accept one run that fails at once; answer its id, its ``hooks_run_failed`` job's stored option, and
    the flags the ``hooks_run_failed`` hook's tool saw."""
    world.interactions.parked_faults = [ConnectionError("store down")]
    gate = asyncio.Event()
    watched = _probe(world, "watch_probe", gate=gate)
    run_id = await _accept(manager, **kwargs)
    while not watched:
        await asyncio.sleep(0)
    [failed_job] = [
        job.id
        for job in (await manager.list_hook_runs(None, 200, None)).items
        if job.topic == HOOK_RUN_FAILED_EVENT_TOPIC
    ]
    stored = await get_run(manager, failed_job)
    assert stored is not None
    gate.set()
    await hook_runs.drain()
    return run_id, stored.to_fields().get("monitoring_disabled"), watched


async def test_a_dead_lettered_run_reports_with_monitoring_off(
    backend, world, hook_runs, clock, monkeypatch, notices, writer, caplog
) -> None:
    manager = hook_runs.use(backend.manager(run_max_attempts=1))
    await manager.register(hook("a"))
    await manager.register(hook("watch", topic=HOOK_RUN_FAILED_EVENT_TOPIC, tool="watch_probe"))
    accepts = _spy_failed_accepts(monkeypatch, manager)
    notified = _spy_notification_flag(monkeypatch)
    failed_before = metric("tai42_hook_runs_failed_total")
    caplog.set_level(logging.ERROR)

    run_id, stored_flag, watched = await _dead_letter(manager, world, hook_runs, monitoring_disabled=True)

    assert [kwargs.get("monitoring_disabled") for kwargs in accepts] == [True]
    assert stored_flag == "1"
    assert watched == [True]
    assert notified == [True]
    assert [n for n in notices if run_id in n]
    assert [r for r in caplog.records if r.levelno == logging.ERROR and run_id in r.getMessage()]
    assert metric("tai42_hook_runs_failed_total") - failed_before == 1


async def test_an_accept_without_the_option_after_one_with_it_does_not_inherit_it(
    backend, world, hook_runs, clock, writer
) -> None:
    manager = hook_runs.use(backend.manager(run_max_attempts=1))
    await manager.register(hook("a"))
    await manager.register(hook("watch", topic=HOOK_RUN_FAILED_EVENT_TOPIC, tool="watch_probe"))
    seen = _probe(world)
    await _accept(manager, monitoring_disabled=True)
    await hook_runs.drain()
    assert seen == [True]

    seen.clear()
    await _accept(manager)
    await hook_runs.drain()
    assert seen == [False]

    _run_id, stored_flag, watched = await _dead_letter(manager, world, hook_runs)
    assert stored_flag is None
    assert watched == [False]


# -- the emitter ------------------------------------------------------------------------


async def test_the_health_watch_emit_runs_its_hooks_with_monitoring_off(world, hook_runs, monkeypatch, writer) -> None:
    from tai42_skeleton.hooks import cache
    from tai42_skeleton.monitoring import health_watch as hw

    # The emit reaches the process's real hooks manager (in memory); every hook-run step reads it too.
    cache._reset_hooks_manager()
    manager = hook_runs.use(cache.get_hooks_manager())
    await manager.register(hook("watch", topic=hw.MONITORING_EXPORT_FAILED_EVENT_TOPIC))
    seen = _probe(world)
    watch = hw.MonitoringHealthWatch()
    await watch.run_pass()

    writer.grow()
    assert await watch.run_pass() is not None
    await hook_runs.drain()

    assert seen == [True]
    cache._reset_hooks_manager()
