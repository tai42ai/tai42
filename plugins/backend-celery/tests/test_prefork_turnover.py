"""The prefork-pool turnover successor: op filtering, the pool guard, the
refresh + restart, and the turnover confirmation that RAISES on a pool that did
not fully recycle."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from typing import Any

import pytest

import tai42_backend_celery.core.prefork as prefork
from tai42_backend_celery.core.settings import celery_settings

_HOST = "celery@node-1"


class _FakeInspect:
    def __init__(self, stats: Any) -> None:
        self._stats = stats

    def stats(self) -> Any:
        return self._stats


class _FakeControl:
    """Models ``inspect().stats()`` (a scripted sequence of pool snapshots) and
    ``broadcast('pool_restart')`` (an arm reply, or an error to raise)."""

    def __init__(self, stats_sequence: list[Any]) -> None:
        self._stats_sequence = stats_sequence
        self._last: Any = {}
        self.broadcasts: list[tuple[str, Any]] = []
        self.connections: list[Any] = []
        self.restart_reply: Any = [{_HOST: {"ok": "pool restart"}}]

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


# --- op filtering -------------------------------------------------------------


async def test_query_op_never_touches_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(prefork, "_turnover_local_pool", lambda op: called.append(op))
    await prefork._on_fleet_op_applied("list_failed_mcps")
    assert called == []


@pytest.mark.parametrize(
    "op", ["reload_config", "reload_mcp", "deregister_mcp", "reload_tool", "remove_tool", "reload_failed_mcps"]
)
async def test_every_mutating_op_recycles_the_pool(op: str, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(prefork, "_turnover_local_pool", lambda o: called.append(o))
    await prefork._on_fleet_op_applied(op)
    assert called == [op]


# --- guards -------------------------------------------------------------------


def test_non_prefork_pool_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    control = _install(monkeypatch, [_pool([111], impl="solo")])
    prefork._turnover_local_pool("reload_config")
    # No restart broadcast for a pool with no forked children to recycle.
    assert control.broadcasts == []


def test_turnover_skipped_before_worker_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    # No node name captured yet: the pool has not forked, so nothing to recycle.
    monkeypatch.setattr(prefork, "_local_nodename", None)
    control = _install(monkeypatch, [_pool([111])])
    prefork._turnover_local_pool("reload_config")
    assert control.broadcasts == []


def test_turnover_waits_for_worker_ready_then_recycles(monkeypatch: pytest.MonkeyPatch) -> None:
    # Setup reached but not yet consuming: the turnover waits, then recycles.
    prefork._worker_ready.clear()

    def _wait(timeout: float | None = None) -> bool:
        prefork._worker_ready.set()  # the worker starts consuming while we wait
        return True

    monkeypatch.setattr(prefork._worker_ready, "wait", _wait)
    control = _install(monkeypatch, [_pool([111]), _pool([222])])
    prefork._turnover_local_pool("reload_config")
    assert control.broadcasts == [("pool_restart", [_HOST])]


def test_turnover_raises_if_worker_never_starts_consuming(monkeypatch: pytest.MonkeyPatch) -> None:
    # Worker never begins consuming: a possibly-stale child cannot be recycled,
    # so the op fails loudly.
    prefork._worker_ready.clear()
    monkeypatch.setattr(prefork._worker_ready, "wait", lambda timeout=None: False)
    control = _install(monkeypatch, [_pool([111])])
    with pytest.raises(RuntimeError, match="did not start consuming control commands"):
        prefork._turnover_local_pool("reload_config")
    assert control.broadcasts == []


def test_worker_that_does_not_answer_stats_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [{}])
    with pytest.raises(RuntimeError, match="did not answer stats"):
        prefork._turnover_local_pool("reload_config")


# --- refresh + restart + confirm ---------------------------------------------


def test_full_turnover_refreshes_env_restarts_and_confirms(stub_app, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(celery_settings().manifest_key, raising=False)
    stub_app.admin.live_manifest = {"backend_module": "tai42_backend_celery", "gen": 2}
    # Pre-state has child 111; after the restart the pool comes back as child 222.
    control = _install(monkeypatch, [_pool([111]), _pool([222])])
    prefork._turnover_local_pool("reload_config")
    # The manifest env the re-forked children inherit was refreshed from the live manifest.
    assert os.environ[celery_settings().manifest_key] == '{"backend_module":"tai42_backend_celery","gen":2}'
    # The local pool was armed for restart.
    assert control.broadcasts == [("pool_restart", [_HOST])]


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
    control = _install(monkeypatch, [_pool([111])])
    control.restart_reply = [{_HOST: {"error": "no pool"}}]
    with pytest.raises(RuntimeError, match="pool restart request failed"):
        prefork._turnover_local_pool("reload_config")


# --- turnover budget ----------------------------------------------------------


def test_turnover_budget_sits_under_the_bus_apply_window(monkeypatch: pytest.MonkeyPatch) -> None:
    # The budget tracks the SAME knob the bus reads and stays under it, so this
    # worker's confirm-or-raise lands before the publisher's report cut.
    monkeypatch.setenv("TAI_BUS_APPLY_TIMEOUT", "30")
    budget = prefork._turnover_budget()
    assert budget < 30.0
    assert budget == pytest.approx(30.0 - prefork._TURNOVER_BUDGET_MARGIN)
    # Raising the operator's bus apply timeout raises the budget under it.
    monkeypatch.setenv("TAI_BUS_APPLY_TIMEOUT", "60")
    raised = prefork._turnover_budget()
    assert raised > budget
    assert raised < 60.0


def test_turnover_budget_defaults_to_the_bus_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAI_BUS_APPLY_TIMEOUT", raising=False)
    assert prefork._turnover_budget() == pytest.approx(
        prefork._BUS_APPLY_TIMEOUT_DEFAULT - prefork._TURNOVER_BUDGET_MARGIN
    )


def test_turnover_budget_is_floored_for_a_small_apply_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_BUS_APPLY_TIMEOUT", "1")
    assert prefork._turnover_budget() == prefork._TURNOVER_BUDGET_FLOOR


def test_turnover_budget_raises_on_a_malformed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAI_BUS_APPLY_TIMEOUT", "soon")
    with pytest.raises(ValueError, match="could not convert string to float"):
        prefork._turnover_budget()


# --- registration -------------------------------------------------------------


def test_register_wires_signals_and_the_lifecycle_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    connected: list[Any] = []
    monkeypatch.setattr(prefork.signals.celeryd_after_setup, "connect", lambda r: connected.append(r))
    monkeypatch.setattr(prefork.signals.worker_ready, "connect", lambda r: connected.append(r))
    registered: list[Any] = []
    monkeypatch.setattr(prefork.tai42_app.lifecycle, "on_fleet_op_applied", lambda f: registered.append(f))
    prefork.register()
    assert prefork._record_local_nodename in connected
    assert prefork._mark_worker_ready in connected
    assert registered == [prefork._on_fleet_op_applied]


def test_signal_receivers_capture_nodename_and_readiness() -> None:
    prefork._local_nodename = None
    prefork._worker_ready.clear()
    prefork._record_local_nodename(sender="celery@x", instance=None)
    assert prefork._local_nodename == "celery@x"
    prefork._mark_worker_ready(sender=object())
    assert prefork._worker_ready.is_set()


def test_handler_offloads_blocking_turnover_off_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The turnover control I/O is blocking, so it must run off the serving loop."""
    import threading

    seen: dict[str, Any] = {}

    def _record(op: str) -> None:
        seen["thread"] = threading.current_thread()

    monkeypatch.setattr(prefork, "_turnover_local_pool", _record)
    asyncio.run(prefork._on_fleet_op_applied("reload_config"))
    # Ran on a worker thread, not the loop's main thread.
    assert seen["thread"] is not threading.main_thread()
