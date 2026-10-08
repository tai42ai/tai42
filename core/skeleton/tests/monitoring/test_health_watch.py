"""Counting and alerting what the monitoring writer could not deliver (a neutral fake writer, no backend)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from tai42_contract.monitoring import MonitoringExportHealth

from tai42_skeleton.monitoring import NoOpWriter, init_monitoring, metrics, registry, reset_monitoring
from tai42_skeleton.monitoring import health_watch as hw


class FakeWriter(NoOpWriter):
    """A recording writer whose export health the test grows; ``flush`` hands it to the listener."""

    def __init__(self) -> None:
        super().__init__()
        self.health = MonitoringExportHealth()
        self.disabled = False

    def is_recording(self) -> bool:
        return True

    def export_health(self) -> MonitoringExportHealth:
        return self.health

    def grow(self, **fields: int) -> None:
        data = self.health.model_dump()
        for name, amount in fields.items():
            data[name] += amount
        data["last_error"] = "collector unreachable"
        self.health = MonitoringExportHealth(**data)

    def flush(self) -> None:
        if self._listener is not None:
            self._listener(self.health)

    @contextmanager
    def disable(self) -> Iterator[None]:
        self.disabled = True
        try:
            yield
        finally:
            self.disabled = False


class _Backend:
    def __init__(self, writer: Any) -> None:
        self.writer = writer
        self.reader = registry.NoOpMonitoring().reader


class _Hooks:
    def __init__(self, writer: FakeWriter, *, fail: bool = False) -> None:
        self.events: list[tuple[str, dict[str, Any], bool]] = []
        self.writer = writer
        self.fail = fail

    async def on_event(self, *, topic: str, payload: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("hooks down")
        self.events.append((topic, payload, self.writer.disabled))


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    reset_monitoring()
    yield
    reset_monitoring()


def _total(field: str) -> int:
    return hw.family_totals()[field]


def _install(writer: Any) -> None:
    init_monitoring(_Backend(writer))  # type: ignore[arg-type]


def test_flush_and_poll_advance_the_counters_by_the_delta_never_twice():
    writer = FakeWriter()
    _install(writer)
    before = hw.family_totals()
    writer.grow(export_failures=2, spans_failed=10)
    writer.flush()
    hw.poll_export_health()
    assert _total("export_failures") - before["export_failures"] == 2
    assert _total("spans_failed") - before["spans_failed"] == 10
    writer.grow(spans_dropped=3)
    hw.poll_export_health()
    writer.flush()
    assert _total("spans_dropped") - before["spans_dropped"] == 3
    assert _total("export_failures") - before["export_failures"] == 2


def test_the_degradation_is_logged(caplog: pytest.LogCaptureFixture):
    writer = FakeWriter()
    _install(writer)
    writer.grow(records_failed=1)
    with caplog.at_level(logging.ERROR):
        writer.flush()
    assert "monitoring export degraded in process" in caplog.text
    assert "collector unreachable" in caplog.text


def test_a_registry_swap_gives_the_new_writer_a_fresh_recorder():
    first = FakeWriter()
    _install(first)
    first.grow(export_failures=4)
    second = FakeWriter()
    second.grow(export_failures=1)
    before = _total("export_failures")
    _install(second)
    recorder = hw.active_recorder()
    assert recorder is not None
    assert recorder.writer is second
    second.flush()
    assert _total("export_failures") - before == 1
    assert first._listener is not None
    assert second._listener == recorder.record


def test_the_previous_writer_is_shut_down_and_counted_before_the_swap():
    first = FakeWriter()
    shutdowns: list[int] = []
    first.shutdown = lambda: (shutdowns.append(1), first.flush())  # type: ignore[method-assign]
    _install(first)
    first.grow(spans_failed=5)
    before = _total("spans_failed")
    _install(FakeWriter())
    assert shutdowns == [1]
    assert _total("spans_failed") - before == 5


def test_a_writer_that_records_nothing_gets_no_listener_and_no_recorder():
    writer = NoOpWriter()
    _install(writer)
    assert hw.active_recorder() is None
    assert writer._listener is None


def test_a_snapshot_below_the_baseline_advances_nothing_and_is_logged(caplog: pytest.LogCaptureFixture):
    writer = FakeWriter()
    _install(writer)
    writer.grow(export_failures=5)
    writer.flush()
    before = _total("export_failures")
    writer.health = MonitoringExportHealth(export_failures=2)
    with caplog.at_level(logging.ERROR):
        writer.flush()
    assert _total("export_failures") == before
    assert "monitoring health counters went backwards" in caplog.text
    writer.grow(export_failures=1)
    writer.flush()
    assert _total("export_failures") - before == 1


def test_record_first_called_in_a_forked_child_starts_from_zero():
    recorder = hw.ExportHealthRecorder(FakeWriter())
    recorder.record(MonitoringExportHealth(export_failures=7))
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child exits through os._exit
        try:
            os.close(read_fd)
            before = _total("export_failures")
            recorder.record(MonitoringExportHealth(export_failures=1))
            os.write(write_fd, str(_total("export_failures") - before).encode())
        finally:
            os._exit(0)
    os.close(write_fd)
    with os.fdopen(read_fd) as pipe:
        delta = pipe.read()
    os.waitpid(pid, 0)
    assert delta == "1"


async def test_in_process_watch_emits_one_event_per_growth_under_disable(monkeypatch: pytest.MonkeyPatch):
    writer = FakeWriter()
    _install(writer)
    hooks = _Hooks(writer)
    monkeypatch.setattr("tai42_skeleton.hooks.cache.get_hooks_manager", lambda: hooks)
    watch = hw.MonitoringHealthWatch()
    await watch.run_pass()
    hooks.events.clear()
    assert await watch.run_pass() is None
    writer.grow(export_failures=1, spans_failed=4)
    writer.flush()
    payload = await watch.run_pass()
    assert payload == {
        "spans_dropped": 0,
        "spans_failed": 4,
        "export_failures": 1,
        "attributes_dropped": 0,
        "records_failed": 0,
    }
    assert hooks.events == [(hw.MONITORING_EXPORT_FAILED_EVENT_TOPIC, payload, True)]
    assert await watch.run_pass() is None
    assert len(hooks.events) == 1


async def test_an_emit_failure_leaves_the_growth_for_the_next_pass(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    writer = FakeWriter()
    _install(writer)
    hooks = _Hooks(writer, fail=True)
    monkeypatch.setattr("tai42_skeleton.hooks.cache.get_hooks_manager", lambda: hooks)
    watch = hw.MonitoringHealthWatch()
    hooks.fail = False
    await watch.run_pass()
    hooks.events.clear()
    writer.grow(records_failed=2)
    writer.flush()
    hooks.fail = True
    with caplog.at_level(logging.ERROR):
        assert await watch.run_pass() is None
    assert "could not emit" in caplog.text
    hooks.fail = False
    payload = await watch.run_pass()
    assert payload is not None
    assert payload["records_failed"] == 2


_FORK_SCRIPT = textwrap.dedent(
    """
    import asyncio, json, os, sys
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = sys.argv[1]
    sys.path.insert(0, sys.argv[2])
    from test_health_watch import FakeWriter, _Backend, _Hooks
    from tai42_skeleton.monitoring import init_monitoring
    from tai42_skeleton.monitoring import health_watch as hw
    from tai42_skeleton.routers.prometheus import assert_multiproc_value_class
    import tai42_skeleton.hooks.cache as cache

    assert_multiproc_value_class()
    writer = FakeWriter()
    init_monitoring(_Backend(writer))
    pid = os.fork()
    if pid == 0:
        writer.grow(export_failures=3, spans_failed=30)
        writer.flush()
        os._exit(0)
    os.waitpid(pid, 0)
    hooks = _Hooks(writer)
    cache.get_hooks_manager = lambda: hooks
    first = asyncio.run(hw.MonitoringHealthWatch().run_pass())
    second = asyncio.run(hw.MonitoringHealthWatch().run_pass())
    print(json.dumps({"totals": hw.family_totals(), "first": first, "second": second, "events": hooks.events}))
    """
)


def test_a_forked_child_counts_reach_the_family_and_alert_once(tmp_path: Path):
    multiproc = tmp_path / "multiproc"
    multiproc.mkdir()
    script = tmp_path / "fork_probe.py"
    script.write_text(_FORK_SCRIPT)
    out = subprocess.run(
        [sys.executable, str(script), str(multiproc), str(Path(__file__).parent)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "PYTHONWARNINGS": "ignore::DeprecationWarning"},
    )
    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout.strip().splitlines()[-1])
    assert report["totals"]["export_failures"] == 3
    assert report["totals"]["spans_failed"] == 30
    assert report["first"]["export_failures"] == 3
    assert report["second"] is None
    assert len(report["events"]) == 1
    topic, payload, disabled = report["events"][0]
    assert topic == hw.MONITORING_EXPORT_FAILED_EVENT_TOPIC
    assert payload["export_failures"] == 3
    assert disabled is True
    assert (multiproc / ".monitoring_alerted.json").exists()


def test_sample_names_match_the_declared_counters():
    from prometheus_client import REGISTRY

    for field in hw.HEALTH_FIELDS:
        metrics.increment(field, 0)
    names = {sample.name for family in REGISTRY.collect() for sample in family.samples}
    assert set(metrics.SAMPLE_NAMES.values()) <= names


async def test_the_watch_loop_logs_a_failed_pass_and_keeps_running(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    import asyncio

    passes: list[int] = []

    async def failing_pass(self: hw.MonitoringHealthWatch) -> None:
        passes.append(1)
        if len(passes) >= 3:
            raise asyncio.CancelledError
        raise RuntimeError("multiproc dir unreadable")

    monkeypatch.setattr(hw, "HEALTH_POLL_SECONDS", 0)
    monkeypatch.setattr(hw.MonitoringHealthWatch, "run_pass", failing_pass)
    with caplog.at_level(logging.ERROR), pytest.raises(asyncio.CancelledError):
        await hw.run_monitoring_health_watch()
    assert len(passes) == 3
    assert caplog.text.count("monitoring health watch pass failed") == 2


def test_the_lifespan_spawns_and_cancels_the_watch(monkeypatch: pytest.MonkeyPatch):
    import asyncio

    from tai42_skeleton.app.lifecycle import TaiMCPLifecycleMixin

    class _M(TaiMCPLifecycleMixin):
        def _mcp_tools(self, config, tools):  # pragma: no cover - unused here
            self._mcp_bound_tools[config.title] = set()

    started: list[int] = []

    async def forever() -> None:
        started.append(1)
        await asyncio.Event().wait()

    monkeypatch.setattr(hw, "run_monitoring_health_watch", forever)

    async def run() -> None:
        m = _M()
        m._spawn_monitoring_health_watch()
        assert m._monitoring_health_watch_task is not None
        assert m._monitoring_health_watch_task.get_name() == "tai-monitoring-health-watch"
        await asyncio.sleep(0)
        await m._cancel_monitoring_health_watch()
        assert m._monitoring_health_watch_task is None
        assert m._dead_perpetual_task is None

    asyncio.run(run())
    assert started == [1]


def test_the_poll_thread_records_the_active_writer(monkeypatch: pytest.MonkeyPatch):
    writer = FakeWriter()
    _install(writer)
    before = _total("records_failed")
    writer.grow(records_failed=2)
    calls: list[int] = []

    def wait_once() -> None:
        calls.append(1)
        if len(calls) > 1:
            raise SystemExit

    monkeypatch.setattr(hw, "_wait_for_next_poll", wait_once)
    with pytest.raises(SystemExit):
        hw._poll_loop()
    assert _total("records_failed") - before == 2


def test_a_reset_recorder_starts_from_a_zero_baseline_and_after_fork_restarts_the_poll_thread():
    writer = FakeWriter()
    _install(writer)
    recorder = hw.active_recorder()
    assert recorder is not None
    recorder.record(MonitoringExportHealth(export_failures=9))
    recorder._pid = -1  # as seen from a forked child
    hw._after_fork_in_child()
    assert recorder._baseline == MonitoringExportHealth()
    assert recorder._pid == os.getpid()
    assert hw._poll_thread is not None
    assert hw._poll_thread.is_alive()


def test_a_failed_poll_is_logged_and_the_thread_keeps_polling(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    writer = FakeWriter()
    _install(writer)
    monkeypatch.setattr(writer, "export_health", lambda: (_ for _ in ()).throw(RuntimeError("health read broke")))
    calls: list[int] = []

    def wait_twice() -> None:
        calls.append(1)
        if len(calls) > 2:
            raise SystemExit

    monkeypatch.setattr(hw, "_wait_for_next_poll", wait_twice)
    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit):
        hw._poll_loop()
    assert caplog.text.count("monitoring health poll failed") == 2


async def test_in_process_totals_alert_once_and_an_unemitted_growth_is_restored(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(hw, "_multiproc_dir", lambda: None)
    totals = dict.fromkeys(hw.HEALTH_FIELDS, 0)
    monkeypatch.setattr(hw, "family_totals", lambda: dict(totals))
    writer = FakeWriter()
    _install(writer)
    hooks = _Hooks(writer)
    monkeypatch.setattr("tai42_skeleton.hooks.cache.get_hooks_manager", lambda: hooks)
    watch = hw.MonitoringHealthWatch()
    assert await watch.run_pass() is None
    totals["spans_failed"] = 4
    hooks.fail = True
    assert await watch.run_pass() is None
    hooks.fail = False
    assert (await watch.run_pass() or {}).get("spans_failed") == 4
    assert await watch.run_pass() is None


def test_in_process_family_totals_read_this_process_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(hw, "_multiproc_dir", lambda: None)
    before = hw.family_totals()["attributes_dropped"]
    metrics.increment("attributes_dropped", 3)
    assert hw.family_totals()["attributes_dropped"] - before == 3


def test_cancelling_a_watch_that_died_completes_the_shutdown(monkeypatch: pytest.MonkeyPatch):
    import asyncio

    from tai42_skeleton.app.lifecycle import TaiMCPLifecycleMixin, lifespan

    class _M(TaiMCPLifecycleMixin):
        def _mcp_tools(self, config, tools):  # pragma: no cover - unused here
            self._mcp_bound_tools[config.title] = set()

    async def dies() -> None:
        raise RuntimeError("watch broke")

    monkeypatch.setattr(hw, "run_monitoring_health_watch", dies)
    monkeypatch.setattr(lifespan, "graceful_exit_for", lambda kind: lambda: None)

    async def run() -> tuple[str, str] | None:
        m = _M()
        m._spawn_monitoring_health_watch()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await m._cancel_monitoring_health_watch()
        return m._dead_perpetual_task

    assert asyncio.run(run()) == ("tai-monitoring-health-watch", "RuntimeError")
