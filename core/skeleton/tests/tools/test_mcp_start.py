"""The process-wide MCP start loop: a caller's wait never ends a server start; only the start budget does."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import threading
import warnings
from collections.abc import Iterator
from typing import Any

import pytest

from tai42_skeleton.tools import mcp_start
from tai42_skeleton.tools.mcp_start import McpStartPending, StartId, start_listing

_THREAD = "tai-mcp-start"

# A scope a caller sets around its wait, as a request sets its identity.
_CALLER_SCOPE: contextvars.ContextVar[str] = contextvars.ContextVar("caller_scope", default="<none>")


@pytest.fixture(autouse=True)
def _closed_start_loop() -> Iterator[None]:
    """Each test starts from no start loop and leaves none running."""
    yield
    asyncio.run(mcp_start.aclose())


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


def _start_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == _THREAD]


async def test_a_result_inside_the_wait_is_returned_and_the_hook_never_fires() -> None:
    hook = _Hook()
    assert (
        await start_listing(_Run(0.01), title="svc", key="svc", wait=5.0, lifetime=10.0, on_late_outcome=hook)
        == "tools"
    )
    await asyncio.sleep(0.05)
    assert hook.outcomes == []


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


async def test_a_start_that_outruns_the_start_budget_is_cancelled_and_raises_naming_the_budget() -> None:
    run, hook = _Run(30.0), _Hook()
    with pytest.raises(TimeoutError, match=r"^MCP server 'svc' did not start within the start budget 0\.2s$"):
        await start_listing(run, title="svc", key="svc", wait=None, lifetime=0.2, on_late_outcome=hook)
    assert run.cancelled.is_set()
    assert hook.outcomes == []


async def test_a_wait_as_long_as_the_start_budget_waits_for_the_start_itself() -> None:
    # Equal budgets never race into a pending answer: the caller sees the start's own end.
    run = _Run(30.0)
    with pytest.raises(TimeoutError, match="did not start within the start budget"):
        await start_listing(run, title="svc", key="svc", wait=0.2, lifetime=0.2, on_late_outcome=_Hook())
    assert run.cancelled.is_set()


async def test_a_late_start_that_outruns_the_start_budget_hands_the_budget_error_to_the_hook() -> None:
    run, hook = _Run(30.0), _Hook()
    with pytest.raises(McpStartPending):
        await start_listing(run, title="svc", key="svc", wait=0.05, lifetime=0.3, on_late_outcome=hook)
    assert await asyncio.to_thread(hook.called.wait, 5)
    assert run.cancelled.is_set()
    [outcome] = hook.outcomes
    assert isinstance(outcome, TimeoutError)
    assert str(outcome) == "MCP server 'svc' did not start within the start budget 0.3s"


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


def test_a_late_outcome_after_the_callers_loop_closed_raises_nothing(caplog: pytest.LogCaptureFixture) -> None:
    # The caller's loop is a throwaway one, gone before the start completes.
    run, hook = _Run(0.3), _Hook()

    async def caller() -> None:
        with pytest.raises(McpStartPending):
            await start_listing(run, title="svc", key="svc", wait=0.05, lifetime=10.0, on_late_outcome=hook)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(caller())
        assert hook.called.wait(5)
        threading.Event().wait(0.1)
    assert hook.outcomes == ["tools"]
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


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


async def test_aclose_cancels_every_pending_start_and_joins_the_start_thread(caplog: pytest.LogCaptureFixture) -> None:
    run, hook = _Run(30.0), _Hook()
    with pytest.raises(McpStartPending):
        await start_listing(run, title="svc", key="svc", wait=0.05, lifetime=60.0, on_late_outcome=hook)
    assert len(_start_threads()) == 1
    with caplog.at_level(logging.INFO, logger="tai42_skeleton.tools.mcp_start"):
        await mcp_start.aclose()
    assert run.cancelled.is_set()
    assert _start_threads() == []
    assert hook.outcomes == []
    assert any("'svc'" in r.getMessage() and "cancelled" in r.getMessage() for r in caplog.records)


async def test_a_caller_waiting_when_the_start_loop_closes_gets_a_loud_error() -> None:
    run = _Run(30.0)
    waiting = asyncio.create_task(
        start_listing(run, title="svc", key="svc", wait=None, lifetime=60.0, on_late_outcome=_Hook())
    )
    await asyncio.sleep(0.05)
    await mcp_start.aclose()
    with pytest.raises(RuntimeError, match=r"^MCP server 'svc' start cancelled: the MCP start loop closed$"):
        await waiting


async def test_the_start_loop_starts_again_after_it_closed() -> None:
    hook = _Hook()
    assert await start_listing(_Run(0.01), title="a", key="a", wait=5.0, lifetime=10.0, on_late_outcome=hook) == "tools"
    await mcp_start.aclose()
    assert await start_listing(_Run(0.01), title="b", key="b", wait=5.0, lifetime=10.0, on_late_outcome=hook) == "tools"
    assert len(_start_threads()) == 1


@pytest.mark.parametrize("parent_start_in_flight", [False, True], ids=["parent idle", "parent start in flight"])
def test_a_forked_child_runs_its_starts_on_its_own_start_loop(parent_start_in_flight: bool) -> None:
    # The parent's start thread, and so any start of it in flight, does not exist in a forked child.
    async def one() -> Any:
        return await start_listing(_Run(0.01), title="svc", key="svc", wait=5.0, lifetime=10.0, on_late_outcome=_Hook())

    async def in_flight() -> None:
        with pytest.raises(McpStartPending):
            await start_listing(_Run(3.0), title="svc", key="svc", wait=0.05, lifetime=10.0, on_late_outcome=_Hook())

    assert asyncio.run(one()) == "tools"
    if parent_start_in_flight:
        asyncio.run(in_flight())
    read_fd, write_fd = os.pipe()
    with warnings.catch_warnings():
        # The parent runs the start thread, so fork warns; the child under test is the point.
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:  # pragma: no cover - the child exits through os._exit
        try:
            os.close(read_fd)

            async def bounded() -> Any:
                return await asyncio.wait_for(one(), 5)

            try:
                outcome = asyncio.run(bounded())
            except BaseException as exc:  # reported to the parent, which asserts on it
                outcome = type(exc).__name__
            os.write(write_fd, str(outcome).encode())
        finally:
            os._exit(0)
    os.close(write_fd)
    with os.fdopen(read_fd) as pipe:
        reported = pipe.read()
    os.waitpid(pid, 0)
    assert reported == "tools"


def test_the_pending_text_names_both_budgets_and_only_the_start_loops_own_facts() -> None:
    assert str(McpStartPending("svc", 3.0, 15.0, start=StartId())) == (
        "MCP server 'svc' start still in progress after 3.0s; it continues up to the start budget 15.0s"
    )


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


async def test_callers_with_other_keys_each_start_their_own() -> None:
    first, second = _CountedRun(0.1), _CountedRun(0.1)
    hook = _Hook()
    await asyncio.gather(
        start_listing(first, title="svc", key="svc-a", wait=5.0, lifetime=10.0, on_late_outcome=hook),
        start_listing(second, title="svc", key="svc-b", wait=5.0, lifetime=10.0, on_late_outcome=hook),
    )
    assert (first.starts, second.starts) == (1, 1)


async def test_a_key_whose_start_ended_starts_again() -> None:
    run = _CountedRun(0.01)
    hook = _Hook()
    for _ in range(2):
        assert (
            await start_listing(run, title="svc", key="svc", wait=5.0, lifetime=10.0, on_late_outcome=hook) == "tools"
        )
    assert run.starts == 2
