"""How a job's private event loop ends: the kit's release, its tasks ended, its async generators closed, the close."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from tai42_kit.backend import PrivateLoop, private_loop
from tai42_kit.clients import PooledClient
from tai42_kit.loop_bound import LoopRegistryMap, ResourceRegistry

from .conftest import _LOGGER, _sleep_then


def test_a_private_loop_runs_job_after_job_and_its_close_releases_the_kit_s_resources_on_it_then_closes_it(
    private: PrivateLoop, steps: list[tuple[str, asyncio.AbstractEventLoop]]
) -> None:
    assert private.run(_sleep_then(0, 1)) == 1
    assert private.run(_sleep_then(0, 2)) == 2
    assert steps == []

    private.close()

    assert steps == [("release", private.loop)]
    assert private.closed


def test_closing_a_closed_private_loop_does_nothing(
    private: PrivateLoop, steps: list[tuple[str, asyncio.AbstractEventLoop]]
) -> None:
    private.close()
    private.close()

    assert steps == [("release", private.loop)]


def test_a_failed_release_raises_from_the_close_and_the_loop_still_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fails() -> None:
        raise RuntimeError("registry close failed")

    monkeypatch.setattr(private_loop, "release_loop_bound_resources", _fails)
    private = PrivateLoop()

    with pytest.raises(RuntimeError, match="registry close failed"):
        private.close()
    assert private.closed


def test_a_private_loop_s_close_does_not_wait_on_the_threads_its_jobs_started(
    steps: list[tuple[str, asyncio.AbstractEventLoop]],
) -> None:
    """A thread cannot be cancelled: the loop closes at once, and the thread ends on its own."""
    leave = threading.Event()
    private = PrivateLoop()

    async def _starts_a_thread() -> None:
        asyncio.get_running_loop().run_in_executor(None, leave.wait, 30)
        await asyncio.sleep(0)

    private.run(_starts_a_thread())
    started = time.monotonic()
    try:
        private.close()
        elapsed = time.monotonic() - started
    finally:
        leave.set()

    assert private.closed
    assert [step for step, _ in steps] == ["release"]
    assert elapsed < 1, f"the close waited {elapsed:.1f}s on the job's thread"


def test_a_private_loop_s_close_closes_the_async_generators_left_suspended_on_it_after_the_release(
    private: PrivateLoop, steps: list[tuple[str, asyncio.AbstractEventLoop]]
) -> None:
    kept: list[AsyncIterator[int]] = []

    async def _counts() -> AsyncIterator[int]:
        try:
            yield 1
            yield 2
        finally:
            steps.append(("generator closed", asyncio.get_running_loop()))

    async def _leaves_a_generator_suspended() -> int:
        generator = _counts()
        kept.append(generator)
        return await anext(generator)

    assert private.run(_leaves_a_generator_suspended()) == 1
    private.close()

    assert steps == [("release", private.loop), ("generator closed", private.loop)]


def test_a_private_loop_s_close_cancels_a_task_left_on_it_and_runs_it_to_its_end_after_the_kit_release(
    private: PrivateLoop, steps: list[tuple[str, asyncio.AbstractEventLoop]]
) -> None:
    kept: list[asyncio.Task[None]] = []

    async def _background() -> None:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            steps.append(("background cleanup", asyncio.get_running_loop()))
            raise

    async def _starts_a_background_task() -> None:
        kept.append(asyncio.get_running_loop().create_task(_background()))
        await asyncio.sleep(0)

    private.run(_starts_a_background_task())
    private.close()

    assert steps == [("release", private.loop), ("background cleanup", private.loop)]
    [background] = kept
    assert background.cancelled()


def test_an_error_a_task_left_on_the_loop_ends_with_after_close_cancels_it_is_logged(
    private: PrivateLoop, steps: list[tuple[str, asyncio.AbstractEventLoop]], caplog: pytest.LogCaptureFixture
) -> None:
    kept: list[asyncio.Task[None]] = []

    async def _background() -> None:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise OSError("background cleanup failed") from None

    async def _starts_a_background_task() -> None:
        kept.append(asyncio.get_running_loop().create_task(_background()))
        await asyncio.sleep(0)

    private.run(_starts_a_background_task())
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        private.close()

    [record] = [record for record in caplog.records if record.name == _LOGGER]
    assert record.levelno == logging.ERROR
    assert "after the loop's close cancelled it" in record.getMessage()
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], OSError)
    assert str(record.exc_info[1]) == "background cleanup failed"
    assert [step for step, _ in steps] == ["release"]


def test_a_private_loop_s_close_ends_a_task_a_task_s_cleanup_starts(
    private: PrivateLoop, steps: list[tuple[str, asyncio.AbstractEventLoop]]
) -> None:
    started: list[asyncio.Task[None]] = []

    async def _flush() -> None:
        try:
            await asyncio.sleep(5)
        finally:
            steps.append(("flush ended", asyncio.get_running_loop()))

    async def _background() -> None:
        try:
            await asyncio.sleep(5)
        finally:
            started.append(asyncio.get_running_loop().create_task(_flush()))

    async def _starts_a_background_task() -> None:
        asyncio.get_running_loop().create_task(_background())
        await asyncio.sleep(0)

    private.run(_starts_a_background_task())
    private.close()

    assert steps == [("release", private.loop), ("flush ended", private.loop)]
    [flush] = started
    assert flush.cancelled()


def test_a_private_loop_s_close_ends_tasks_round_after_round_and_those_a_generator_s_close_makes_after_the_release(
    private: PrivateLoop, steps: list[tuple[str, asyncio.AbstractEventLoop]]
) -> None:
    """A task's cleanup starts a task whose own cleanup starts one more; a generator's close starts one too."""
    kept: list[AsyncIterator[int]] = []

    async def _flush(name: str, then: str | None = None) -> None:
        try:
            await asyncio.sleep(5)
        finally:
            steps.append((f"{name} ended", asyncio.get_running_loop()))
            if then is not None:
                asyncio.get_running_loop().create_task(_flush(then))

    async def _generator() -> AsyncIterator[int]:
        try:
            yield 1
        finally:
            asyncio.get_running_loop().create_task(_flush("made by the generator's close"))

    async def _background() -> None:
        try:
            await asyncio.sleep(5)
        finally:
            asyncio.get_running_loop().create_task(_flush("first", then="second"))

    async def _leaves_a_generator_and_a_task() -> None:
        generator = _generator()
        kept.append(generator)
        await anext(generator)
        asyncio.get_running_loop().create_task(_background())
        await asyncio.sleep(0)

    private.run(_leaves_a_generator_and_a_task())
    private.close()

    assert [step for step, _ in steps] == [
        "release",
        "first ended",
        "second ended",
        "made by the generator's close ended",
    ]


def test_a_private_loop_s_close_ends_the_work_a_thread_hands_it_while_the_release_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The thread outlived its job; its hand-off is cancelled before the loop closes, so the thread's wait ends."""
    release_runs, handed = threading.Event(), threading.Event()
    outcome: list[BaseException | object] = []

    async def _release() -> None:
        release_runs.set()
        await asyncio.to_thread(handed.wait, 5)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(private_loop, "release_loop_bound_resources", _release)
    private = PrivateLoop()

    def _hands_the_loop_work() -> None:
        release_runs.wait(5)
        future = asyncio.run_coroutine_threadsafe(asyncio.sleep(30), private.loop)
        handed.set()
        try:
            outcome.append(future.result(5))
        except BaseException as exc:
            outcome.append(exc)

    async def _starts_a_thread() -> None:
        asyncio.get_running_loop().run_in_executor(None, _hands_the_loop_work)
        await asyncio.sleep(0)

    private.run(_starts_a_thread())
    private.close()
    deadline = time.monotonic() + 3
    while not outcome and time.monotonic() < deadline:
        time.sleep(0.01)

    [ended] = outcome or [None]
    assert isinstance(ended, concurrent.futures.CancelledError), f"the thread's wait did not end: {ended!r}"
    assert private.closed


class _WorkerPool:
    """A connection pool shaped like psycopg's: it runs worker tasks of its own, and its close awaits them."""

    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self.workers = [asyncio.get_running_loop().create_task(self._work()) for _ in range(2)]
        self.closed = False

    async def _work(self) -> None:
        await self._stop.wait()

    async def close(self) -> None:
        self._stop.set()
        await asyncio.gather(*self.workers)
        self.closed = True


def test_a_private_loop_s_close_releases_a_pooled_client_whose_close_awaits_its_own_tasks() -> None:
    """The release runs while the pool's own workers are alive, so its close awaits them to their end."""
    pools: list[_WorkerPool] = []

    class _WorkerPoolClient(PooledClient[_WorkerPool]):
        async def _create(self, **kwargs: object) -> _WorkerPool:
            pool = _WorkerPool()
            pools.append(pool)
            return pool

        async def _close(self, client: _WorkerPool) -> None:
            await client.close()

    async def _uses_the_pool() -> None:
        async with _WorkerPoolClient().current(url="probe"):
            pass

    private = PrivateLoop()
    private.run(_uses_the_pool())
    private.close()

    [pool] = pools
    assert pool.closed
    assert all(worker.done() and not worker.cancelled() for worker in pool.workers)
    assert private.closed


def test_a_private_loop_s_close_ends_a_task_the_release_leaves_running(monkeypatch: pytest.MonkeyPatch) -> None:
    left: list[asyncio.Task[None]] = []

    async def _release() -> None:
        left.append(asyncio.get_running_loop().create_task(asyncio.sleep(30)))

    monkeypatch.setattr(private_loop, "release_loop_bound_resources", _release)
    private = PrivateLoop()
    private.close()

    [task] = left
    assert task.cancelled()
    assert private.closed


def test_a_failed_release_raises_once_the_tasks_left_and_the_generators_have_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ended: list[str] = []
    kept: list[AsyncIterator[int]] = []

    async def _fails() -> None:
        raise RuntimeError("registry close failed")

    async def _counts() -> AsyncIterator[int]:
        try:
            yield 1
        finally:
            ended.append("generator closed")

    async def _background() -> None:
        try:
            await asyncio.sleep(30)
        finally:
            ended.append("background ended")

    async def _leaves_a_generator_and_a_task() -> None:
        generator = _counts()
        kept.append(generator)
        await anext(generator)
        asyncio.get_running_loop().create_task(_background())
        await asyncio.sleep(0)

    monkeypatch.setattr(private_loop, "release_loop_bound_resources", _fails)
    private = PrivateLoop()
    private.run(_leaves_a_generator_and_a_task())

    with pytest.raises(RuntimeError, match="registry close failed"):
        private.close()
    assert ended == ["background ended", "generator closed"]
    assert private.closed


def test_a_task_s_cleanup_during_the_close_is_refused_a_new_pooled_client_and_registry_and_nothing_stays_open() -> None:
    """Once the loop's close has begun, a cleanup that leases a client or a registry raises; none is opened."""
    opened: list[str] = []
    closed: list[str] = []
    refusals: list[BaseException] = []
    registries: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry)

    class _ProbeClient(PooledClient[str]):
        async def _create(self, **kwargs: object) -> str:
            opened.append("client")
            return "client"

        async def _close(self, client: str) -> None:
            closed.append(client)

    async def _lease_a_client() -> None:
        async with _ProbeClient().current(url="probe"):
            pass

    async def _open_a_registry_resource() -> None:
        async def _factory() -> tuple[str, Callable[[], Awaitable[None]]]:
            opened.append("registry resource")

            async def _close() -> None:
                closed.append("registry resource")

            return "registry resource", _close

        await registries.current()._get_or_init_resource("probe", _factory)

    async def _background() -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            for lease in (_lease_a_client, _open_a_registry_resource):
                try:
                    await lease()
                except RuntimeError as refused:
                    refusals.append(refused)
            raise

    async def _uses_both_then_leaves_a_task() -> None:
        await _lease_a_client()
        await _open_a_registry_resource()
        asyncio.get_running_loop().create_task(_background())
        await asyncio.sleep(0)

    private = PrivateLoop()
    private.run(_uses_both_then_leaves_a_task())
    private.close()

    assert opened == ["client", "registry resource"]
    assert sorted(closed) == ["client", "registry resource"]
    closing = "the loop is closing, its loop-bound resources are being released"
    assert [str(refused).split(" on event loop ")[0] for refused in refusals] == [
        "_ProbeClient cannot lease a client",
        "LoopRegistryMap cannot serve a registry",
    ]
    assert all(str(refused).endswith(closing) for refused in refusals)
    assert private.closed


def test_the_close_marks_the_loop_closing_before_the_release_so_a_lease_during_the_release_is_refused() -> None:
    """A client's close that leases another pooled client runs inside the release; the lease is refused."""
    opened: list[str] = []
    closed: list[str] = []
    refusals: list[RuntimeError] = []

    class _InnerClient(PooledClient[str]):
        async def _create(self, **kwargs: object) -> str:
            opened.append("inner")
            return "inner"

        async def _close(self, client: str) -> None:
            closed.append(client)

    class _OuterClient(PooledClient[str]):
        async def _create(self, **kwargs: object) -> str:
            opened.append("outer")
            return "outer"

        async def _close(self, client: str) -> None:
            closed.append(client)
            try:
                async with _InnerClient().current(url="probe"):
                    pass
            except RuntimeError as refused:
                refusals.append(refused)

    async def _uses_the_outer_client() -> None:
        async with _OuterClient().current(url="probe"):
            pass

    private = PrivateLoop()
    private.run(_uses_the_outer_client())
    private.close()

    assert opened == ["outer"]
    assert closed == opened
    [refused] = refusals
    assert str(refused).startswith("_InnerClient cannot lease a client on event loop ")
    assert str(refused).endswith("the loop is closing, its loop-bound resources are being released")
    assert private.closed


def test_a_close_refused_while_the_loop_runs_a_job_leaves_the_loop_leasing() -> None:
    """A close called while another thread runs the loop raises and marks nothing: the job still leases."""
    started = threading.Event()
    go_on = threading.Event()
    leased: list[str] = []
    jobs: list[str] = []

    class _ProbeClient(PooledClient[str]):
        async def _create(self, **kwargs: object) -> str:
            return "client"

        async def _close(self, client: str) -> None:
            pass

    async def _leases_once_the_close_failed() -> str:
        started.set()
        while not go_on.is_set():
            await asyncio.sleep(0.01)
        async with _ProbeClient().current(url="probe") as client:
            leased.append(client)
        return "done"

    private = PrivateLoop()
    job = threading.Thread(target=lambda: jobs.append(private.run(_leases_once_the_close_failed())))
    job.start()
    assert started.wait(5)
    try:
        with pytest.raises(RuntimeError, match="running"):
            private.close()
    finally:
        go_on.set()
        job.join(5)

    assert jobs == ["done"]
    assert leased == ["client"]
    assert not private.closed
    private.close()
    assert private.closed
