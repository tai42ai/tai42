"""Counting and alerting what the monitoring writer could not deliver.

COUNTING. One :class:`ExportHealthRecorder` per activated recording writer turns the
writer's cumulative ``export_health()`` into ``/metrics`` counter increments (the delta from
its baseline). It is fed from two places, so a process's failures reach the counters
whatever its lifetime: the writer's health listener (every ``flush()`` / ``shutdown()``,
including a forked work-horse's flush right before it exits) and a daemon poll thread
(every ``HEALTH_POLL_SECONDS``, for a long-lived process with no event loop of its own).
A forked child starts from a zero baseline, matching the writer's own per-process counters.

ALERTING. :func:`run_monitoring_health_watch`, spawned by every process that runs the app
lifespan, compares the FAMILY totals of the counters (the merged per-pid files in a
multiprocess run family, else this process's registry) with the totals last alerted and
emits ONE ``monitoring_export_failed`` platform event per growth. In a multiprocess family
the last-alerted totals live in the multiproc dir under the dir's exclusive lock, so exactly
one process of the family raises each growth. The event is emitted with the writer disabled,
so the hook runs it starts do not feed a failing pipeline. ``/ready`` is never tied to
monitoring: a monitoring outage must not pull serving workers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any

from tai42_contract.monitoring import MonitoringExportHealth, MonitoringWriter

from tai42_skeleton.monitoring import metrics

logger = logging.getLogger(__name__)

HEALTH_POLL_SECONDS = 15.0
# The platform-event topic raised when monitoring could not deliver records. Payload: the
# growth of each family total since the last alert (``spans_dropped``, ``spans_failed``,
# ``export_failures``, ``attributes_dropped``, ``records_failed``).
MONITORING_EXPORT_FAILED_EVENT_TOPIC = "monitoring_export_failed"
HEALTH_FIELDS: tuple[str, ...] = (
    "spans_dropped",
    "spans_failed",
    "export_failures",
    "attributes_dropped",
    "records_failed",
)
# Beside the ``*.db`` files the multiprocess merge reads; removed with the dir by the family's wipe.
_ALERTED_FILE = ".monitoring_alerted.json"


class ExportHealthRecorder:
    """Turns one writer's cumulative export health into counter increments, never twice for one delta."""

    def __init__(self, writer: MonitoringWriter) -> None:
        """Count ``writer``'s health from a zero baseline."""
        self.writer = writer
        self._lock = threading.Lock()
        self._baseline = MonitoringExportHealth()
        self._pid = os.getpid()

    def reset_after_fork(self) -> None:
        """In a forked child: a fresh lock and a zero baseline (the child's writer counts from zero)."""
        if os.getpid() == self._pid:
            return
        self._lock = threading.Lock()
        self._baseline = MonitoringExportHealth()
        self._pid = os.getpid()

    def record(self, snapshot: MonitoringExportHealth) -> None:
        """Advance the counters by ``snapshot``'s growth over the baseline; the baseline becomes ``snapshot``."""
        self.reset_after_fork()
        with self._lock:
            previous = self._baseline
            deltas = {f: getattr(snapshot, f) - getattr(previous, f) for f in HEALTH_FIELDS}
            self._baseline = snapshot
        pid = os.getpid()
        if any(d < 0 for d in deltas.values()):
            logger.error(
                "monitoring health counters went backwards in process %d: baseline %s, snapshot %s",
                pid,
                previous,
                snapshot,
            )
        grown = {f: d for f, d in deltas.items() if d > 0}
        for field, delta in grown.items():
            metrics.increment(field, delta)
        if grown:
            logger.error(
                "monitoring export degraded in process %d: %s (last error: %s)", pid, grown, snapshot.last_error
            )

    def poll(self) -> None:
        """Record the writer's current export health."""
        self.record(self.writer.export_health())


_recorder: ExportHealthRecorder | None = None
_poll_thread: threading.Thread | None = None
_poll_thread_lock = threading.Lock()


def active_recorder() -> ExportHealthRecorder | None:
    """The recorder of the active recording writer, or ``None`` when the active writer records nothing."""
    return _recorder


def activate_export_health(writer: MonitoringWriter) -> None:
    """Count ``writer``'s export health from now on (a writer that records nothing gets no listener, no thread)."""
    global _recorder
    if not writer.is_recording():
        _recorder = None
        return
    recorder = ExportHealthRecorder(writer)
    writer.set_health_listener(recorder.record)
    _recorder = recorder
    _ensure_poll_thread()


def deactivate_export_health() -> None:
    """Stop counting: no writer is active."""
    global _recorder
    _recorder = None


def poll_export_health() -> None:
    """Record the active writer's export health once (the poll thread's step)."""
    recorder = _recorder
    if recorder is not None:
        recorder.poll()


def _wait_for_next_poll() -> None:
    time.sleep(HEALTH_POLL_SECONDS)


def _poll_loop() -> None:
    while True:
        _wait_for_next_poll()
        try:
            poll_export_health()
        except Exception:
            logger.exception("monitoring health poll failed")


def _ensure_poll_thread() -> None:
    global _poll_thread
    with _poll_thread_lock:
        if _poll_thread is not None and _poll_thread.is_alive():
            return
        _poll_thread = threading.Thread(target=_poll_loop, name="tai42-monitoring-health", daemon=True)
        _poll_thread.start()


def _after_fork_in_child() -> None:
    global _poll_thread, _poll_thread_lock
    _poll_thread_lock = threading.Lock()
    _poll_thread = None
    recorder = _recorder
    if recorder is not None:
        recorder.reset_after_fork()
        _ensure_poll_thread()


os.register_at_fork(after_in_child=_after_fork_in_child)


# --- alerting ------------------------------------------------------------------------


def _multiproc_dir() -> str | None:
    from tai42_skeleton.routers.prometheus import multiproc_active  # imports prometheus_client: alert time only

    if not multiproc_active():
        return None
    return os.environ["PROMETHEUS_MULTIPROC_DIR"]


def family_totals() -> dict[str, int]:
    """The counters' totals over the run family (the merged per-pid files), else over this process."""
    from prometheus_client import REGISTRY, CollectorRegistry
    from prometheus_client.multiprocess import MultiProcessCollector

    directory = _multiproc_dir()
    if directory is not None:
        registry = CollectorRegistry()
        MultiProcessCollector(registry, path=directory)
        source: Any = registry
    else:
        source = REGISTRY
    wanted = {name: field for field, name in metrics.SAMPLE_NAMES.items()}
    totals = dict.fromkeys(HEALTH_FIELDS, 0)
    for family in source.collect():
        for sample in family.samples:
            field = wanted.get(sample.name)
            if field is not None:
                totals[field] += int(sample.value)
    return totals


class _AlertedTotals:
    """The family totals last alerted: a file under the multiproc dir's lock, else process memory."""

    def __init__(self) -> None:
        self._memory = dict.fromkeys(HEALTH_FIELDS, 0)

    def advance(self, totals: dict[str, int]) -> tuple[dict[str, int], dict[str, int]] | None:
        """Mark ``totals`` alerted; return ``(previous, growth)`` when anything grew, else ``None``."""
        directory = _multiproc_dir()
        if directory is None:
            previous = self._memory
            growth = _growth(previous, totals)
            if growth is None:
                return None
            self._memory = dict(totals)
            return previous, growth
        with _family_lock(directory):
            previous = _read_alerted(directory)
            growth = _growth(previous, totals)
            if growth is None:
                return None
            _write_alerted(directory, totals)
        return previous, growth

    def restore(self, previous: dict[str, int], written: dict[str, int]) -> None:
        """Undo an advance whose alert was not emitted (only while nothing advanced it since)."""
        directory = _multiproc_dir()
        if directory is None:
            if self._memory == written:
                self._memory = dict(previous)
            return
        with _family_lock(directory):
            if _read_alerted(directory) == written:
                _write_alerted(directory, previous)


def _growth(previous: dict[str, int], totals: dict[str, int]) -> dict[str, int] | None:
    growth = {f: max(0, totals[f] - previous.get(f, 0)) for f in HEALTH_FIELDS}
    return growth if any(growth.values()) else None


class _family_lock:  # noqa: N801 - a lock context, named for the dir it guards
    def __init__(self, directory: str) -> None:
        self._path = directory

    def __enter__(self) -> None:
        from tai42_skeleton.routers.prometheus import _lock_exclusive, _multiproc_lock_path

        self._file = open(_multiproc_lock_path(self._path), "w")
        _lock_exclusive(self._file.fileno())

    def __exit__(self, *exc: object) -> None:
        from tai42_skeleton.routers.prometheus import _unlock

        try:
            _unlock(self._file.fileno())
        finally:
            self._file.close()


def _read_alerted(directory: str) -> dict[str, int]:
    path = os.path.join(directory, _ALERTED_FILE)
    if not os.path.exists(path):
        return dict.fromkeys(HEALTH_FIELDS, 0)
    with open(path) as f:
        stored = json.load(f)
    return {field: int(stored[field]) for field in HEALTH_FIELDS}


def _write_alerted(directory: str, totals: dict[str, int]) -> None:
    path = os.path.join(directory, _ALERTED_FILE)
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w") as f:
        json.dump(totals, f)
    os.replace(temporary, path)


class MonitoringHealthWatch:
    """One process's alert watch over the family totals."""

    def __init__(self) -> None:
        """Start with nothing alerted in this process's memory."""
        self._alerted = _AlertedTotals()

    async def run_pass(self) -> dict[str, int] | None:
        """Emit one ``monitoring_export_failed`` event when the family totals grew; return its payload."""
        totals = family_totals()
        advanced = self._alerted.advance(totals)
        if advanced is None:
            return None
        previous, growth = advanced
        try:
            await _emit(growth)
        except Exception:
            logger.exception("monitoring could not emit %r %s", MONITORING_EXPORT_FAILED_EVENT_TOPIC, growth)
            self._alerted.restore(previous, totals)
            return None
        return growth


async def _emit(payload: dict[str, int]) -> None:
    from tai42_skeleton.hooks.cache import get_hooks_manager
    from tai42_skeleton.monitoring.registry import get_monitoring

    with get_monitoring().writer.disable():
        await get_hooks_manager().on_event(topic=MONITORING_EXPORT_FAILED_EVENT_TOPIC, payload=dict(payload))


async def run_monitoring_health_watch() -> None:
    """Run alert passes every ``HEALTH_POLL_SECONDS`` until cancelled; a failed pass is logged and the next runs."""
    watch = MonitoringHealthWatch()
    while True:
        await asyncio.sleep(HEALTH_POLL_SECONDS)
        try:
            await watch.run_pass()
        except Exception:
            logger.exception("monitoring health watch pass failed")
