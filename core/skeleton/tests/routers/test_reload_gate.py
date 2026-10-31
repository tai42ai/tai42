"""The awaitable reload gate.

Proves the reload lock's three surfaces: a gated route answers the retriable 503
while a reload holds the gate; the serving loop is NOT frozen during a reload
(health + a read-only GET answer concurrently while the reload sleeps on its
worker thread); the FastMCP session middleware rejects a tool call the same way;
after release everything answers again; the reload-through-gate path runs its
reload on a worker thread and returns the reload result under a ``{"data": ...}``
envelope; and the body runs under the process fork gate, so a forking backend's
job child can never be alive while the manifest modules are re-imported.

Handlers are driven directly (the router-test pattern); the gate is the
process-wide :data:`reload_gate` singleton the routers import.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Coroutine
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastmcp.exceptions import ToolError
from starlette.requests import Request
from tai42_contract.app import tai42_app
from tai42_kit.fork_gate import fork_gate

from tai42_skeleton.app import instance
from tai42_skeleton.app.reload_gate import REJECT_MESSAGE, ReloadGate, reload_gate
from tai42_skeleton.app.server import TaiMCP
from tai42_skeleton.app.sessions import ReloadRejectionMiddleware
from tai42_skeleton.routers import agents as agents_router
from tai42_skeleton.routers import backup as backup_router
from tai42_skeleton.routers import config as config_router
from tai42_skeleton.routers import health as health_router
from tai42_skeleton.routers import manifest as manifest_router
from tai42_skeleton.routers import presets as presets_router
from tai42_skeleton.routers import schedules as schedules_router
from tai42_skeleton.routers import sub_mcp as sub_mcp_router
from tai42_skeleton.routers import tool_runs as tool_runs_router
from tai42_skeleton.routers import tools as tools_router

from .._fakes.bus import FakeBus
from .._fakes.mcp_listing import no_mcp_listing
from .._fakes.write_span import ExclusiveWriteStore


def _dummy_req(**path_params: Any) -> Request:
    """A stand-in request for a handler that rejects before touching the request
    (the gate check short-circuits ahead of any body/path-param read)."""
    return cast(Request, SimpleNamespace(path_params=path_params))


def _json(resp: Any) -> dict[str, Any]:
    return json.loads(bytes(resp.body))


def _body_req(body: bytes, path: str) -> Request:
    scope = {"type": "http", "method": "POST", "path": path, "headers": [], "query_string": b""}
    delivered = {"done": False}

    async def receive() -> dict[str, Any]:
        if delivered["done"]:
            return {"type": "http.disconnect"}
        delivered["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


class _FakeTools:
    def __init__(self, tools: dict[str, Any]) -> None:
        self._tools = tools

    async def get_tools(self) -> dict[str, Any]:
        return self._tools

    async def has_tool(self, key: str) -> bool:
        return key in self._tools


# -- reject_response shape ---------------------------------------------------


async def test_reject_response_is_retriable_503() -> None:
    resp = reload_gate.reject_response()
    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "5"
    assert _json(resp) == {"error": REJECT_MESSAGE, "reloading": True}


# -- run() offloads to a worker thread ---------------------------------------


async def test_run_offloads_to_worker_thread_and_returns_result() -> None:
    """The heavy body runs on a worker thread (not the serving loop's thread) and
    ``run`` returns its result; the lock is held during and released after."""
    gate = ReloadGate()
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def body() -> dict[str, str]:
        seen["thread"] = threading.get_ident()
        return {"status": "ok"}

    assert not gate.locked
    result = await gate.run(body, reimports=False)
    assert result == {"status": "ok"}
    assert seen["thread"] != loop_thread
    assert not gate.locked


# -- run(reimports=True) also excludes a forking backend's job children ------


async def test_a_reimporting_body_holds_the_fork_gate_for_its_whole_span() -> None:
    """A re-importing body pops and re-imports the manifest modules, so while it runs no
    child may be forked and no in-process job may run: ``run`` holds the process fork
    gate around it, and holds it OWNED by the thread running the body (not merely
    pending)."""
    gate = ReloadGate()
    seen: dict[str, Any] = {}

    def body() -> None:
        seen["blocked"] = fork_gate.blocked
        seen["owner"] = fork_gate._owner
        seen["thread"] = threading.get_ident()

    assert not fork_gate.blocked
    await gate.run(body, reimports=True)
    assert seen["blocked"] is True
    # The hold is owned by the very thread running the body — the sync door takes the
    # gate inline rather than through a holder thread.
    assert seen["owner"] == seen["thread"]
    assert not fork_gate.blocked
    assert fork_gate._owner is None


async def test_a_non_reimporting_body_does_not_hold_the_fork_gate() -> None:
    """An MCP re-probe/rebind imports nothing, so it must NOT stall a forking backend's
    jobs for its duration — the gate stays free."""
    gate = ReloadGate()
    seen: dict[str, Any] = {}

    def body() -> None:
        seen["blocked"] = fork_gate.blocked
        seen["owner"] = fork_gate._owner

    assert not fork_gate.blocked
    await gate.run(body, reimports=False)
    assert seen["blocked"] is False
    assert seen["owner"] is None


async def test_run_releases_the_fork_gate_when_the_body_raises() -> None:
    """A failed reload must not leave the fork gate held — that would block every
    subsequent job child for the process lifetime."""
    gate = ReloadGate()

    def body() -> None:
        raise RuntimeError("reload blew up")

    with pytest.raises(RuntimeError, match="reload blew up"):
        await gate.run(body, reimports=True)
    assert not fork_gate.blocked
    assert fork_gate._owner is None
    assert not gate.locked


async def test_a_reimporting_reload_a_job_runs_does_not_wait_on_that_jobs_span(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A job that runs a reload (the reload projected as a tool) holds a job span; the
    reload's fork-gate hold passes that span and runs the body at once."""
    gate = ReloadGate()
    ran = threading.Event()

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        async with fork_gate.job_span_async(timeout=5.0):
            started = time.monotonic()
            await gate.run(ran.set, reimports=True)
            waited = time.monotonic() - started

    assert ran.is_set()
    assert waited < 3.0
    assert not caplog.records
    assert not fork_gate.blocked


async def test_two_jobs_running_a_reimporting_reload_at_once_do_not_hold_each_other_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two jobs each run a reload at once: the first holds the reload lock and quiesces the
    fork gate while the second waits for that lock with its own job span open. The first
    reload's body starts at once, with no quiesce error, and then the second runs."""
    gate = ReloadGate()
    entered: dict[str, float] = {}
    first_holds_the_lock = asyncio.Event()
    started = time.monotonic()

    def body(name: str) -> None:
        entered[name] = time.monotonic() - started

    async def first_job() -> None:
        async with fork_gate.job_span_async(timeout=5.0):

            async def reload() -> None:
                await gate.run(lambda: body("first"), reimports=True)

            task = asyncio.create_task(reload())
            while not gate.locked:
                await asyncio.sleep(0.01)
            first_holds_the_lock.set()
            await task

    async def second_job() -> None:
        async with fork_gate.job_span_async(timeout=5.0):
            await first_holds_the_lock.wait()
            await gate.run(lambda: body("second"), reimports=True)

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        second = asyncio.create_task(second_job())
        await asyncio.sleep(0.05)
        await asyncio.wait_for(asyncio.gather(first_job(), second), 60.0)

    assert entered["first"] < 3.0
    assert entered["second"] >= entered["first"]
    assert not caplog.records
    assert not fork_gate.blocked


def _on_a_loop_of_its_own(
    work: Callable[[], Coroutine[Any, Any, None]],
) -> tuple[threading.Thread, list[BaseException]]:
    """Start a thread that runs ``work()`` on a new event loop of its own; collect what it raises.

    The shape of a backend job running on its private loop beside the serving loop.
    """
    errors: list[BaseException] = []

    def run() -> None:
        try:
            asyncio.run(work())
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, name="test-own-loop")
    thread.start()
    return thread, errors


async def test_the_gate_excludes_holders_on_two_loops_in_two_threads() -> None:
    """A serving loop and a job's private loop both hold the gate: the holds never overlap,
    and the loop that is not holding reads the gate as locked while the other holds."""
    gate = ReloadGate()
    windows: list[tuple[float, float]] = []
    first_holds = threading.Event()
    seen_locked: list[bool] = []

    async def first() -> None:
        async with gate.driving():
            started = time.monotonic()
            first_holds.set()
            await asyncio.sleep(0.3)
            windows.append((started, time.monotonic()))

    async def second() -> None:
        assert await asyncio.to_thread(first_holds.wait, 5.0)
        seen_locked.append(gate.locked)
        async with gate.driving():
            started = time.monotonic()
            await asyncio.sleep(0.3)
            windows.append((started, time.monotonic()))

    threads = [_on_a_loop_of_its_own(first), _on_a_loop_of_its_own(second)]
    for thread, _ in threads:
        await asyncio.to_thread(thread.join, 10.0)
    assert [errors for _, errors in threads] == [[], []]
    assert seen_locked == [True]
    (first_start, first_end), (second_start, _) = windows
    assert second_start >= first_end > first_start


async def test_a_second_loop_touching_the_gate_keeps_the_first_loops_hold() -> None:
    """While this loop holds the gate, another loop on another thread asks for it: this loop's
    gate still reads as locked, and a second holder on this loop waits for the first to release."""
    gate = ReloadGate()
    order: list[str] = []
    asking = threading.Event()

    async def other_loop() -> None:
        asking.set()
        async with gate.synchronized():
            order.append("other loop held")

    async def second_holder() -> None:
        async with gate.driving():
            order.append("second holder held")

    async with gate.driving():
        thread, errors = _on_a_loop_of_its_own(other_loop)
        assert await asyncio.to_thread(asking.wait, 5.0)
        await asyncio.sleep(0.2)
        assert gate.locked
        second = asyncio.create_task(second_holder())
        await asyncio.sleep(0.2)
        assert gate.locked
        assert order == []
        order.append("first holder released")
    await asyncio.wait_for(second, 10.0)
    await asyncio.to_thread(thread.join, 10.0)
    assert errors == []
    assert order[0] == "first holder released"
    assert sorted(order[1:]) == ["other loop held", "second holder held"]
    assert not gate.locked


async def test_a_job_on_its_own_loop_running_a_reimporting_reload_beside_another_does_not_hold_it_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two jobs each run a reload at once, the second on its own loop and thread inside its job
    span: the first holds the reload lock and quiesces the fork gate while the second waits for
    that lock. The first reload's body starts at once, with no quiesce error, and the second's
    starts only once the first's has ended."""
    gate = ReloadGate()
    windows: dict[str, tuple[float, float]] = {}
    started = time.monotonic()

    def body(name: str) -> None:
        began = time.monotonic() - started
        if name == "first":
            time.sleep(0.5)
        windows[name] = (began, time.monotonic() - started)

    async def second_reload() -> None:
        await _until(lambda: gate.locked)
        await gate.run(lambda: body("second"), reimports=True)

    errors: list[BaseException] = []

    def second_job() -> None:
        try:
            with fork_gate.job_span(timeout=5.0):
                asyncio.run(second_reload())
        except BaseException as exc:
            errors.append(exc)

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        second = threading.Thread(target=second_job, name="test-second-job")
        second.start()
        await asyncio.sleep(0.05)
        async with fork_gate.job_span_async(timeout=5.0):
            await asyncio.wait_for(gate.run(lambda: body("first"), reimports=True), 60.0)
        await asyncio.to_thread(second.join, 60.0)

    assert errors == []
    first_began, first_ended = windows["first"]
    assert first_began < 3.0
    assert windows["second"][0] >= first_ended
    assert not caplog.records
    assert not fork_gate.blocked


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5.0):
        while not condition():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
async def test_a_job_whose_wait_for_the_reload_lock_stops_early_runs_on_only_after_the_reimport(
    stop: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A job queued on the reload lock behind a re-importing reload does not hold that reload up.
    When its wait stops early (its own timeout, or a cancellation), the code it runs next still
    waits for the re-import to end: the job counts again before it runs."""
    gate = ReloadGate()
    span_open, lock_held = asyncio.Event(), asyncio.Event()
    in_body, may_end, body_ended = threading.Event(), threading.Event(), threading.Event()
    seen: list[bool] = []

    def body() -> None:
        in_body.set()
        assert may_end.wait(10.0)
        body_ended.set()

    async def job() -> None:
        async with fork_gate.job_span_async(timeout=5.0):
            span_open.set()
            await lock_held.wait()
            try:
                if stop == "timeout":
                    async with asyncio.timeout(0.2):
                        await gate.run(lambda: None, reimports=True)
                else:
                    await gate.run(lambda: None, reimports=True)
            finally:
                seen.append(body_ended.is_set())

    with caplog.at_level(logging.ERROR, logger="tai42_kit.fork_gate"):
        queued = asyncio.create_task(job())
        await span_open.wait()
        reload = asyncio.create_task(gate.run(body, reimports=True))
        while not gate.locked:
            await asyncio.sleep(0.01)
        lock_held.set()
        assert await asyncio.to_thread(in_body.wait, 3.0)
        if stop == "cancel":
            queued.cancel()
        await asyncio.sleep(0.5)
        assert seen == []
        may_end.set()
        await asyncio.wait_for(reload, 10.0)
        with pytest.raises(TimeoutError if stop == "timeout" else asyncio.CancelledError):
            await asyncio.wait_for(queued, 10.0)

    assert seen == [True]
    assert not caplog.records
    assert not fork_gate.blocked
    assert fork_gate.live_spans == 0


# -- gated routes reject while the gate is held ------------------------------


async def test_gated_routes_reject_while_reloading(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every run / live-registry-mutation / reload-trigger route answers the
    retriable 503 while a reload holds the gate, then answers normally after."""

    async def _assert_rejected(resp: Any) -> None:
        assert resp.status_code == 503
        assert resp.headers["Retry-After"] == "5"
        assert _json(resp) == {"error": REJECT_MESSAGE, "reloading": True}

    async with reload_gate.synchronized():
        assert reload_gate.locked
        # A run surface.
        await _assert_rejected(await tools_router.run_tool(_body_req(b"{}", "/api/run-tool")))
        await _assert_rejected(await tool_runs_router.submit_run(_body_req(b"{}", "/api/tool-runs")))
        await _assert_rejected(await agents_router.run_agent(_dummy_req(name="a")))
        await _assert_rejected(await agents_router.run_authored_agent(_dummy_req(name="a")))
        # A live-registry mutation.
        await _assert_rejected(await sub_mcp_router.register_sub_mcp(_body_req(b"{}", "/api/sub-mcp")))
        await _assert_rejected(await sub_mcp_router.unregister_sub_mcp(_dummy_req(slug="s")))
        await _assert_rejected(await presets_router.create_preset(_body_req(b"{}", "/api/presets")))
        await _assert_rejected(await backup_router.import_backup(_body_req(b"{}", "/api/backup/import")))
        # The schedule create/delete doors dispatch a tool run through the same
        # run-tool seam, so they are gated too (the read-only list/datetime are not).
        await _assert_rejected(await schedules_router.create_schedule(_body_req(b"{}", "/api/schedules")))
        await _assert_rejected(await schedules_router.delete_schedule(_dummy_req(schedule_name="s")))
        # A reload-trigger route — a second reload gets the same 503, never a queue.
        await _assert_rejected(await config_router.write_env(_body_req(b"{}", "/api/config/env")))
        await _assert_rejected(await manifest_router.set_mcp_config(_body_req(b"{}", "/api/mcp-config")))
        await _assert_rejected(await manifest_router.reload_mcp(_dummy_req(title="gh")))

    # Released: a run surface is past the gate again — the request reaches normal
    # handling (here a 400 for the missing tool name), not the 503 rejection.
    assert not reload_gate.locked
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(tools=_FakeTools({})))
    resp = await tools_router.run_tool(_body_req(b"{}", "/api/run-tool"))
    assert resp.status_code == 400


# -- the serving loop is NOT frozen during a reload --------------------------


async def test_serving_loop_not_frozen_during_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    """While a reload holds the gate and sleeps on its worker thread, /health and a
    read-only GET both answer 200 on the still-running serving loop."""
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(tools=_FakeTools({"a": object(), "b": object()})))
    loop_thread = threading.get_ident()
    entered = threading.Event()
    release = threading.Event()
    worker: dict[str, int] = {}

    def blocking_reload() -> dict[str, Any]:
        worker["thread"] = threading.get_ident()
        entered.set()
        # Sleep on the worker thread until the test lets go, leaving the serving
        # loop free the whole time.
        assert release.wait(timeout=5)
        return {"status": "ok", "env_keys": 3}

    task = asyncio.create_task(reload_gate.run(blocking_reload, reimports=True))
    try:
        for _ in range(500):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        assert reload_gate.locked
        # The serving loop keeps answering while the reload runs on its thread.
        health_resp = await health_router.health_check(cast(Request, object()))
        assert health_resp.status_code == 200
        assert bytes(health_resp.body) == b"OK"
        listing = await tools_router.list_tools(_dummy_req())
        assert listing.status_code == 200
        assert _json(listing) == {"data": ["a", "b"]}
    finally:
        release.set()
        result = await task

    assert result == {"status": "ok", "env_keys": 3}
    assert worker["thread"] != loop_thread  # the reload really ran off the serving thread
    assert not reload_gate.locked


# -- FastMCP session middleware ----------------------------------------------


async def test_middleware_rejects_session_tool_call_while_reloading() -> None:
    """A session ``tools/call`` raises the retriable reloading error while the gate
    is held, and passes through once released — never dispatching while held."""
    middleware = ReloadRejectionMiddleware(reload_gate)
    calls: list[str] = []

    async def call_next(context: Any) -> str:
        calls.append("dispatched")
        return "tool-result"

    async with reload_gate.synchronized():
        with pytest.raises(ToolError) as excinfo:
            await middleware.on_call_tool(cast(Any, object()), call_next)
        assert str(excinfo.value) == REJECT_MESSAGE
    assert calls == []  # the held call never reached dispatch

    # Released: the call dispatches normally.
    result = await middleware.on_call_tool(cast(Any, object()), call_next)
    assert result == "tool-result"
    assert calls == ["dispatched"]


def _fork_and_call_a_gated_tool() -> str:
    """Fork, and in the child run one session tool call through the reload-rejection middleware.

    Runs on a worker thread, as a backend pool forks its job children off the serving loop, so
    the child's only thread has no running loop and can run the call on a loop of its own.
    Returns what the child reports: ``dispatched``, or the rejection message.
    """
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child exits through os._exit
        try:
            os.close(read_fd)

            async def call_next(context: Any) -> str:
                return "dispatched"

            async def call() -> str:
                try:
                    return await ReloadRejectionMiddleware(reload_gate).on_call_tool(cast(Any, object()), call_next)
                except ToolError as exc:
                    return str(exc)

            os.write(write_fd, asyncio.run(call()).encode())
        finally:
            os._exit(0)
    os.close(write_fd)
    try:
        with os.fdopen(read_fd) as pipe:
            return pipe.read()
    finally:
        os.waitpid(pid, 0)


async def test_a_child_forked_while_the_parent_holds_the_gate_serves_gated_calls() -> None:
    """A forked child runs no reload of its own, and the parent's hold can never be released in
    it, so the child starts with the gate free: a backend job child forked while its worker
    holds the gate (a fleet op applying, a re-probe pass) runs its gated calls instead of
    answering ``reloading`` for its whole life. The parent's own calls are still rejected."""
    async with reload_gate.synchronized():
        assert await asyncio.to_thread(_fork_and_call_a_gated_tool) == "dispatched"
        assert reload_gate.locked


def test_reload_rejection_middleware_registered_on_server() -> None:
    """A constructed server wires ``ReloadRejectionMiddleware`` onto its FastMCP
    middleware stack — dropping the ``add_middleware`` call would fail here, not
    just the isolated on_call_tool behaviour above."""
    server = TaiMCP(name="reload-gate-under-test", version="1.0")
    assert any(isinstance(mw, ReloadRejectionMiddleware) for mw in server.fastmcp.middleware)


# -- real reload-through-gate result shape -----------------------------------


class _FakeConfigManager(ExclusiveWriteStore):
    def __init__(self) -> None:
        super().__init__()
        self._env: dict[str, str] = {}
        self.written: list[dict[str, str]] = []

    def read_env(self) -> dict[str, str]:
        return self._env

    def read_manifest_preserved(self) -> dict[str, str]:
        # No backend registered, so the env-change invariant has nothing to reject.
        return {}

    def write_env(self, config: dict[str, str]) -> None:
        self.written.append(config)
        self._env = {**self._env, **config}


class _FakeAdmin:
    def __init__(self, manager: _FakeConfigManager) -> None:
        self._manager = manager
        self.reloads = 0
        self.reload_thread: int | None = None

    def reload_config(self) -> dict[str, Any]:
        self.reloads += 1
        self.reload_thread = threading.get_ident()
        return {"status": "ok", "env_keys": len(self._manager._env)}

    served_mcp_names = staticmethod(no_mcp_listing)


async def test_write_env_reloads_through_gate_same_result_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """``POST /api/config/env`` runs its reload through the gate on a worker thread
    and returns the reload result under a ``{"data": <reload result>}`` envelope."""
    manager = _FakeConfigManager()
    admin = _FakeAdmin(manager)
    monkeypatch.setattr(
        tai42_app,
        "_impl",
        SimpleNamespace(
            config=SimpleNamespace(config_manager=manager),
            admin=admin,
            backends=SimpleNamespace(backend=None),
        ),
    )
    monkeypatch.setattr(instance.app, "_bus", FakeBus(origin="serve-x"))

    assert not reload_gate.locked
    resp = await config_router.write_env(_body_req(b'{"env": {"NEW": "val"}}', "/api/config/env"))

    assert resp.status_code == 200
    assert _json(resp) == {
        "data": {
            "status": "ok",
            "env_keys": 1,
            "fanout": {"mode": "local-only", "note": "no worker bus configured; only this worker reloaded"},
            "recycle": None,
            "awaiting_respawn": [],
            "span_release_error": None,
        }
    }
    assert manager.written == [{"NEW": "val"}]
    assert admin.reloads == 1
    # The reload ran on a worker thread, not the serving loop's thread.
    assert admin.reload_thread is not None
    assert admin.reload_thread != threading.get_ident()
    assert not reload_gate.locked  # released after the route returns


# -- the work engaged with the gate, recorded on its generation ----------------


def _serve_on(epoch: Any, body: Any) -> asyncio.Task[None]:
    """Serve ``body`` as one HTTP request admitted on ``epoch`` by the real admission wrapper."""
    from tai42_skeleton.app.admission import EpochAdmissionApp

    async def _app(scope: Any, receive: Any, send: Any) -> None:
        await body()

    return asyncio.create_task(EpochAdmissionApp(_app, epoch)({"type": "http"}, None, None))  # type: ignore[arg-type]


async def test_driving_records_the_request_as_waiting_then_driving_until_it_finishes() -> None:
    from tai42_skeleton.app.epoch import Epoch

    epoch = Epoch(number=0)
    inside = asyncio.Event()
    after_gate = asyncio.Event()
    finish = asyncio.Event()

    async def _body() -> None:
        async with reload_gate.driving():
            inside.set()
        after_gate.set()
        await finish.wait()

    async with reload_gate.synchronized():
        request = _serve_on(epoch, _body)
        while not epoch.waiting:
            await asyncio.sleep(0)
        # Waiting: it cannot move, so it is out of the in-flight count; not yet driving.
        assert (len(epoch.waiting), epoch.driving, epoch.in_flight) == (1, set(), 0)
        assert reload_gate.locked
    await inside.wait()
    (state,) = epoch.driving
    # Driving: out of the in-flight count, no longer waiting.
    assert (epoch.waiting, epoch.in_flight, state.slot_released) == (set(), 0, True)
    await after_gate.wait()
    assert epoch.driving == {state}, "a driver stays recorded after the gated body until its request ends"
    assert not reload_gate.locked
    finish.set()
    await request
    assert (epoch.driving, epoch.waiting, epoch.in_flight, state.finished.is_set()) == (set(), set(), 0, True)


async def test_synchronized_records_the_request_as_waiting_only_while_it_waits() -> None:
    from tai42_skeleton.app.epoch import Epoch

    epoch = Epoch(number=0)
    inside = asyncio.Event()
    finish = asyncio.Event()

    async def _body() -> None:
        async with reload_gate.synchronized():
            inside.set()
            await finish.wait()

    async with reload_gate.synchronized():
        request = _serve_on(epoch, _body)
        while not epoch.waiting:
            await asyncio.sleep(0)
        # Waiting: out of the in-flight count while it cannot move.
        assert epoch.in_flight == 0
    await inside.wait()
    # Holding the gate as a pass-through: ordinary in-flight work again.
    assert (epoch.waiting, epoch.driving, epoch.in_flight) == (set(), set(), 1)
    finish.set()
    await request
    assert epoch.in_flight == 0


async def test_a_waiter_cancelled_on_the_gate_leaves_no_record() -> None:
    from tai42_skeleton.app.epoch import Epoch

    epoch = Epoch(number=0)

    async def _body() -> None:
        async with reload_gate.driving():
            raise AssertionError("never acquires")  # pragma: no cover

    async with reload_gate.synchronized():
        request = _serve_on(epoch, _body)
        while not epoch.waiting:
            await asyncio.sleep(0)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
    assert (epoch.waiting, epoch.driving, epoch.in_flight) == (set(), set(), 0)


async def test_work_outside_any_request_or_run_holds_the_gate_and_records_nothing() -> None:
    from tai42_skeleton.app.admission import current_request_admission
    from tai42_skeleton.app.epoch import Epoch

    epoch = Epoch(number=0)
    assert current_request_admission() is None
    async with reload_gate.driving():
        assert reload_gate.locked
    async with reload_gate.synchronized():
        assert reload_gate.locked
    assert not reload_gate.locked
    assert not epoch.engaged
