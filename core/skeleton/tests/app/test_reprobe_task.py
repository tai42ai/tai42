"""The lifespan-owned failed-MCP backoff re-probe task.

Two layers:

* A network-free ``_Mixin`` drives ``_reprobe_failed_mcps_loop`` with a
  controllable clock (an instance ``_reprobe_sleep`` seam) and a scripted
  ``_probe_and_apply_failed``, proving: a recovering title gets its tools bound
  and leaves the failed set (real reload path); the interval doubles per
  all-failed pass and caps at the max, resetting on recovery; an empty failed set
  probes nothing; the failed-set snapshot is taken under the reload gate; the
  probe runs OFF the gate so a reload-gated write is not blocked mid-probe; a
  per-pass error is logged at ERROR and the loop survives; and the shutdown cancel
  swallows a non-``CancelledError`` failure (already surfaced by the done-callback)
  so teardown is never aborted.
* The real process ``app`` proves ``app_context`` spawns the task and cancels it
  cleanly at shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from tai42_contract.manifest import MCPConfig, TaiMCPConfig

from tai42_skeleton.app.instance import app
from tai42_skeleton.app.lifecycle import TaiMCPLifecycleMixin
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.settings.cache import mcp_reload_probe_timeout


class _FakeMcpTool:
    name = "ping"
    description = "ping"
    inputSchema = {"type": "object", "properties": {}}  # noqa: RUF012
    outputSchema = {}  # noqa: RUF012


class _NoManifestConfig:
    """No external manifest file, so ``_refresh_manifest_mcp`` keeps the in-memory
    MCP rows instead of re-reading."""

    def read_manifest(self):
        raise FileNotFoundError("no external manifest")


def _stub_core() -> Any:
    """A minimal serving-core stand-in: the per-epoch generation state the mixin's
    forwarding properties read/write, without a real ``ServingCore``/FastMCP."""
    from unittest.mock import MagicMock

    return SimpleNamespace(
        _manifest=None,
        _tool_registry=MagicMock(),
        _extension_registry=MagicMock(),
        _failed_mcps={},
        _mcp_bound_tools={},
        _mcp_preset_conflicts={},
        _resource_manager_cache=None,
        _fast_mcp=SimpleNamespace(local_provider=SimpleNamespace(remove_tool=lambda name: None)),
    )


class _Mixin(TaiMCPLifecycleMixin):
    """Concrete-enough mixin: ``_mcp_tools`` records bound tool names, no server."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The forwarding properties resolve through ``_building`` when set — install a
        # stub core so ``_manifest``/``_failed_mcps``/``_mcp_bound_tools`` are read/written
        # here without a real epoch.
        self._building = _stub_core()
        self._config_manager = _NoManifestConfig()  # pyright: ignore[reportAttributeAccessIssue]
        # No presets are bound in these network-free tests, so the post-reload
        # reconciliation is a no-op stub (a bare mixin has no preset manager).
        self.preset_manager = cast("Any", SimpleNamespace(reconcile_bases=AsyncMock()))

    def _mcp_tools(self, config, tools):
        self._mcp_bound_tools[config.title] = {f"{config.title}_t"}


class _Stop(BaseException):
    """Breaks the re-probe loop out of its ``while True`` from the test's fake
    sleep. A ``BaseException`` so the loop's ``except Exception`` guard does not
    swallow it (that guard is exactly what keeps the loop alive across real
    per-pass errors)."""


def _cfg(title="svc") -> TaiMCPConfig:
    return TaiMCPConfig(title=title, include=[], config=MCPConfig(type="http", url="http://x/mcp"))


def _settings(initial: float, maximum: float):
    return lambda: SimpleNamespace(mcp_reprobe_initial_seconds=initial, mcp_reprobe_max_seconds=maximum)


# -- a recovering title binds tools + leaves the failed set (real reload path) --


async def test_recovering_title_binds_tools_and_leaves_failed_set(monkeypatch):
    m = _Mixin()
    m._manifest = Manifest.model_validate({"mcp": [_cfg("svc").model_dump()]})
    m._failed_mcps = {"svc": "unavailable"}
    m._probe_mcp = AsyncMock(return_value=[_FakeMcpTool()])
    monkeypatch.setattr("tai42_skeleton.app.lifecycle.CoreSettings", _settings(1.0, 8.0))

    sleeps = {"n": 0}

    async def fake_sleep(_seconds):
        sleeps["n"] += 1
        if sleeps["n"] >= 2:  # let exactly one pass run, then stop
            raise _Stop
        return

    m._reprobe_sleep = fake_sleep  # type: ignore[method-assign]

    with pytest.raises(_Stop):
        await m._reprobe_failed_mcps_loop()

    # The real probe/apply path ran: svc probed viable, bound, cleared.
    assert "svc" not in m._failed_mcps
    assert m._mcp_bound_tools.get("svc") == {"svc_t"}


# -- backoff: doubles per all-failed pass, caps at max, resets on recovery ------


async def test_backoff_doubles_caps_and_resets_on_recovery(monkeypatch):
    m = _Mixin()
    m._manifest = Manifest.model_validate({})
    m._failed_mcps = {"a": "unavailable"}
    monkeypatch.setattr("tai42_skeleton.app.lifecycle.CoreSettings", _settings(1.0, 8.0))

    pass_index = {"n": 0}

    async def fake_probe_apply(_snapshot):
        i = pass_index["n"]
        pass_index["n"] += 1
        if i == 5:  # recovers on the sixth probe pass
            m._failed_mcps.pop("a", None)
            return [{"title": "a", "status": "ok"}], set(m._failed_mcps)
        return [{"title": "a", "status": "unavailable"}], set(m._failed_mcps)

    m._probe_and_apply_failed = fake_probe_apply  # type: ignore[method-assign]

    intervals: list[float] = []

    async def fake_sleep(seconds):
        intervals.append(seconds)
        if len(intervals) >= 8:
            raise _Stop
        return

    m._reprobe_sleep = fake_sleep  # type: ignore[method-assign]

    with pytest.raises(_Stop):
        await m._reprobe_failed_mcps_loop()

    # 1 → 2 → 4 → 8 (double each all-failed pass), hold at the 8s cap, then reset
    # to the initial 1s the moment "a" recovers (and stay there while idle).
    assert intervals == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0, 1.0, 1.0]


# -- a new failure resets the backoff -----------------------------------------


async def test_new_failed_title_resets_backoff(monkeypatch):
    m = _Mixin()
    m._manifest = Manifest.model_validate({})
    m._failed_mcps = {"a": "unavailable"}
    monkeypatch.setattr("tai42_skeleton.app.lifecycle.CoreSettings", _settings(1.0, 8.0))

    async def fake_probe_apply(_snapshot):
        return [{"title": t, "status": "unavailable"} for t in m._failed_mcps], set(m._failed_mcps)

    m._probe_and_apply_failed = fake_probe_apply  # type: ignore[method-assign]

    intervals: list[float] = []

    async def fake_sleep(seconds):
        intervals.append(seconds)
        # "b" fails between passes (e.g. a serving-loop reload_mcp recorded it)
        # right before the fourth pass snapshots the failed set.
        if len(intervals) == 4:
            m._failed_mcps["b"] = "unavailable"
        if len(intervals) >= 5:
            raise _Stop
        return

    m._reprobe_sleep = fake_sleep  # type: ignore[method-assign]

    with pytest.raises(_Stop):
        await m._reprobe_failed_mcps_loop()

    # 1 → 2 → 4 → 8 (all-failed doubling); the fourth pass sees "b" freshly failed
    # and resets to the initial 1s rather than continuing to double.
    assert intervals == [1.0, 2.0, 4.0, 8.0, 1.0]


# -- the failed-set snapshot is taken under the reload gate lock ---------------


async def test_reprobe_snapshots_failed_set_under_the_gate_lock(monkeypatch, caplog):
    """The pass snapshots ``_failed_mcps`` only while holding the reload gate, so a
    concurrent worker-thread reload (which holds the same lock) can never mutate the
    dict mid-snapshot — the dictionary-changed-size race the lock removes.

    Asserts on the value the pass ACTUALLY snapshotted (the ``probed=`` set it logs,
    taken from ``current_failed`` under the lock), so moving the snapshot OUT of the
    lock would capture only the pre-mutation set and fail this test."""
    m = _Mixin()
    m._manifest = Manifest.model_validate({})
    m._failed_mcps = {"a": "unavailable"}
    monkeypatch.setattr("tai42_skeleton.app.lifecycle.CoreSettings", _settings(1.0, 8.0))

    reload_ran = asyncio.Event()

    async def fake_probe_apply(_snapshot):
        reload_ran.set()
        return [{"title": t, "status": "unavailable"} for t in sorted(m._failed_mcps)], set(m._failed_mcps)

    m._probe_and_apply_failed = fake_probe_apply  # type: ignore[method-assign]

    slept = {"done": False}

    async def fake_sleep(_seconds):
        if not slept["done"]:
            slept["done"] = True
            return  # let the first pass run
        await asyncio.Event().wait()  # park until cancelled

    m._reprobe_sleep = fake_sleep  # type: ignore[method-assign]

    # Hold the gate exactly as a concurrent reload would while it mutates the set.
    async with reload_gate.lock:
        task = asyncio.create_task(m._reprobe_failed_mcps_loop())
        await asyncio.sleep(0)  # let the pass reach the gate and block on it
        assert not reload_ran.is_set()  # blocked before snapshotting — no read yet
        m._failed_mcps["b"] = "unavailable"  # a reload mutates under the same lock

    # Lock released: the pass now acquires it, snapshots the settled set, and logs
    # what it snapshotted.
    try:
        with caplog.at_level(logging.INFO):
            await asyncio.wait_for(reload_ran.wait(), timeout=5)
            for _ in range(1000):  # let the pass reach its post-snapshot log line
                if "re-probe pass" in caplog.text:
                    break
                await asyncio.sleep(0)
        # The pass snapshotted AFTER acquiring the gate, so its probed set holds BOTH
        # the boot failure and the one the concurrent reload added under the lock.
        assert "probed=['a', 'b']" in caplog.text
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# -- an empty failed set probes nothing ---------------------------------------


async def test_empty_failed_set_probes_nothing(monkeypatch):
    m = _Mixin()
    m._manifest = Manifest.model_validate({})
    m._failed_mcps = {}
    monkeypatch.setattr("tai42_skeleton.app.lifecycle.CoreSettings", _settings(1.0, 8.0))

    probe_apply = AsyncMock()
    m._probe_and_apply_failed = probe_apply  # type: ignore[method-assign]

    intervals: list[float] = []

    async def fake_sleep(seconds):
        intervals.append(seconds)
        if len(intervals) >= 3:
            raise _Stop
        return

    m._reprobe_sleep = fake_sleep  # type: ignore[method-assign]

    with pytest.raises(_Stop):
        await m._reprobe_failed_mcps_loop()

    probe_apply.assert_not_awaited()
    assert intervals == [1.0, 1.0, 1.0]  # stays idle at the initial cadence


# -- the probe runs OFF the gate, so a reload-gated write is not blocked ---------


async def test_in_flight_probe_does_not_block_gated_writes(monkeypatch):
    """A persistently-down MCP's re-probe must NOT hold the reload gate across the
    network probe: the gate wraps only the brief snapshot/apply, so a reload-gated
    write stays fast while a pass is mid-probe.

    Fails against the old under-gate code, which held the gate for the whole probe
    (up to the 15s cold-boot budget) → the gated write below would block on the gate
    until the probe finished, so ``wait_for`` would fire."""
    m = _Mixin()
    m._manifest = Manifest.model_validate({"mcp": [_cfg("svc").model_dump()]})
    m._failed_mcps = {"svc": "unavailable"}
    monkeypatch.setattr("tai42_skeleton.app.lifecycle.CoreSettings", _settings(1.0, 8.0))

    probing = asyncio.Event()
    release = asyncio.Event()
    seen: dict[str, Any] = {}

    async def parked_probe(_config, timeout=None):
        seen["timeout"] = timeout
        probing.set()
        await release.wait()  # stay mid-probe until the test lets go
        raise TimeoutError("never recovers")

    m._probe_mcp = parked_probe  # type: ignore[method-assign]

    sleeps = {"n": 0}

    async def fake_sleep(_seconds):
        sleeps["n"] += 1
        if sleeps["n"] == 1:
            return  # let the first pass run
        await asyncio.Event().wait()  # park until cancelled

    m._reprobe_sleep = fake_sleep  # type: ignore[method-assign]

    assert not reload_gate.locked
    task = asyncio.create_task(m._reprobe_failed_mcps_loop())
    try:
        await asyncio.wait_for(probing.wait(), timeout=5)  # the pass is mid-probe

        # A reload-gated write takes reload_gate.lock; with the probe off the gate it
        # acquires promptly. Under the old under-gate probe the lock is held for the
        # whole (here, parked) probe, so this acquire would block and time out.
        async def gated_write() -> str:
            async with reload_gate.lock:
                return "ok"

        assert await asyncio.wait_for(gated_write(), timeout=1) == "ok"
        assert not reload_gate.locked  # nothing holds the gate during the probe
        # Parity with _load_mcps: the reprobe path probes on the short reload budget,
        # never the generous cold-boot timeout the under-gate bug used.
        assert seen["timeout"] == mcp_reload_probe_timeout()
    finally:
        release.set()
        await asyncio.sleep(0)  # let the pass finish and release the gate
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert not reload_gate.locked


# -- a per-pass error is logged at ERROR and the loop survives ------------------


async def test_pass_error_is_logged_and_loop_survives(monkeypatch, caplog):
    m = _Mixin()
    m._manifest = Manifest.model_validate({})
    m._failed_mcps = {"a": "unavailable"}
    monkeypatch.setattr("tai42_skeleton.app.lifecycle.CoreSettings", _settings(1.0, 8.0))

    calls = {"n": 0}

    async def fake_probe_apply(_snapshot):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("probe subsystem exploded")
        return [{"title": "a", "status": "unavailable"}], set(m._failed_mcps)

    m._probe_and_apply_failed = fake_probe_apply  # type: ignore[method-assign]

    sleeps = {"n": 0}

    async def fake_sleep(_seconds):
        sleeps["n"] += 1
        if sleeps["n"] >= 3:  # two full passes, then stop
            raise _Stop
        return

    m._reprobe_sleep = fake_sleep  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR), pytest.raises(_Stop):
        await m._reprobe_failed_mcps_loop()

    assert "re-probe pass failed" in caplog.text
    assert calls["n"] == 2, "the loop survived the first pass's error and ran the second"


# -- the shutdown cancel swallows a non-CancelledError failure -----------------


async def test_cancel_reprobe_task_swallows_non_cancel_error(caplog):
    """A re-probe task that died with a non-cancel exception is awaited-and-
    swallowed at shutdown, not re-raised: the failure is already surfaced at ERROR
    by the perpetual-task done-callback, and re-raising here (inside the app_context
    shutdown ``finally``) would skip the remaining teardown steps."""
    m = _Mixin()

    async def boom():
        raise RuntimeError("task died")

    with caplog.at_level(logging.ERROR):
        m._reprobe_task = asyncio.create_task(boom(), name="tai-failed-mcp-reprobe")
        m._reprobe_task.add_done_callback(m._on_perpetual_task_done)
        await asyncio.sleep(0)  # let the task fail
        await asyncio.sleep(0)  # let its done-callback run and log
        # No exception escapes — teardown would continue past this in app_context.
        await m._cancel_reprobe_task()
    assert m._reprobe_task is None
    # The loud surface is preserved: the done-callback logged the real exception.
    assert "terminated with an exception" in caplog.text


# -- the re-probe task's done-callback surfaces a runtime death at ERROR --------


async def test_reprobe_task_death_is_logged_at_error(caplog):
    """The perpetual-task done-callback the re-probe task carries logs a non-cancel
    runtime death at ERROR (mirroring the control-plane subscription), so a silently
    dead recovery loop is loud. Defensive: the loop itself already catches per-pass
    errors, so only an unexpected escape reaches here."""
    m = _Mixin()

    async def boom():
        raise RuntimeError("reprobe loop escaped")

    task = asyncio.create_task(boom(), name="tai-failed-mcp-reprobe")
    task.add_done_callback(m._on_perpetual_task_done)
    with caplog.at_level(logging.ERROR):
        with contextlib.suppress(RuntimeError):
            await task
        await asyncio.sleep(0)  # let the done-callback run

    assert "terminated with an exception" in caplog.text
    assert "tai-failed-mcp-reprobe" in caplog.text


# -- app_context spawns the task and cancels it cleanly at shutdown ------------


async def test_app_context_spawns_and_cancels_reprobe_task():
    async with app.app_context(Manifest.model_validate({})):
        task = app._reprobe_task
        assert task is not None
        assert not task.done()

    # Cleanly cancelled at shutdown — no pending-task warning, attr cleared.
    assert cast(asyncio.Task, task).cancelled()
    assert app._reprobe_task is None
