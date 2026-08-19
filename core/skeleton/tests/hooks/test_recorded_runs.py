"""Hook- and trigger-dispatched fires write the SAME run records a background submit
does, so they are listable via ``GET /api/tool-runs`` and gettable by id.

The firing path routes through ``operations.tool_runs.run_recorded``: with the tool-run
store configured a fire creates a ``running`` record and writes a terminal
(``succeeded``/``failed``) record under the fire's bound execution key; with the store OFF
the tool still runs, unrecorded. A raising tool is recorded ``failed`` AND still surfaces
through the fan-out's per-hook error log. The fire never reserves a ``max_concurrent_runs``
slot — its capacity is the hooks manager's own ``max_workers`` semaphore.

Redis is the focused in-memory fake the tool-runs tests use, yielded at the operation
module's ``client_ctx`` seam; ``tai42_app`` is a stand-in exposing the template renderer
and a tool runner that records its calls (incl. ``offload_sync``).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.hooks import HookParams
from tai42_kit.utils.detached_util import in_detached_run

from tai42_skeleton.authz.execution import bind_execution_identity
from tai42_skeleton.hooks.managers.in_memory_hooks_manager import InMemoryHooksManager
from tai42_skeleton.hooks.settings import HooksSettings
from tai42_skeleton.routers.tool_runs_settings import ToolRunsSettings

from .._fakes.tool_runs_redis import FakeRedis


class _Tools:
    """A tool runner recording each call and its ``offload_sync`` flag; a name in
    ``raise_for`` raises so the failed-record path is exercised. A ``gate`` — an
    ``asyncio.Event`` left unset — holds the tool in flight until a drain cancels it."""

    def __init__(self, raise_for: set[str] | None = None, gate: asyncio.Event | None = None) -> None:
        self.calls: list[tuple[str, dict, bool]] = []
        # The value of the detached flag observed inside each call — the fire path
        # must run the tool as detached (no live caller), so the turn budget is skipped.
        self.detached_seen: list[bool] = []
        self._raise_for = raise_for or set()
        self._gate = gate

    async def run_tool(self, name, tool_input, *, offload_sync=False):
        self.calls.append((name, tool_input, offload_sync))
        self.detached_seen.append(in_detached_run())
        if self._gate is not None:
            await self._gate.wait()
        if name in self._raise_for:
            raise RuntimeError("boom")
        return {"ok": True}


class _ResourceManager:
    async def render_by_id_or_content(self, *, content, template_id, kwargs):
        return content


class _App:
    def __init__(self, raise_for: set[str] | None = None, gate: asyncio.Event | None = None) -> None:
        self.tools = _Tools(raise_for, gate)
        self.storage = SimpleNamespace(resource_manager=_ResourceManager())


@pytest.fixture
def wired(monkeypatch):
    """Wire the tool-run store seam to the in-memory fake and bind a fake ``tai42_app``.

    ``operations.tool_runs`` is imported HERE (at test-run time), not at module top: the
    operations registry test-isolation pops and re-imports operation leaf modules, so a
    reference captured at collection can go stale — while the hook firing path imports
    ``run_recorded`` fresh at fire time from the CURRENT module. Importing the same current
    module here keeps the monkeypatches on the object the fire actually runs.

    The store presence gate reads env fresh — a test sets/clears it to toggle ON/OFF."""
    import importlib

    ops = importlib.import_module("tai42_skeleton.operations.tool_runs")

    fake = FakeRedis()
    settings = ToolRunsSettings()

    @asynccontextmanager
    async def ctx(client_cls, s=None, *, fresh=False, **kwargs):
        yield fake

    monkeypatch.setattr(ops, "client_ctx", ctx)
    monkeypatch.setattr(ops, "tool_runs_settings", lambda: settings)
    monkeypatch.setattr(ops, "_now", lambda: datetime(2026, 1, 1, tzinfo=UTC))

    def bind(raise_for: set[str] | None = None, gate: asyncio.Event | None = None) -> _App:
        app = _App(raise_for, gate)
        monkeypatch.setattr(tai42_app, "_impl", app)
        return app

    return SimpleNamespace(
        fake=fake, settings=settings, bind=bind, store=ops.ToolRunStore(settings.key_prefix), ops=ops
    )


def _hook(tool: str = "echo_tool") -> HookParams:
    return HookParams(name="h", topic="t", tool=tool, execution_key="k-fire", execution_key_fingerprint="fp-fire")


async def test_hook_fire_records_run_listable_and_gettable(wired, monkeypatch):
    # Store configured: the fire writes the full lifecycle record — listable per tool and
    # gettable by id, attributed to the fire's execution key, run through the offload seam.
    monkeypatch.setenv("TAI_TOOL_RUNS_REDIS_URL", "redis://localhost:6379/0")
    app = wired.bind()
    manager = InMemoryHooksManager(HooksSettings())
    await manager.register(_hook())

    await manager.on_event("t", {"any": 1})

    assert app.tools.calls == [("echo_tool", {}, True)]

    entries = await wired.ops.list_tool_runs("echo_tool")
    assert [e["status"] for e in entries] == ["succeeded"]
    run_id = entries[0]["run_id"]

    record = await wired.store.get_run(wired.fake, run_id)
    assert record["status"] == "succeeded"
    assert record["tool_name"] == "echo_tool"
    assert record["user_id"] == "k-fire"

    view = await wired.ops.get_run(run_id)
    assert view["result"] == {"ok": True}


async def test_store_off_runs_hook_unrecorded_without_error(wired, monkeypatch, caplog):
    # Store OFF: the tool still runs, unrecorded — nothing is written to the store and no
    # error/warning is raised — but it thread-offloads like every other execution site; the
    # OFF branch only skips recording, not the offload.
    monkeypatch.delenv("TAI_TOOL_RUNS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    app = wired.bind()
    manager = InMemoryHooksManager(HooksSettings())
    await manager.register(_hook())

    with caplog.at_level(logging.WARNING):
        await manager.on_event("t", {"any": 1})

    # The tool ran through the offload seam like every other execution site, so a
    # synchronous tool never blocks the loop; the OFF branch only skips recording.
    assert app.tools.calls == [("echo_tool", {}, True)]
    # No record, index, or liveness key was ever written.
    assert wired.fake._hashes == {}
    assert wired.fake._zsets == {}
    assert wired.fake._strings == {}
    assert caplog.records == []


async def test_hook_fire_runs_the_tool_detached_store_on(wired, monkeypatch):
    # Store configured: the fire runs through ``_supervise``, which flags the run as
    # detached around the tool invocation so the turn budget is skipped. The flag
    # is sampled INSIDE the tool call (``detached_seen``); the fan-out runs each hook in
    # its own ``asyncio.gather`` child task, so the reset is only observable in that
    # task's context — the reset itself is pinned by the direct-call tests below.
    monkeypatch.setenv("TAI_TOOL_RUNS_REDIS_URL", "redis://localhost:6379/0")
    app = wired.bind()
    manager = InMemoryHooksManager(HooksSettings())
    await manager.register(_hook())

    await manager.on_event("t", {"any": 1})

    assert app.tools.detached_seen == [True]


async def test_hook_fire_runs_the_tool_detached_store_off(wired, monkeypatch):
    # Store OFF: the bare (unrecorded) run path flags the run as detached exactly as
    # the store-ON path does. As above, the flag is sampled inside the tool call; the
    # reset is pinned by the direct-call tests below.
    monkeypatch.delenv("TAI_TOOL_RUNS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    app = wired.bind()
    manager = InMemoryHooksManager(HooksSettings())
    await manager.register(_hook())

    await manager.on_event("t", {"any": 1})

    assert app.tools.detached_seen == [True]


async def test_run_recorded_resets_detached_flag_after_success_store_on(wired, monkeypatch):
    # Store configured: ``_supervise`` runs in the enrolled supervisor task, whose own
    # copied context carries the detached flag. The tool — running in that task — observes
    # it True (``detached_seen``), while this awaiting context never sees it, so after
    # ``run_recorded`` returns ``in_detached_run()`` here is False: the flag never leaked
    # past the run, pinning ``_supervise``'s ``finally`` reset.
    monkeypatch.setenv("TAI_TOOL_RUNS_REDIS_URL", "redis://localhost:6379/0")
    app = wired.bind()

    assert in_detached_run() is False
    async with bind_execution_identity("k-fire", bound_fingerprint="fp-fire"):
        await wired.ops.run_recorded("echo_tool", {})

    assert app.tools.detached_seen == [True]
    assert in_detached_run() is False


async def test_run_recorded_resets_detached_flag_after_success_store_off(wired, monkeypatch):
    # Store OFF: the bare (unrecorded) branch of ``run_recorded`` flags the run detached,
    # runs the tool, and resets in its own ``finally``. Awaited directly here, so the
    # reset is observable in this context — True inside the call, False after.
    monkeypatch.delenv("TAI_TOOL_RUNS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    app = wired.bind()

    assert in_detached_run() is False
    async with bind_execution_identity("k-fire", bound_fingerprint="fp-fire"):
        await wired.ops.run_recorded("echo_tool", {})

    assert app.tools.detached_seen == [True]
    assert in_detached_run() is False


async def test_run_recorded_resets_detached_flag_on_failure_store_on(wired, monkeypatch):
    # Store configured, tool raises: ``_supervise`` records the failure in the enrolled
    # supervisor task and (with ``propagate_failure``) re-raises it, which ``run_recorded``
    # surfaces to this caller. The ``finally`` reset still runs in that task, so the flag
    # never leaked here — ``in_detached_run()`` is False after the raise.
    monkeypatch.setenv("TAI_TOOL_RUNS_REDIS_URL", "redis://localhost:6379/0")
    app = wired.bind(raise_for={"echo_tool"})

    async with bind_execution_identity("k-fire", bound_fingerprint="fp-fire"):
        with pytest.raises(RuntimeError, match="boom"):
            await wired.ops.run_recorded("echo_tool", {})

    assert app.tools.detached_seen == [True]
    assert in_detached_run() is False


async def test_run_recorded_resets_detached_flag_on_failure_store_off(wired, monkeypatch):
    # Store OFF, tool raises: the bare branch lets the exception propagate, and its
    # ``finally`` reset still runs, so the flag is False after the raise.
    monkeypatch.delenv("TAI_TOOL_RUNS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    app = wired.bind(raise_for={"echo_tool"})

    async with bind_execution_identity("k-fire", bound_fingerprint="fp-fire"):
        with pytest.raises(RuntimeError, match="boom"):
            await wired.ops.run_recorded("echo_tool", {})

    assert app.tools.detached_seen == [True]
    assert in_detached_run() is False


async def test_failing_hook_tool_is_recorded_failed_and_still_surfaces(wired, monkeypatch, caplog):
    # A raising tool: the record captures the failure AND the fan-out's per-hook error log
    # still fires (the run record's failure state is additive to the existing surfacing).
    monkeypatch.setenv("TAI_TOOL_RUNS_REDIS_URL", "redis://localhost:6379/0")
    wired.bind(raise_for={"echo_tool"})
    manager = InMemoryHooksManager(HooksSettings())
    await manager.register(_hook())

    with caplog.at_level(logging.ERROR):
        await manager.on_event("t", {"any": 1})

    entries = await wired.ops.list_tool_runs("echo_tool")
    assert [e["status"] for e in entries] == ["failed"]
    view = await wired.ops.get_run(entries[0]["run_id"])
    assert view["status"] == "failed"
    assert view["error"] == "boom"

    # The hooks manager's own per-hook failure log surfaced the failing hook by name.
    assert any(
        rec.name == "tai42_skeleton.hooks.managers.base_hooks_manager"
        and rec.levelno == logging.ERROR
        and "'h'" in rec.getMessage()
        for rec in caplog.records
    )


async def test_hook_fired_run_in_flight_is_drained_to_failed(wired, monkeypatch):
    # A hook-fired run blocked in its tool when ``drain_supervisors`` runs must be
    # cancelled-and-awaited exactly like a submitted run: it enrols in the drain registry,
    # so the drain writes its terminal ``failed`` record naming the drain reason BEFORE the
    # pooled clients close — the record is NEVER left ``running`` to reconcile to ``lost``.
    # The awaiting ``run_recorded`` surfaces the cancellation. Mirrors the submit-drain
    # idiom: a gate held open keeps the run in flight, the drain cancels it, its cancel
    # branch CAS-writes the ``failed`` record, and the supervisor set drains empty.
    monkeypatch.setenv("TAI_TOOL_RUNS_REDIS_URL", "redis://localhost:6379/0")
    gate = asyncio.Event()  # never set — the run stays in flight until the drain cancels it
    app = wired.bind(gate=gate)

    async with bind_execution_identity("k-fire", bound_fingerprint="fp-fire"):
        run = asyncio.create_task(wired.ops.run_recorded("echo_tool", {}))
    # Let the run create its record and reach the gated tool.
    while not app.tools.calls and not run.done():
        await asyncio.sleep(0)
    if run.done():
        run.result()  # a run that finished before reaching the tool re-raises its failure
    # It enrolled in the drain registry, exactly like a submitted run.
    assert [t for t in wired.ops._SUPERVISORS if not t.done()]
    run_id = (await wired.ops.list_tool_runs("echo_tool"))[0]["run_id"]

    reason = "the serving epoch was retired by a config apply before the tool-run completed"
    await wired.ops.drain_supervisors(reason=reason)

    # The drain cancelled-and-awaited the run: its terminal ``failed`` record names the
    # drain reason and the record is NOT left ``running``.
    record = await wired.store.get_run(wired.fake, run_id)
    assert record["status"] == "failed"
    assert record["status"] != "running"
    assert record["error"] == reason
    assert record["finished_at"]
    # The awaiting ``run_recorded`` surfaced the cancellation; the drain registry is clear.
    with pytest.raises(asyncio.CancelledError):
        await run
    assert set() == wired.ops._SUPERVISORS
