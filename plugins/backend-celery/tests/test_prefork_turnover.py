"""The Celery-shaped half of the pool turnover: the pool guards, the restart, the
bounded restart propagation with its kill-and-respawn guard, and the turnover
confirmation that RAISES on a pool that did not fully recycle.

Which ops warrant a turnover, how the budget is sized, and the manifest env the
re-forked children inherit are the shared backend base's — covered by the kit
suite, not repeated here. This module drives ``turnover_local_pool`` with a budget
handed in, exactly as the runtime does.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

import tai42_backend_celery.core.prefork as prefork

_HOST = "celery@node-1"

# A budget generous enough that only a deliberately wedged path can exhaust it —
# the base derives the real one from the bus apply window.
_BUDGET = 5.0


class _FakeInspect:
    def __init__(self, stats: Any) -> None:
        self._stats = stats

    def stats(self) -> Any:
        return self._stats


class _FakeControl:
    """Models ``inspect().stats()`` (a scripted sequence of pool snapshots) and
    ``broadcast('pool_restart')`` (an arm reply, an error to raise, or — with
    ``wedge`` set — a propagation that never returns, the billiard deadlock)."""

    def __init__(self, stats_sequence: list[Any]) -> None:
        self._stats_sequence = stats_sequence
        self._last: Any = {}
        self.broadcasts: list[tuple[str, Any]] = []
        self.connections: list[Any] = []
        self.restart_reply: Any = [{_HOST: {"ok": "pool restart"}}]
        self.wedge: threading.Event | None = None

    def inspect(
        self, destination: Any = None, timeout: Any = None, limit: Any = None, connection: Any = None
    ) -> _FakeInspect:
        # Control I/O rides a dedicated connection, never the shared pool.
        assert connection is not None
        self.connections.append(connection)
        if self._stats_sequence:
            self._last = self._stats_sequence.pop(0)
        return _FakeInspect(self._last)

    def broadcast(
        self,
        method: str,
        arguments: Any = None,
        reply: Any = None,
        destination: Any = None,
        connection: Any = None,
        **kwargs: Any,
    ) -> Any:
        assert connection is not None
        self.connections.append(connection)
        self.broadcasts.append((method, destination))
        if self.wedge is not None:
            self.wedge.wait()  # the propagation never comes back on its own
        if isinstance(self.restart_reply, Exception):
            raise self.restart_reply
        return self.restart_reply


def _pool(pids: list[int], max_conc: int = 1, impl: str = "prefork") -> dict[str, Any]:
    return {_HOST: {"pool": {"processes": pids, "max-concurrency": max_conc, "implementation": impl}}}


@pytest.fixture(autouse=True)
def _worker_up(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Present a ready worker with a known node name, and make polls instant."""
    monkeypatch.setattr(prefork, "_local_nodename", _HOST)
    prefork._worker_ready.set()
    monkeypatch.setattr(prefork.time, "sleep", lambda _s: None)
    yield
    prefork._worker_ready.clear()


def _install(monkeypatch: pytest.MonkeyPatch, stats_sequence: list[Any]) -> _FakeControl:
    control = _FakeControl(stats_sequence)
    monkeypatch.setattr(prefork.celery_app, "control", control)
    return control


def _record_kills(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Capture the signals the kill guard would send; no real process is signalled."""
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(prefork.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    return kills


@pytest.fixture
def wedged_restart() -> Iterator[threading.Event]:
    """A ``pool_restart`` propagation that never returns — the billiard deadlock.
    Released at teardown so the watchdog thread it strands can finish."""
    release = threading.Event()
    yield release
    release.set()


# --- guards -------------------------------------------------------------------


def test_non_prefork_pool_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    control = _install(monkeypatch, [_pool([111], impl="solo")])
    prefork.turnover_local_pool("reload_config", _BUDGET)
    # No restart broadcast for a pool with no forked children to recycle.
    assert control.broadcasts == []


def test_turnover_skipped_before_worker_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    # No node name captured yet: the pool has not forked, so nothing to recycle.
    monkeypatch.setattr(prefork, "_local_nodename", None)
    control = _install(monkeypatch, [_pool([111])])
    prefork.turnover_local_pool("reload_config", _BUDGET)
    assert control.broadcasts == []


def test_turnover_waits_for_worker_ready_then_recycles(monkeypatch: pytest.MonkeyPatch) -> None:
    # Setup reached but not yet consuming: the turnover waits, then recycles.
    prefork._worker_ready.clear()

    def _wait(timeout: float | None = None) -> bool:
        prefork._worker_ready.set()  # the worker starts consuming while we wait
        return True

    monkeypatch.setattr(prefork._worker_ready, "wait", _wait)
    control = _install(monkeypatch, [_pool([111]), _pool([222])])
    prefork.turnover_local_pool("reload_config", _BUDGET)
    assert control.broadcasts == [("pool_restart", [_HOST])]


def test_turnover_raises_if_worker_never_starts_consuming(monkeypatch: pytest.MonkeyPatch) -> None:
    # Worker never begins consuming: a possibly-stale child cannot be recycled,
    # so the op fails loudly.
    prefork._worker_ready.clear()
    monkeypatch.setattr(prefork._worker_ready, "wait", lambda timeout=None: False)
    control = _install(monkeypatch, [_pool([111])])
    with pytest.raises(RuntimeError, match="did not start consuming control commands"):
        prefork.turnover_local_pool("reload_config", _BUDGET)
    assert control.broadcasts == []


def test_worker_that_does_not_answer_stats_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [{}])
    with pytest.raises(RuntimeError, match="did not answer stats"):
        prefork.turnover_local_pool("reload_config", _BUDGET)


# --- restart + confirm --------------------------------------------------------


def test_full_turnover_restarts_and_confirms(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pre-state has child 111; after the restart the pool comes back as child 222.
    control = _install(monkeypatch, [_pool([111]), _pool([222])])
    prefork.turnover_local_pool("reload_config", _BUDGET)
    # The local pool was armed for restart.
    assert control.broadcasts == [("pool_restart", [_HOST])]


def test_the_whole_turnover_is_spent_inside_the_caller_s_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    # The budget is the caller's window, not a local constant: a worker that never
    # starts consuming must give up inside it rather than outlive the op's apply.
    prefork._worker_ready.clear()
    _install(monkeypatch, [_pool([111])])
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=r"within 0\.05s"):
        prefork.turnover_local_pool("reload_config", 0.05)
    assert time.monotonic() - started < 2.0


def test_old_child_still_present_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # The pre-restart child (111) never leaves — a stale child would keep serving
    # the old registry, so the turnover must fail loudly.
    control = _install(monkeypatch, [_pool([111]), _pool([111, 222])])
    monkeypatch.setattr(prefork, "_POOL_RECYCLE_POLL_INTERVAL", 0.0)
    with pytest.raises(RuntimeError, match="old children still present"):
        prefork._confirm_turnover(_HOST, ({111}, 1), timeout=0.05)
    assert control.broadcasts == []  # confirm-only helper does not restart


def test_pool_came_back_short_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Old child gone but the pool never refills to its configured size.
    _install(monkeypatch, [_pool([], max_conc=2)])
    monkeypatch.setattr(prefork, "_POOL_RECYCLE_POLL_INTERVAL", 0.0)
    with pytest.raises(RuntimeError, match="pool came back short"):
        prefork._confirm_turnover(_HOST, ({111}, 2), timeout=0.05)


def test_pool_restart_arm_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # The arm runs on the watchdog thread; its failure must surface on the caller's.
    control = _install(monkeypatch, [_pool([111])])
    control.restart_reply = [{_HOST: {"error": "no pool"}}]
    with pytest.raises(RuntimeError, match="pool restart request failed"):
        prefork.turnover_local_pool("reload_config", _BUDGET)


def test_normal_turnover_never_kills_pool_children(monkeypatch: pytest.MonkeyPatch) -> None:
    # A restart that propagates is left alone: the guard is the wedge path only.
    control = _install(monkeypatch, [_pool([111]), _pool([222])])
    kills = _record_kills(monkeypatch)
    prefork.turnover_local_pool("reload_config", _BUDGET)
    assert control.broadcasts == [("pool_restart", [_HOST])]
    assert kills == []


# --- wedged restart: the bounded kill-and-respawn guard -----------------------


def test_wedged_restart_is_bounded_then_killed_and_confirmed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, wedged_restart: threading.Event
) -> None:
    # The propagation never returns (billiard deadlock): the guard must fire inside
    # the budget, SIGKILL the pre-restart child, and confirm the re-fork (222).
    control = _install(monkeypatch, [_pool([222])])
    control.wedge = wedged_restart
    kills = _record_kills(monkeypatch)
    started = time.monotonic()
    with caplog.at_level(logging.ERROR, logger=prefork.logger.name):
        prefork._restart_pool_and_confirm(_HOST, ({111}, 1), budget=0.4)
    assert time.monotonic() - started < 0.4  # bounded well inside the caller's budget
    assert control.broadcasts == [("pool_restart", [_HOST])]
    assert kills == [(111, signal.SIGKILL)]
    # The wedge is named, not just reported as a generic timeout.
    assert "_putlock" in caplog.text
    assert "killed wedged pool child 111" in caplog.text


def test_wedged_restart_that_cannot_respawn_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, wedged_restart: threading.Event
) -> None:
    # Killed and still not turned over (111 never leaves the pool): the op must
    # fail, naming both the wedge and the failed hard path.
    control = _install(monkeypatch, [_pool([111])])
    control.wedge = wedged_restart
    kills = _record_kills(monkeypatch)
    with pytest.raises(RuntimeError) as excinfo:
        prefork._restart_pool_and_confirm(_HOST, ({111}, 1), budget=0.2)
    assert kills == [(111, signal.SIGKILL)]
    assert "wedged inside billiard (_putlock)" in str(excinfo.value)
    assert "old children still present: [111]" in str(excinfo.value)


def test_confirm_failure_after_a_healthy_restart_is_not_blamed_on_a_wedge(monkeypatch: pytest.MonkeyPatch) -> None:
    # The restart propagated; only the re-fork failed. The op still fails loudly,
    # but reports what happened rather than a wedge, and nothing is killed.
    control = _install(monkeypatch, [_pool([111])])
    kills = _record_kills(monkeypatch)
    with pytest.raises(RuntimeError) as excinfo:
        prefork._restart_pool_and_confirm(_HOST, ({111}, 1), budget=0.1)
    assert control.broadcasts == [("pool_restart", [_HOST])]
    assert kills == []
    assert "wedged" not in str(excinfo.value)
    assert "old children still present: [111]" in str(excinfo.value)


def test_restart_propagation_bound_fits_inside_the_turnover_budget() -> None:
    # The bound is carved out of the SAME budget the confirmation spends, so a
    # wedge is killed and re-confirmed before the caller's apply window closes.
    budget = 27.0
    bound = prefork._restart_propagation_timeout(budget)
    assert 0.0 < bound < budget
    # What is left over pays for the kill and the confirmation.
    assert budget - bound >= bound
    # An exhausted budget yields no wait at all, never a negative one.
    assert prefork._restart_propagation_timeout(-1.0) == 0.0


def test_kill_guard_spares_the_parent_and_tolerates_a_reaped_child(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    parent = os.getpid()
    reaped, alive = parent + 10, parent + 20
    kills: list[int] = []

    def _kill(pid: int, sig: int) -> None:
        kills.append(pid)
        if pid == reaped:
            raise ProcessLookupError(pid)

    monkeypatch.setattr(prefork.os, "kill", _kill)
    with caplog.at_level(logging.WARNING, logger=prefork.logger.name):
        prefork._kill_pool_children(_HOST, {parent, 0, reaped, alive})
    # The worker parent and a bogus pid are never signalled; an already-reaped
    # child is reported, not fatal.
    assert kills == [reaped, alive]
    assert "already gone" in caplog.text


def test_kill_guard_propagates_an_unexpected_signal_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _kill(pid: int, sig: int) -> None:
        raise PermissionError(pid)

    monkeypatch.setattr(prefork.os, "kill", _kill)
    with pytest.raises(PermissionError):
        prefork._kill_pool_children(_HOST, {os.getpid() + 30})


# --- registration -------------------------------------------------------------


def test_register_wires_only_the_celery_signals(monkeypatch: pytest.MonkeyPatch, stub_app) -> None:
    # The setup/ready signals are all this module needs; the post-apply hook that
    # decides WHEN to turn over is the shared backend base's, so registering one
    # here would double-wire it.
    connected: list[Any] = []
    monkeypatch.setattr(prefork.signals.celeryd_after_setup, "connect", lambda r: connected.append(r))
    monkeypatch.setattr(prefork.signals.worker_ready, "connect", lambda r: connected.append(r))
    stub_app.lifecycle.fleet_op_applied.clear()

    prefork.register()

    assert connected == [prefork._record_local_nodename, prefork._mark_worker_ready]
    assert stub_app.lifecycle.fleet_op_applied == []


def test_signal_receivers_capture_nodename_and_readiness() -> None:
    prefork._local_nodename = None
    prefork._worker_ready.clear()
    prefork._record_local_nodename(sender="celery@x", instance=None)
    assert prefork._local_nodename == "celery@x"
    prefork._mark_worker_ready(sender=object())
    assert prefork._worker_ready.is_set()
