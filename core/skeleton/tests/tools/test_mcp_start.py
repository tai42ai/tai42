"""MCP server starts: a caller's wait never ends a server start; the start runs on the serving loop."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import threading
import time
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_kit.fork_gate import fork_gate

from tai42_skeleton.app import instance
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.tools import mcp_start
from tai42_skeleton.tools.mcp_start import McpStartPending, StartId, start_listing

from ..app.lifecycle._doubles import _mixin
from ..connectors.credential_path import bind_as_serving, job_loop, owner_loop

# A guarded fork that waited on its own reload would take this long; one that does not, milliseconds.
_GUARD_WAIT = 30.0
_PROMPT = 3.0

# A scope a caller sets around its wait, as a request sets its identity.
_CALLER_SCOPE: contextvars.ContextVar[str] = contextvars.ContextVar("caller_scope", default="<none>")


@pytest.fixture(autouse=True)
async def _no_start_left_running() -> AsyncIterator[None]:
    """Each test leaves no start of its loop running."""
    yield
    await mcp_start.aclose()


class _Run:
    """A start that sleeps ``seconds`` then returns ``result`` or raises ``error``; records how it ended."""

    def __init__(self, seconds: float, *, result: Any = "tools", error: BaseException | None = None) -> None:
        self.seconds = seconds
        self.result = result
        self.error = error
        self.cancelled = threading.Event()
        self.finished = threading.Event()

    async def __call__(self) -> Any:
        try:
            await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        self.finished.set()
        if self.error is not None:
            raise self.error
        return self.result


class _Hook:
    """A late-outcome hook that records every outcome it is handed."""

    def __init__(self) -> None:
        self.outcomes: list[Any] = []
        self.called = threading.Event()

    def __call__(self, _start: StartId, outcome: Any) -> None:
        self.outcomes.append(outcome)
        self.called.set()


def _starts_on(loop: asyncio.AbstractEventLoop) -> list[asyncio.Task[Any]]:
    return mcp_start._flights.on_loop(loop)


@pytest.mark.usefixtures("serving_here")
async def test_a_result_inside_the_wait_is_returned_and_the_hook_never_fires() -> None:
    hook = _Hook()
    assert (
        await start_listing(_Run(0.01), title="svc", key="svc", wait=5.0, lifetime=10.0, on_late_outcome=hook)
        == "tools"
    )
    await asyncio.sleep(0.05)
    assert hook.outcomes == []


@pytest.mark.usefixtures("serving_here")
async def test_a_failure_inside_the_wait_raises_the_starts_own_error_and_the_hook_never_fires() -> None:
    hook = _Hook()
    with pytest.raises(ConnectionError, match="refused"):
        await start_listing(
            _Run(0.01, error=ConnectionError("refused")),
            title="svc",
            key="svc",
            wait=5.0,
            lifetime=10.0,
            on_late_outcome=hook,
        )
    await asyncio.sleep(0.05)
    assert hook.outcomes == []


@pytest.mark.usefixtures("serving_here")
async def test_a_start_that_outruns_the_wait_is_not_cancelled_and_hands_its_result_to_the_hook_once() -> None:
    run, hook = _Run(0.5), _Hook()
    with pytest.raises(McpStartPending) as raised:
        await start_listing(run, title="svc", key="svc", wait=0.05, lifetime=10.0, on_late_outcome=hook)
    assert (raised.value.title, raised.value.wait, raised.value.lifetime) == ("svc", 0.05, 10.0)
    assert await asyncio.to_thread(hook.called.wait, 5)
    await asyncio.sleep(0.05)
    assert run.finished.is_set()
    assert not run.cancelled.is_set()
    assert hook.outcomes == ["tools"]


@pytest.mark.usefixtures("serving_here")
async def test_a_start_that_fails_after_the_wait_hands_its_exception_to_the_hook_once() -> None:
    error = ConnectionError("closed")
    hook = _Hook()
    with pytest.raises(McpStartPending):
        await start_listing(
            _Run(0.3, error=error), title="svc", key="svc", wait=0.05, lifetime=10.0, on_late_outcome=hook
        )
    assert await asyncio.to_thread(hook.called.wait, 5)
    await asyncio.sleep(0.05)
    assert hook.outcomes == [error]


@pytest.mark.usefixtures("serving_here")
async def test_a_start_that_outruns_the_start_budget_is_cancelled_and_raises_naming_the_budget() -> None:
    run, hook = _Run(30.0), _Hook()
    with pytest.raises(TimeoutError, match=r"^MCP server 'svc' did not start within the start budget 0\.2s$"):
        await start_listing(run, title="svc", key="svc", wait=None, lifetime=0.2, on_late_outcome=hook)
    assert run.cancelled.is_set()
    assert hook.outcomes == []


@pytest.mark.usefixtures("serving_here")
async def test_a_wait_as_long_as_the_start_budget_waits_for_the_start_itself() -> None:
    # Equal budgets never race into a pending answer: the caller sees the start's own end.
    run = _Run(30.0)
    with pytest.raises(TimeoutError, match="did not start within the start budget"):
        await start_listing(run, title="svc", key="svc", wait=0.2, lifetime=0.2, on_late_outcome=_Hook())
    assert run.cancelled.is_set()


@pytest.mark.usefixtures("serving_here")
async def test_a_late_start_that_outruns_the_start_budget_hands_the_budget_error_to_the_hook() -> None:
    run, hook = _Run(30.0), _Hook()
    with pytest.raises(McpStartPending):
        await start_listing(run, title="svc", key="svc", wait=0.05, lifetime=0.3, on_late_outcome=hook)
    assert await asyncio.to_thread(hook.called.wait, 5)
    assert run.cancelled.is_set()
    [outcome] = hook.outcomes
    assert isinstance(outcome, TimeoutError)
    assert str(outcome) == "MCP server 'svc' did not start within the start budget 0.3s"


@pytest.mark.usefixtures("serving_here")
async def test_a_caller_cancelled_while_waiting_leaves_the_start_running_and_hands_its_outcome_to_the_hook() -> None:
    run, hook = _Run(0.4), _Hook()
    waiting = asyncio.create_task(
        start_listing(run, title="svc", key="svc", wait=None, lifetime=10.0, on_late_outcome=hook)
    )
    await asyncio.sleep(0.05)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert await asyncio.to_thread(hook.called.wait, 5)
    assert not run.cancelled.is_set()
    assert hook.outcomes == ["tools"]


def test_a_late_start_ends_with_its_loops_close_and_raises_nothing(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The start is a task of the serving loop, which closes before the start completes: the
    # close cancels the start, its delivery logs the cancel, and the hook is handed nothing.
    run, hook = _Run(0.3), _Hook()

    async def caller() -> None:
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(instance, "_app", SimpleNamespace(serving_loop=lambda: loop))
        with pytest.raises(McpStartPending):
            await start_listing(run, title="svc", key="svc", wait=0.05, lifetime=10.0, on_late_outcome=hook)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(caller())
    assert run.cancelled.is_set()
    assert hook.outcomes == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert any(
        r.levelno == logging.INFO and r.getMessage() == "MCP server 'svc' start cancelled before it completed"
        for r in caplog.records
    )


@pytest.mark.usefixtures("serving_here")
async def test_a_hook_that_raises_is_logged_at_error_naming_the_title(caplog: pytest.LogCaptureFixture) -> None:
    raised = threading.Event()

    def hook(_start: StartId, _outcome: Any) -> None:
        raised.set()
        raise RuntimeError("apply broke")

    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.tools.mcp_start"):
        with pytest.raises(McpStartPending):
            await start_listing(_Run(0.2), title="svc", key="svc", wait=0.05, lifetime=10.0, on_late_outcome=hook)
        assert await asyncio.to_thread(raised.wait, 5)
        await asyncio.sleep(0.05)
    [record] = [r for r in caplog.records if r.name == "tai42_skeleton.tools.mcp_start"]
    assert record.levelno == logging.ERROR
    assert "'svc'" in record.getMessage()
    assert record.exc_info is not None
    assert str(record.exc_info[1]) == "apply broke"


@pytest.mark.usefixtures("serving_here")
async def test_aclose_cancels_every_pending_start_of_its_loop(caplog: pytest.LogCaptureFixture) -> None:
    loop = asyncio.get_running_loop()
    run, hook = _Run(30.0), _Hook()
    with pytest.raises(McpStartPending):
        await start_listing(run, title="svc", key="svc", wait=0.05, lifetime=60.0, on_late_outcome=hook)
    assert len(_starts_on(loop)) == 1
    with caplog.at_level(logging.INFO, logger="tai42_skeleton.tools.mcp_start"):
        await mcp_start.aclose()
    assert run.cancelled.is_set()
    assert _starts_on(loop) == []
    assert hook.outcomes == []
    assert any("'svc'" in r.getMessage() and "cancelled" in r.getMessage() for r in caplog.records)


@pytest.mark.usefixtures("serving_here")
async def test_a_caller_waiting_when_its_start_is_closed_gets_a_loud_error() -> None:
    run = _Run(30.0)
    waiting = asyncio.create_task(
        start_listing(run, title="svc", key="svc", wait=None, lifetime=60.0, on_late_outcome=_Hook())
    )
    await asyncio.sleep(0.05)
    await mcp_start.aclose()
    with pytest.raises(RuntimeError, match=r"^MCP server 'svc' start cancelled before it completed$"):
        await waiting


@pytest.mark.usefixtures("serving_here")
async def test_starts_run_again_after_aclose() -> None:
    hook = _Hook()
    assert await start_listing(_Run(0.01), title="a", key="a", wait=5.0, lifetime=10.0, on_late_outcome=hook) == "tools"
    await mcp_start.aclose()
    assert await start_listing(_Run(0.01), title="a", key="a", wait=5.0, lifetime=10.0, on_late_outcome=hook) == "tools"
    assert hook.outcomes == []


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is not available on this platform")
@pytest.mark.parametrize("parent_start_in_flight", [False, True], ids=["parent idle", "parent start in flight"])
def test_a_forked_child_launches_its_own_start_and_never_joins_the_parents(
    parent_start_in_flight: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The parent's start tasks do not run in a forked child, so a start of the same key there is its own.
    lifecycle = _mixin()
    monkeypatch.setattr(instance, "_app", lifecycle)

    async def one() -> Any:
        return await start_listing(_Run(0.01), title="svc", key="svc", wait=5.0, lifetime=10.0, on_late_outcome=_Hook())

    async def parent() -> str:
        lifecycle.bind_serving_loop()
        try:
            return await parent_bound()
        finally:
            lifecycle.unbind_serving_loop()

    async def parent_bound() -> str:
        assert await one() == "tools"
        if parent_start_in_flight:
            with pytest.raises(McpStartPending):
                await start_listing(
                    _Run(3.0), title="svc", key="svc", wait=0.05, lifetime=10.0, on_late_outcome=_Hook()
                )
            assert len(_starts_on(asyncio.get_running_loop())) == 1
        read_fd, write_fd = os.pipe()
        with warnings.catch_warnings():
            # Fork warns in a process with threads; the child under test is the point.
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
        if pid == 0:  # pragma: no cover - the child exits through os._exit
            try:
                os.close(read_fd)

                async def bounded() -> Any:
                    return await asyncio.wait_for(one(), 5)

                try:
                    # The child runs a loop of its own: the parent's running loop is not the child's.
                    outcome = asyncio.run(bounded())
                except BaseException as exc:  # reported to the parent, which asserts on it
                    outcome = type(exc).__name__
                os.write(write_fd, str(outcome).encode())
            finally:
                os._exit(0)
        os.close(write_fd)

        def read() -> str:
            with os.fdopen(read_fd) as pipe:
                return pipe.read()

        reported = await asyncio.to_thread(read)
        await asyncio.to_thread(os.waitpid, pid, 0)
        return reported

    assert asyncio.run(parent()) == "tools"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is not available on this platform")
def test_a_forked_child_with_no_serving_loop_waits_its_start_out_on_its_jobs_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A long-lived forked child runs each job on one private loop that runs only while a job runs,
    # and the fork unbinds the parent's serving loop. A job that waits less than its start takes
    # still gets the start's outcome, so no start is left frozen on that loop between jobs, and a
    # later job's start of the same key is a fresh one, not a frozen one read as a timeout.
    lifecycle = _mixin()
    monkeypatch.setattr(instance, "_app", lifecycle)

    budget = 1.0

    async def job() -> Any:
        return await start_listing(
            _Run(0.3), title="svc", key="svc", wait=0.05, lifetime=budget, on_late_outcome=_Hook()
        )

    with owner_loop() as serving:
        bind_as_serving(lifecycle, serving)
        try:
            reported = _reported_by_a_forked_child(job, idle=budget + 0.2)
        finally:
            lifecycle.unbind_serving_loop()
    assert reported == "None | tools | tools"


def _reported_by_a_forked_child(job: Callable[[], Awaitable[Any]], *, idle: float) -> str:
    """Fork a child that runs ``job`` twice on one private loop, idle ``idle`` seconds after each.

    The child reports its bound serving loop and each job's outcome, or its error's type and text.
    """
    read_fd, write_fd = os.pipe()
    with warnings.catch_warnings():
        # Fork warns in a process with threads; the child under test is the point.
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:  # pragma: no cover - the child exits through os._exit
        try:
            os.close(read_fd)
            outcomes: list[str] = []
            jobs = asyncio.new_event_loop()
            for _ in range(2):
                try:
                    outcomes.append(str(jobs.run_until_complete(asyncio.wait_for(job(), 5))))
                except BaseException as exc:  # reported to the parent, which asserts on it
                    outcomes.append(f"{type(exc).__name__}: {exc}")
                # Idle between jobs, as a child waiting for its next one: its loop does not run.
                time.sleep(idle)
            os.write(write_fd, " | ".join([str(instance.bound_serving_loop()), *outcomes]).encode())
        finally:
            os._exit(0)
    os.close(write_fd)
    with os.fdopen(read_fd) as pipe:
        reported = pipe.read()
    os.waitpid(pid, 0)
    return reported


async def test_a_caller_with_no_serving_loop_cancelled_while_waiting_returns_once_its_start_has_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The start is a task of the caller's own loop: the cancel hands the hook, and the caller
    # stays until the start has ended, so the start never outlives the caller's await.
    monkeypatch.setattr(instance, "_app", None)
    run, hook = _Run(0.4), _Hook()
    waiting = asyncio.create_task(
        start_listing(run, title="svc", key="svc", wait=0.05, lifetime=10.0, on_late_outcome=hook)
    )
    await asyncio.sleep(0.1)
    waiting.cancel()
    await asyncio.sleep(0.05)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert run.finished.is_set()
    assert not run.cancelled.is_set()
    assert hook.outcomes == ["tools"]


def test_the_pending_text_names_both_budgets_and_only_the_start_loops_own_facts() -> None:
    assert str(McpStartPending("svc", 3.0, 15.0, start=StartId())) == (
        "MCP server 'svc' start still in progress after 3.0s; it continues up to the start budget 15.0s"
    )


@pytest.mark.usefixtures("serving_here")
async def test_a_start_and_its_late_outcome_hook_run_from_no_callers_context() -> None:
    seen: dict[str, str] = {}
    called = threading.Event()

    async def run() -> str:
        seen["start"] = _CALLER_SCOPE.get()
        await asyncio.sleep(0.3)
        return "tools"

    def hook(_start: StartId, _outcome: Any) -> None:
        seen["hook"] = _CALLER_SCOPE.get()
        called.set()

    async def caller() -> None:
        _CALLER_SCOPE.set("request-42 caller identity")
        with pytest.raises(McpStartPending):
            await start_listing(run, title="svc", key="svc", wait=0.05, lifetime=10.0, on_late_outcome=hook)

    await asyncio.create_task(caller())
    assert await asyncio.to_thread(called.wait, 5)
    assert seen == {"start": "<none>", "hook": "<none>"}


def test_the_outcome_text_is_listed_for_a_result_and_the_redacted_error_for_a_failure() -> None:
    assert mcp_start.outcome_text(["tool"]) == "listed"
    failure = ConnectionError("refused at https://alice:s3cret@example.test/mcp?token=abc")
    assert mcp_start.outcome_text(failure) == "ConnectionError: refused at https://***@example.test/mcp?token=***"


@pytest.mark.usefixtures("serving_here")
async def test_the_late_hook_is_handed_the_start_its_pending_names_and_each_start_is_its_own() -> None:
    handed: list[StartId] = []
    both = threading.Event()

    def hook(start: StartId, _outcome: Any) -> None:
        handed.append(start)
        if len(handed) == 2:
            both.set()

    with pytest.raises(McpStartPending) as first:
        await start_listing(_Run(0.2), title="svc", key="svc-a", wait=0.05, lifetime=10.0, on_late_outcome=hook)
    with pytest.raises(McpStartPending) as second:
        await start_listing(_Run(0.2), title="svc", key="svc-b", wait=0.05, lifetime=10.0, on_late_outcome=hook)
    assert await asyncio.to_thread(both.wait, 5)
    assert first.value.start is not second.value.start
    assert sorted(map(id, handed)) == sorted([id(first.value.start), id(second.value.start)])


class _CountedRun(_Run):
    """A :class:`_Run` that counts how many times it was started."""

    def __init__(self, seconds: float, **kwargs: Any) -> None:
        super().__init__(seconds, **kwargs)
        self.starts = 0

    async def __call__(self) -> Any:
        self.starts += 1
        return await super().__call__()


@pytest.mark.usefixtures("serving_here")
async def test_callers_with_one_key_share_one_start_and_its_result() -> None:
    first, second = _CountedRun(0.2), _CountedRun(0.2)
    hook = _Hook()
    both = await asyncio.gather(
        start_listing(first, title="svc", key="svc", wait=5.0, lifetime=10.0, on_late_outcome=hook),
        start_listing(second, title="svc", key="svc", wait=5.0, lifetime=10.0, on_late_outcome=hook),
    )
    assert both == ["tools", "tools"]
    assert (first.starts, second.starts) == (1, 0)
    assert hook.outcomes == []


@pytest.mark.usefixtures("serving_here")
async def test_a_joiner_past_its_wait_names_the_shared_start_and_its_outcome_reaches_one_hook_once() -> None:
    run = _CountedRun(0.3)
    launcher_hook, joiner_hook = _Hook(), _Hook()
    with pytest.raises(McpStartPending) as launched:
        await start_listing(run, title="svc", key="svc", wait=0.05, lifetime=10.0, on_late_outcome=launcher_hook)
    with pytest.raises(McpStartPending) as joined:
        await start_listing(
            _CountedRun(0.3), title="svc", key="svc", wait=0.05, lifetime=10.0, on_late_outcome=joiner_hook
        )
    assert joined.value.start is launched.value.start
    assert await asyncio.to_thread(launcher_hook.called.wait, 5)
    await asyncio.sleep(0.1)
    assert (launcher_hook.outcomes, joiner_hook.outcomes, run.starts) == (["tools"], [], 1)


@pytest.mark.usefixtures("serving_here")
async def test_a_joiner_inside_its_wait_gets_the_shared_start_failure() -> None:
    error = ConnectionError("refused")
    hook = _Hook()
    with pytest.raises(McpStartPending):
        await start_listing(
            _Run(0.2, error=error), title="svc", key="svc", wait=0.05, lifetime=10.0, on_late_outcome=hook
        )
    with pytest.raises(ConnectionError) as caught:
        await start_listing(_Run(0.0), title="svc", key="svc", wait=5.0, lifetime=10.0, on_late_outcome=hook)
    assert caught.value is error


@pytest.mark.usefixtures("serving_here")
async def test_callers_with_other_keys_each_start_their_own() -> None:
    first, second = _CountedRun(0.1), _CountedRun(0.1)
    hook = _Hook()
    await asyncio.gather(
        start_listing(first, title="svc", key="svc-a", wait=5.0, lifetime=10.0, on_late_outcome=hook),
        start_listing(second, title="svc", key="svc-b", wait=5.0, lifetime=10.0, on_late_outcome=hook),
    )
    assert (first.starts, second.starts) == (1, 1)


@pytest.mark.usefixtures("serving_here")
async def test_a_key_whose_start_ended_starts_again() -> None:
    run = _CountedRun(0.01)
    hook = _Hook()
    for _ in range(2):
        assert (
            await start_listing(run, title="svc", key="svc", wait=5.0, lifetime=10.0, on_late_outcome=hook) == "tools"
        )
    assert run.starts == 2


@pytest.mark.usefixtures("serving_here")
async def test_a_start_runs_on_the_callers_loop_as_the_rebuilds_own_work() -> None:
    # A start a re-importing rebuild asks for is a task of the loop that asked, and it
    # belongs to that rebuild: the fork gate counts its work as the rebuild's own.
    seen: dict[str, object] = {}

    async def run() -> str:
        seen["loop"] = asyncio.get_running_loop()
        with fork_gate._cond:
            seen["own_work"] = fork_gate._runs_own_work()
        return "tools"

    async with fork_gate.exclusive_async(timeout=5.0):
        assert await start_listing(run, title="svc", key="svc", wait=5.0, lifetime=10.0, on_late_outcome=_Hook()) == (
            "tools"
        )
    assert seen == {"loop": asyncio.get_running_loop(), "own_work": True}


def test_a_start_a_job_loop_launches_runs_on_the_serving_loop_and_reaches_a_later_caller_there(serving) -> None:
    # A caller on a job's own loop stops waiting, and that loop never runs again: the start
    # still ends on the serving loop, so a later caller there gets its outcome, not a pending.
    hook = _Hook()
    run = _Run(0.5)

    async def probe(wait: float) -> Any:
        return await start_listing(run, title="svc", key="probe", wait=wait, lifetime=10.0, on_late_outcome=hook)

    with job_loop() as job:
        with pytest.raises(McpStartPending):
            job.run(probe(0.01))
        assert asyncio.run_coroutine_threadsafe(probe(5.0), serving).result(10) == "tools"
        assert hook.called.wait(5)
    assert hook.outcomes == ["tools"]


def test_a_start_a_job_loop_asks_for_inside_a_rebuild_runs_on_the_serving_loop_as_its_own_work(serving) -> None:
    seen: dict[str, object] = {}

    async def run() -> str:
        seen["loop"] = asyncio.get_running_loop()
        with fork_gate._cond:
            seen["own_work"] = fork_gate._runs_own_work()
        return "tools"

    async def rebuild() -> Any:
        async with fork_gate.exclusive_async(timeout=5.0):
            return await start_listing(run, title="svc", key="svc", wait=5.0, lifetime=10.0, on_late_outcome=_Hook())

    with job_loop() as job:
        assert job.run(rebuild()) == "tools"
    assert seen == {"loop": serving, "own_work": True}


@pytest.fixture
def _guarded_forks() -> Iterator[None]:
    """Guard the process's forks for one test; the fork hooks stay registered but do nothing after it."""
    fork_gate.guard_forks(timeout=_GUARD_WAIT)
    yield
    fork_gate._fork_hooks.guard = None


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is not available on this platform")
@pytest.mark.usefixtures("_guarded_forks")
async def test_a_stdio_start_a_reimporting_reload_asks_for_forks_as_the_reloads_own(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A stdio server's start forks its child. Asked for inside a re-importing reload, that
    # fork is the reload's own: it neither waits on the reload's hold nor is reported as a
    # fork racing a module re-import.
    async def run() -> float:
        started = time.monotonic()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - runs in the child
            os._exit(0)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        return time.monotonic() - started

    async def reload_body() -> float:
        return await start_listing(run, title="svc", key="svc", wait=None, lifetime=60.0, on_late_outcome=_Hook())

    with caplog.at_level(logging.ERROR, logger="tai42_kit"):
        waited = await reload_gate.run(reload_body, reimports=True)

    assert waited < _PROMPT
    assert "fork gate: a fork" not in caplog.text
