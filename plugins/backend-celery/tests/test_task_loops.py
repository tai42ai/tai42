"""The private event loops a task's jobs run their async work on, one per job running at once.

How one such loop runs a job and ends is the kit's :class:`~tai42_kit.backend.private_loop.PrivateLoop`,
tested with it; here, how the task keeps, serves, retires and closes its loops.
"""

from __future__ import annotations

import asyncio
import errno
import inspect
import threading
from collections.abc import Callable
from typing import Any, cast

import pytest
from tai42_kit.backend import private_loop
from tai42_kit.backend.private_loop import Task

from tai42_backend_celery.core.task_loops import PrivateLoops


async def _sleep_then(seconds: float, value: object = None) -> object:
    await asyncio.sleep(seconds)
    return value


def test_a_private_loop_runs_a_job_and_serves_the_next(loop_releases) -> None:
    loops = PrivateLoops()

    assert loops.run(_sleep_then(0, 1)) == 1
    assert loops.run(_sleep_then(0, 2)) == 2

    assert loops.count == 1
    loops.close()
    assert loop_releases.calls == 1


async def _running_loop() -> asyncio.AbstractEventLoop:
    return asyncio.get_running_loop()


def _two_jobs_at_once(loops: PrivateLoops) -> tuple[asyncio.AbstractEventLoop, asyncio.AbstractEventLoop]:
    """Run two jobs on ``loops`` at the same time, each from a thread of its own; return the loop each ran on."""
    both_run = threading.Barrier(2)

    async def _waits_for_the_other() -> asyncio.AbstractEventLoop:
        await asyncio.to_thread(both_run.wait, 5)
        return asyncio.get_running_loop()

    ran: list[asyncio.AbstractEventLoop] = []
    jobs = [threading.Thread(target=lambda: ran.append(loops.run(_waits_for_the_other()))) for _ in range(2)]
    for job in jobs:
        job.start()
    for job in jobs:
        job.join(5)
    first, second = ran
    return first, second


def test_a_free_private_loop_serves_the_next_job_and_close_shuts_it_down(loop_releases) -> None:
    loops = PrivateLoops()
    first = loops.run(_running_loop())
    second = loops.run(_running_loop())
    assert second is first

    loops.close()

    assert first.is_closed()
    assert loop_releases.calls == 1
    rebuilt = loops.run(_running_loop())
    assert rebuilt is not first
    assert not rebuilt.is_closed()
    loops.close()


def test_jobs_at_once_each_hold_a_private_loop_of_their_own(loop_releases) -> None:
    loops = PrivateLoops()
    first, second = _two_jobs_at_once(loops)
    assert second is not first
    assert loops.run(_running_loop()) in (first, second)

    assert loops.count == 2
    loops.close()
    assert first.is_closed()
    assert second.is_closed()
    assert loop_releases.calls == 2


def test_a_closed_free_private_loop_is_replaced(stub_app) -> None:
    loops = PrivateLoops()
    first = loops.run(_running_loop())
    first.close()

    replaced = loops.run(_running_loop())
    assert replaced is not first
    assert not replaced.is_closed()

    assert loops.count == 1
    loops.close()


def test_close_releases_the_kits_loop_bound_resources_on_each_loop_before_closing_it(loop_releases) -> None:
    loops = PrivateLoops()
    first, second = _two_jobs_at_once(loops)

    loops.close()

    assert sorted(map(id, loop_releases.loops)) == sorted(map(id, [first, second]))
    assert first.is_closed()
    assert second.is_closed()


def test_close_closes_every_private_loop_and_raises_each_failed_release(monkeypatch: pytest.MonkeyPatch) -> None:
    released_on: list[tuple[asyncio.AbstractEventLoop, bool]] = []

    async def _fail() -> None:
        loop = asyncio.get_running_loop()
        released_on.append((loop, loop.is_closed()))
        raise RuntimeError("registry close failed")

    loops = PrivateLoops()
    first, second = _two_jobs_at_once(loops)
    monkeypatch.setattr(private_loop, "release_loop_bound_resources", _fail)

    with pytest.raises(ExceptionGroup) as raised:
        loops.close()

    assert [str(failure) for failure in raised.value.exceptions] == ["registry close failed", "registry close failed"]
    assert sorted(id(loop) for loop, _ in released_on) == sorted(map(id, [first, second]))
    assert [closed for _, closed in released_on] == [False, False]
    assert first.is_closed()
    assert second.is_closed()
    assert loops.count == 0


def test_a_job_for_which_no_loop_could_be_taken_closes_its_coroutine(stub_app, monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_loop() -> None:
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(asyncio, "new_event_loop", _no_loop)
    unrun = _sleep_then(0, 1)

    with pytest.raises(OSError, match="Too many open files"):
        PrivateLoops().run(unrun)
    assert inspect.getcoroutinestate(unrun) == inspect.CORO_CLOSED


def _a_second_signal_in(
    monkeypatch: pytest.MonkeyPatch, owner: type, name: str, *, after: BaseException, interrupt: BaseException
) -> Callable[[], None]:
    """Make ``owner.name`` raise ``interrupt`` once, on its first call after the returned callback raised ``after``."""
    real = getattr(owner, name)
    happened: set[str] = set()

    def _patched(self: object, *args: Any, **kwargs: Any) -> object:
        if "after" in happened and "interrupt" not in happened:
            happened.add("interrupt")
            raise interrupt
        return real(self, *args, **kwargs)

    def _raises_after() -> None:
        happened.add("after")
        raise after

    monkeypatch.setattr(owner, name, _patched)
    return _raises_after


def test_a_loop_a_job_s_work_could_not_be_ended_on_leaves_the_open_ones_and_the_next_job_makes_one_anew(
    stub_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A further exception lands in the cancel: the job raises it and its loop closes, leaving the task's loops."""
    first, again = KeyboardInterrupt("first signal"), KeyboardInterrupt("second signal")
    raises_the_first = _a_second_signal_in(monkeypatch, Task, "cancel", after=first, interrupt=again)
    loops = PrivateLoops()
    loops_of_the_job: list[asyncio.AbstractEventLoop] = []

    async def _interrupted() -> None:
        loops_of_the_job.append(asyncio.get_running_loop())
        asyncio.get_running_loop().call_soon(raises_the_first)
        await asyncio.sleep(5)

    with pytest.raises(KeyboardInterrupt) as raised:
        loops.run(_interrupted())

    assert raised.value is again
    [retired] = loops_of_the_job
    assert retired.is_closed()
    assert loops.count == 0

    async def _loop_of_the_job() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    assert loops.run(_loop_of_the_job()) is not retired
    assert loops.count == 1
    loops.close()


def _breaks_the_loop_s_wait() -> None:
    """Make the running loop's wait for its I/O fail on every pass, as a selector whose descriptor is gone."""

    def _select(timeout: float | None = None) -> object:
        raise OSError(errno.EBADF, "Bad file descriptor")

    cast(Any, asyncio.get_running_loop())._selector.select = _select


async def _breaks_its_loop() -> str:
    _breaks_the_loop_s_wait()
    await asyncio.sleep(0.05)
    return "ok"


def test_a_private_loop_that_fails_on_every_run_fails_its_job_at_once_and_is_retired(stub_app) -> None:
    loops = PrivateLoops()
    outcomes: dict[str, object] = {}

    def _job() -> None:
        try:
            outcomes["broken"] = loops.run(_breaks_its_loop())
        except BaseException as exc:
            outcomes["broken"] = exc

    job = threading.Thread(target=_job, daemon=True)
    job.start()
    job.join(5)

    raised = outcomes.get("broken")
    assert isinstance(raised, OSError), f"the job never ended: {raised!r}"
    assert raised.errno == errno.EBADF
    assert loops.count == 0
    assert loops.run(_sleep_then(0, "ok")) == "ok"
    loops.close()


def test_a_private_job_run_inside_another_job_s_loop_retires_the_inner_loop(stub_app) -> None:
    """The inner job takes a loop of its own, which cannot run while the outer one does: it raises and is retired."""
    loops = PrivateLoops()
    inner = _sleep_then(0, "inner")

    async def _runs_a_job_inside() -> tuple[BaseException | None, bool]:
        try:
            loops.run(inner)
        except BaseException as exc:
            return exc, _current_loop() is asyncio.get_running_loop()
        return None, _current_loop() is asyncio.get_running_loop()

    outcomes: dict[str, object] = {}
    outer = threading.Thread(target=lambda: outcomes.update(outer=loops.run(_runs_a_job_inside())), daemon=True)
    outer.start()
    outer.join(5)
    inner.close()

    raised, outer_is_current = cast(tuple[object, object], outcomes.get("outer", (None, False)))
    assert isinstance(raised, RuntimeError), f"the outer job never ended: {raised!r}"
    assert str(raised) == "Cannot run the event loop while another loop is running"
    assert outer_is_current is True, "the outer job's loop is no longer the thread's current loop"
    assert loops.count == 1
    loops.close()


def _current_loop() -> asyncio.AbstractEventLoop | None:
    """The thread's current event loop, the one ``asyncio.set_event_loop`` set, or ``None`` when none is."""
    try:
        return asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        return None
