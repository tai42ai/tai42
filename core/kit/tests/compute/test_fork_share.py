"""A process's compute pool shared with the children it forks.

Every test shares inside a forked helper process, so the test session's own process never
shares its pool.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import multiprocessing
import operator
import os
import select
import signal
import socket
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from tai42_kit.compute import (
    ComputePool,
    ComputePoolClient,
    ComputePoolUnavailableError,
    WorkerTraceback,
    compute_pool,
    share_compute_pool,
    stop_compute_pool,
)
from tai42_kit.compute import fork_share as fork_share_module
from tai42_kit.compute import process_pool as process_pool_module
from tai42_kit.compute.wire_sockets import wire_sockets

from .descriptor_limit import at_descriptor_limit
from .forked import fork_child, fork_daemonic_child, in_forked_process, read_report, report_to, wait_exit

pytestmark = pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")

_NO_RESULT = (
    "the process holding the compute pool gave no result for time.sleep: "
    "it ended, or it could not take or serve the job"
)


@pytest.fixture(autouse=True)
def _one_worker(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("COMPUTE_WORKERS", "1")
    stop_compute_pool()  # the helper process inherits no running pool
    yield
    stop_compute_pool()


def _worker_pid() -> int:
    return asyncio.run(compute_pool().run(os.getpid, timeout=30))


def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def _sharing(body: object) -> object:
    """Share this process's pool, run ``body`` and stop the pool, in a forked helper process."""

    def helper() -> object:
        share_compute_pool()
        try:
            return body()  # type: ignore[operator]
        finally:
            stop_compute_pool()

    return in_forked_process(helper)


def test_a_daemonic_multiprocessing_child_of_a_sharing_process_runs_its_job_in_the_parents_pool() -> None:
    def body() -> object:
        parent_worker = _worker_pid()

        def child() -> object:
            return {
                "daemon": multiprocessing.current_process().daemon,
                "reaches_the_parent": isinstance(compute_pool(), ComputePoolClient),
                "worker": _worker_pid(),
                "sum": asyncio.run(compute_pool().run(operator.add, 20, 22)),
            }

        return {"parent_worker": parent_worker, "child": fork_daemonic_child(child).result()}

    seen = _sharing(body)

    assert seen == {
        "parent_worker": seen["parent_worker"],  # type: ignore[index]
        "child": {"daemon": True, "reaches_the_parent": True, "worker": seen["parent_worker"], "sum": 42},  # type: ignore[index]
    }


def test_a_forked_child_of_a_sharing_process_starts_no_workers_of_its_own() -> None:
    def body() -> object:
        parent_worker = _worker_pid()

        def child() -> object:
            worker = _worker_pid()
            return {
                "worker": worker,
                "own_pool": process_pool_module._pool is not None,
                "children": [process.pid for process in multiprocessing.active_children()],
            }

        return {"parent_worker": parent_worker, "child": fork_child(child).result()}

    seen = _sharing(body)

    assert seen["child"] == {"worker": seen["parent_worker"], "own_pool": False, "children": []}  # type: ignore[index]


def test_a_job_past_its_timeout_in_a_child_raises_timeout_error_and_the_parents_pool_replaces_the_worker() -> None:
    def body() -> object:
        first_worker = _worker_pid()

        def child() -> object:
            try:
                asyncio.run(compute_pool().run(time.sleep, 30, timeout=0.3))
            except TimeoutError as exc:
                raised = str(exc)
            else:
                raised = "nothing"
            return {"raised": raised, "next": _worker_pid()}

        seen = fork_child(child).result()
        return {"first_worker": first_worker, "first_worker_gone": _gone_within(first_worker, 5), **seen}  # type: ignore[dict-item]

    seen = _sharing(body)

    assert seen == {
        "first_worker": seen["first_worker"],  # type: ignore[index]
        "first_worker_gone": True,
        "raised": "compute job time.sleep exceeded its 0.3s timeout; its worker was terminated",
        "next": seen["next"],  # type: ignore[index]
    }
    assert seen["next"] != seen["first_worker"]  # type: ignore[index]


def test_cancelling_a_childs_job_terminates_its_worker_in_the_parent() -> None:
    def body() -> object:
        first_worker = _worker_pid()

        def child() -> object:
            async def cancelled() -> bool:
                job = asyncio.ensure_future(compute_pool().run(time.sleep, 30))
                await asyncio.sleep(0.3)
                job.cancel()
                try:
                    await job
                except asyncio.CancelledError:
                    return True
                return False

            return {"cancelled": asyncio.run(cancelled()), "next": _worker_pid()}

        seen = fork_child(child).result()
        return {"first_worker": first_worker, "first_worker_gone": _gone_within(first_worker, 5), **seen}  # type: ignore[dict-item]

    seen = _sharing(body)

    assert seen["cancelled"] is True  # type: ignore[index]
    assert seen["first_worker_gone"] is True  # type: ignore[index]
    assert seen["next"] != seen["first_worker"]  # type: ignore[index]


def test_cancelling_a_childs_job_terminates_its_worker_while_a_grandchild_forked_during_the_job_lives() -> None:
    def body() -> object:
        first_worker = _worker_pid()

        def child() -> object:
            async def cancelled_with_a_grandchild() -> bool:
                job = asyncio.ensure_future(compute_pool().run(time.sleep, 30))
                await asyncio.sleep(0.3)
                grandchild = fork_child(lambda: time.sleep(5))
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
                gone = _gone_within(first_worker, 3)
                grandchild.result()
                return gone

            return asyncio.run(cancelled_with_a_grandchild())

        return fork_child(child).result()

    assert _sharing(body) is True


def test_what_the_job_raises_is_raised_in_the_child_as_itself_with_the_workers_traceback_as_its_cause() -> None:
    def body() -> object:
        def child() -> object:
            try:
                asyncio.run(compute_pool().run(operator.truediv, 1, 0))
            except ZeroDivisionError as exc:
                return {
                    "cause": type(exc.__cause__).__name__,
                    "traceback_names_it": "ZeroDivisionError" in str(exc.__cause__),
                }
            return {"raised": "nothing"}

        return fork_child(child).result()

    assert _sharing(body) == {"cause": WorkerTraceback.__name__, "traceback_names_it": True}


def _ends_its_parent_mid_job() -> object:
    """In a child of a sharing process: end the parent during a job, then run another job."""

    async def jobs() -> list[str]:
        loop = asyncio.get_running_loop()
        parent = os.getppid()
        loop.call_later(0.3, os.kill, parent, signal.SIGKILL)
        seen = []
        started = time.monotonic()
        for job in ((time.sleep, 3), (operator.add, 1, 1)):
            try:
                await compute_pool().run(*job, timeout=10)  # type: ignore[arg-type]
            except ComputePoolUnavailableError as exc:
                seen.append(str(exc))
            else:
                seen.append("ran")
            # A killed process closes its sockets before its children are reparented: the next
            # job is sent once the parent has closed them all.
            while os.getppid() == parent and time.monotonic() - started < 2:
                await asyncio.sleep(0.01)
        seen.append("answered at once" if time.monotonic() - started < 2.5 else "answered late")
        return seen

    return asyncio.run(jobs())


def test_a_child_whose_sharing_parent_ended_gets_compute_pool_unavailable_error() -> None:
    def body() -> object:
        # The sharing process is killed by its child, so the child reports to this process directly.
        read_fd, write_fd = os.pipe()
        sharing = os.fork()
        if sharing == 0:  # pragma: no cover - the sharing process, killed by its child
            try:
                os.close(read_fd)
                share_compute_pool()
                _worker_pid()
                child = os.fork()
                if child == 0:
                    code = 1
                    try:
                        code = report_to(write_fd, _ends_its_parent_mid_job)
                    finally:
                        os._exit(code)
                os.close(write_fd)
                time.sleep(0.1)
                fork_child(lambda: time.sleep(5))  # a sibling forked while the first child's job runs
                time.sleep(30)
            finally:
                os._exit(1)
        os.close(write_fd)
        seen = read_report(read_fd, sharing)
        return {"seen": seen, "sharing_exit": wait_exit(sharing)}

    assert in_forked_process(body) == {
        "seen": [
            _NO_RESULT,
            "the process holding the compute pool is gone; _operator.add cannot run",
            "answered at once",
        ],
        "sharing_exit": -signal.SIGKILL,
    }


def test_jobs_from_several_children_at_once_each_get_their_own_result() -> None:
    def body() -> object:
        _worker_pid()

        def burst(base: int) -> object:
            async def jobs() -> list[int]:
                return await asyncio.gather(*(compute_pool().run(operator.add, base, i) for i in range(100)))

            return asyncio.run(jobs()) == [base + i for i in range(100)]

        children = [fork_child(lambda base=base: burst(base)) for base in (0, 1000, 2000)]
        return [child.result() for child in children]

    assert _sharing(body) == [True, True, True]


def test_a_childs_job_starts_the_parents_pool_when_the_parent_never_ran_one() -> None:
    def body() -> object:
        before = process_pool_module._pool is not None
        child_worker = fork_child(_worker_pid).result()
        return {"before": before, "child_worker": child_worker, "parent_worker": _worker_pid()}

    seen = _sharing(body)

    assert seen == {"before": False, "child_worker": seen["parent_worker"], "parent_worker": seen["parent_worker"]}  # type: ignore[index]


def test_a_childs_job_after_the_parents_stop_compute_pool_starts_a_fresh_pool() -> None:
    def body() -> object:
        first_worker = _worker_pid()
        stopped = compute_pool()
        stop_compute_pool()
        child_worker = fork_child(_worker_pid).result()
        fresh = compute_pool()
        assert isinstance(fresh, ComputePool)
        return {
            "fresh_pool": fresh is not stopped,
            "child_worker_is_new": child_worker != first_worker,
            "parent_runs_in_it": _worker_pid() == child_worker,
        }

    assert _sharing(body) == {"fresh_pool": True, "child_worker_is_new": True, "parent_runs_in_it": True}


def _loops_whose_run_forever_is(
    run_forever: Callable[[asyncio.AbstractEventLoop, Callable[[], None]], None],
) -> list[asyncio.AbstractEventLoop]:
    """Make every event loop the share's serving thread makes run ``run_forever(loop, its_own_run_forever)``.

    Returns the loops made, in order.
    """
    made: list[asyncio.AbstractEventLoop] = []
    real_new_event_loop = asyncio.events.new_event_loop

    def new_event_loop() -> asyncio.AbstractEventLoop:
        loop = real_new_event_loop()
        if threading.current_thread().name != "compute pool share":
            return loop
        own = loop.run_forever
        loop.run_forever = lambda: run_forever(loop, own)  # type: ignore[method-assign]
        made.append(loop)
        return loop

    asyncio.events.new_event_loop = new_event_loop  # type: ignore[assignment]
    return made


def _socket_pairs_made() -> list[tuple[socket.socket, socket.socket]]:
    """Note every socket pair made from now on, the share's rendezvous and its loop's own; the pairs made."""
    made: list[tuple[socket.socket, socket.socket]] = []
    real_socketpair = socket.socketpair

    def socketpair(*args: object) -> tuple[socket.socket, socket.socket]:
        pair = real_socketpair(*args)  # type: ignore[arg-type]
        made.append(pair)
        return pair

    socket.socketpair = socketpair  # type: ignore[assignment]
    return made


def test_a_share_whose_loop_cannot_run_raises_at_its_start_and_leaves_nothing_open() -> None:
    def cannot_run(loop: asyncio.AbstractEventLoop, own: Callable[[], None]) -> None:
        raise RuntimeError("Cannot run the event loop while another loop is running")

    def body() -> object:
        loops = _loops_whose_run_forever_is(cannot_run)
        pairs = _socket_pairs_made()
        try:
            fork_share_module.PoolShare(process_pool_module._own_pool)
        except ComputePoolUnavailableError as exc:
            raised = f"{exc}; cause {type(exc.__cause__).__name__}: {exc.__cause__}"
        else:
            raised = "nothing"
        return {
            "raised": raised,
            "ends_left_open": sum(end.fileno() != -1 for pair in pairs for end in pair),
            "loops_closed": [loop.is_closed() for loop in loops],
        }

    assert in_forked_process(body) == {
        "raised": "the compute pool's share did not start serving; "
        "cause RuntimeError: Cannot run the event loop while another loop is running",
        "ends_left_open": 0,
        "loops_closed": [True],
    }


def _failed_share() -> dict[str, object]:
    """Call ``share_compute_pool()``, expecting it to raise; what it raised, and what it left behind."""
    pairs = _socket_pairs_made()
    try:
        share_compute_pool()
    except ComputePoolUnavailableError as exc:
        cause = exc.__cause__
        # An OSError is named by its error alone: the file it names depends on which call ran out.
        told = f"[Errno {cause.errno}] {cause.strerror}" if isinstance(cause, OSError) else cause
        raised = f"{exc}; cause {type(cause).__name__}: {told}"
    else:
        raised = "nothing"
    lock_free = process_pool_module._lock.acquire(blocking=False)
    if lock_free:
        process_pool_module._lock.release()
    return {
        "raised": raised,
        "ends_left_open": sum(end.fileno() != -1 for pair in pairs for end in pair),
        "lock_free": lock_free,
        "shared": process_pool_module._share is not None,
    }


def test_a_share_started_while_the_process_has_no_descriptor_free_raises_and_leaves_nothing_then_a_share_serves() -> (
    None
):
    def body() -> object:
        opened = len(os.listdir("/proc/self/fd"))
        recorded = set(wire_sockets._held)
        with at_descriptor_limit():
            failed = _failed_share()
        left = {
            "descriptors": len(os.listdir("/proc/self/fd")) - opened,
            "recorded": len(set(wire_sockets._held) ^ recorded),
        }
        share_compute_pool()
        try:
            child = fork_child(lambda: asyncio.run(compute_pool().run(operator.add, 20, 22, timeout=30)))
            return {"failed": failed, "left": left, "child_job": child.result()}
        finally:
            stop_compute_pool()

    assert in_forked_process(body) == {
        "failed": {
            "raised": "the compute pool's share did not start serving; cause OSError: [Errno 24] Too many open files",
            "ends_left_open": 0,
            "lock_free": True,
            "shared": False,
        },
        "left": {"descriptors": 0, "recorded": 0},
        "child_job": 42,
    }


def test_a_share_whose_loop_cannot_be_made_raises_at_its_start_and_leaves_nothing_open() -> None:
    def cannot_make() -> asyncio.AbstractEventLoop:
        raise OSError(errno.EMFILE, "Too many open files")

    def body() -> object:
        asyncio.events.new_event_loop = cannot_make  # type: ignore[assignment]
        return _failed_share()

    assert in_forked_process(body, timeout=10) == {
        "raised": "the compute pool's share did not start serving; cause OSError: [Errno 24] Too many open files",
        "ends_left_open": 0,
        "lock_free": True,
        "shared": False,
    }


def test_a_share_whose_thread_cannot_start_raises_at_its_start_and_leaves_nothing_open() -> None:
    class CannotStart(threading.Thread):
        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    def body() -> object:
        fork_share_module.threading.Thread = CannotStart  # type: ignore[misc]
        return _failed_share()

    assert in_forked_process(body, timeout=10) == {
        "raised": "the compute pool's share did not start serving; cause RuntimeError: can't start new thread",
        "ends_left_open": 0,
        "lock_free": True,
        "shared": False,
    }


def test_a_serving_loop_that_ends_with_a_childs_job_in_flight_ends_that_job_and_leaves_nothing_pending() -> None:
    def ends_when_stopped(loop: asyncio.AbstractEventLoop, own: Callable[[], None]) -> None:
        own()
        raise RuntimeError("the serving loop failed")

    def body() -> object:
        loops = _loops_whose_run_forever_is(ends_when_stopped)
        share_compute_pool()
        try:
            share = process_pool_module._share
            assert share is not None
            (serving_thread,) = (thread for thread in threading.enumerate() if thread.name == "compute pool share")

            def child() -> object:
                try:
                    asyncio.run(asyncio.wait_for(compute_pool().run(time.sleep, 30), 5))
                except Exception as exc:  # what the child's job ends in is the report
                    return f"{type(exc).__name__}: {exc}"
                return "ran"

            in_flight = fork_child(child)
            deadline = time.monotonic() + 5
            while not share._server._connections and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.2)  # the job reaches the worker
            loops[0].call_soon_threadsafe(loops[0].stop)
            outcome = in_flight.result()
            serving_thread.join(5)
            return {
                "child": outcome,
                "serving_thread_ended": not serving_thread.is_alive(),
                "loop_closed": loops[0].is_closed(),
                "tasks_pending": sum(not task.done() for task in asyncio.all_tasks(loops[0])),
            }
        finally:
            stop_compute_pool()

    assert in_forked_process(body) == {
        "child": f"ComputePoolUnavailableError: {_NO_RESULT}",
        "serving_thread_ended": True,
        "loop_closed": True,
        "tasks_pending": 0,
    }


def test_a_serving_loop_that_ends_logs_the_error_and_a_childs_next_job_finds_the_pool_gone() -> None:
    def ends_after_serving(loop: asyncio.AbstractEventLoop, own: Callable[[], None]) -> None:
        loop.call_later(0.1, loop.stop)
        own()
        raise RuntimeError("the serving loop failed")

    def body() -> object:
        _loops_whose_run_forever_is(ends_after_serving)
        logged: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = logged.append  # type: ignore[method-assign]
        logging.getLogger(fork_share_module.__name__).addHandler(handler)
        share_compute_pool()
        try:
            share = process_pool_module._share
            assert share is not None
            deadline = time.monotonic() + 5
            while share._server._serving_end.fileno() != -1 and time.monotonic() < deadline:
                time.sleep(0.01)

            def child() -> object:
                try:
                    asyncio.run(asyncio.wait_for(compute_pool().run(operator.add, 1, 1), 10))
                except ComputePoolUnavailableError as exc:
                    return str(exc)
                return "ran"

            return {
                "logged": [
                    [record.levelname, record.getMessage(), str(record.exc_info[1] if record.exc_info else None)]
                    for record in logged
                ],
                "serving_end_closed": share._server._serving_end.fileno() == -1,
                "child": fork_child(child).result(),
            }
        finally:
            stop_compute_pool()

    assert in_forked_process(body) == {
        "logged": [["ERROR", "the compute pool's share stopped serving", "the serving loop failed"]],
        "serving_end_closed": True,
        "child": "the process holding the compute pool is gone; _operator.add cannot run",
    }


# Set first in a child ``_fork_a_paused_child`` forks, which never forks again.
_IN_PAUSED_CHILD = False


def _fork_a_paused_child(forks: list[int], pid_to: int | None = None) -> None:
    """Fork, once per ``forks``, a child that only pauses until killed; its pid goes to ``forks`` and ``pid_to``."""
    global _IN_PAUSED_CHILD
    if _IN_PAUSED_CHILD or forks:
        return
    forks.append(0)  # counted before the fork
    child = os.fork()
    if child == 0:  # pragma: no cover - runs in the child, which exits through os._exit
        _IN_PAUSED_CHILD = True
        try:
            signal.alarm(60)
            signal.pause()
        finally:
            os._exit(0)
    forks[0] = child
    if pid_to is not None:
        os.write(pid_to, f"{child}\n".encode())


def _from_another_thread(call: Callable[[], None], wait: float | None = None) -> None:
    """Run ``call`` on a thread of its own; wait for it to end, or for ``wait`` seconds at most."""
    thread = threading.Thread(target=call)
    thread.start()
    thread.join(wait)


def _kill_the_pid_read_from(pids: int) -> None:
    ready, _, _ = select.select([pids], [], [], 10)
    assert ready, "no paused child was forked"
    os.kill(int(os.read(pids, 64).split()[0]), signal.SIGKILL)


def _a_child_ends_its_sharing_parent(arm: Callable[[int], None], child: Callable[[int], object]) -> object:
    """What ``child(pids)`` returned, run in a child of a sharing process it ends.

    The sharing process calls ``arm(pid_to)`` before it shares its pool, runs a job in it, forks
    the child and waits to be killed. A paused child forked in the sharing process writes its pid
    to ``pid_to``, which the child reads on ``pids``.
    """

    def body() -> object:
        # The sharing process is killed by its child, so the child reports to this process directly.
        reports, report_to_end = os.pipe()
        pids, pid_to = os.pipe()
        sharing = os.fork()
        if sharing == 0:  # pragma: no cover - the sharing process, killed by its child
            try:
                os.close(reports)
                arm(pid_to)
                share_compute_pool()
                _worker_pid()
                if os.fork() == 0:
                    code = 1
                    try:
                        code = report_to(report_to_end, lambda: child(pids))
                    finally:
                        os._exit(code)
                os.close(report_to_end)
                time.sleep(30)
            finally:
                os._exit(1)
        os.close(report_to_end)
        os.close(pid_to)
        try:
            return {"seen": read_report(reports, sharing), "sharing_exit": wait_exit(sharing)}
        finally:
            os.close(pids)

    return in_forked_process(body)


def _a_job_as_the_sharing_parent_is_killed(after: float) -> dict[str, object]:
    """Run a job in the parent's pool, killing the parent ``after`` seconds into it; how the job ended."""

    async def job() -> dict[str, object]:
        asyncio.get_running_loop().call_later(after, os.kill, os.getppid(), signal.SIGKILL)
        started = time.monotonic()
        try:
            await asyncio.wait_for(compute_pool().run(time.sleep, 3, timeout=10), 10)
        except ComputePoolUnavailableError as exc:
            ended = str(exc)
        except TimeoutError:
            ended = "no reply within 10 s"
        else:
            ended = "ran"
        return {"job": ended, "at_once": time.monotonic() - started < after + 2}

    return asyncio.run(job())


_KILLED_MID_JOB = {
    "seen": {"job": _NO_RESULT, "at_once": True},
    "sharing_exit": -signal.SIGKILL,
}


def test_a_grandchild_forked_by_another_thread_while_a_job_is_sent_leaves_a_killed_parents_job_failing_at_once() -> (
    None
):
    # The child's call to ``socket.send_fds`` waits for another thread to fork, so the fork lands
    # while the job holds both ends of its socket pair.
    def child(pids: int) -> object:
        forks: list[int] = []
        send = socket.send_fds

        def send_after_a_fork(*args: object, **kwargs: object) -> int:
            _from_another_thread(lambda: _fork_a_paused_child(forks))
            return send(*args, **kwargs)  # type: ignore[arg-type]

        socket.send_fds = send_after_a_fork  # type: ignore[assignment]
        try:
            return _a_job_as_the_sharing_parent_is_killed(after=1.5)
        finally:
            socket.send_fds = send
            for fork in forks:
                os.kill(fork, signal.SIGKILL)
                os.waitpid(fork, 0)

    assert _a_child_ends_its_sharing_parent(lambda pid_to: None, child) == _KILLED_MID_JOB


def test_a_child_forked_by_another_thread_while_a_job_is_received_leaves_that_job_failing_at_once() -> None:
    # The sharing process's call to ``recvmsg`` waits, a second at most, for another thread to
    # fork once it received a job's socket, so the fork lands while the received socket is in
    # hand, or as soon as the receiving lets it.
    def arm(pid_to: int) -> None:
        forks: list[int] = []
        receive = socket.socket.recvmsg

        def receive_then_fork(
            end: socket.socket, *args: object
        ) -> tuple[bytes, list[tuple[int, int, bytes]], int, object]:
            received = receive(end, *args)  # type: ignore[arg-type]
            if received[1]:
                _from_another_thread(lambda: _fork_a_paused_child(forks, pid_to), wait=1)
            return received

        socket.socket.recvmsg = receive_then_fork  # type: ignore[method-assign,assignment]

    def child(pids: int) -> object:
        try:
            return _a_job_as_the_sharing_parent_is_killed(after=1.5)
        finally:
            _kill_the_pid_read_from(pids)

    assert _a_child_ends_its_sharing_parent(arm, child) == _KILLED_MID_JOB


def test_a_child_forked_while_the_share_starts_leaves_a_later_childs_job_failing_once_the_parent_ended() -> None:
    # The share's serving thread starts once its starter forked, so the fork lands while the
    # share's two ends exist and the process does not share yet.
    def arm(pid_to: int) -> None:
        forks: list[int] = []
        start = threading.Thread.start

        def start_after_a_fork(thread: threading.Thread) -> None:
            if thread.name == "compute pool share":
                _fork_a_paused_child(forks, pid_to)
            start(thread)

        threading.Thread.start = start_after_a_fork  # type: ignore[method-assign]

    def child(pids: int) -> object:
        async def job() -> str:
            parent = os.getppid()
            os.kill(parent, signal.SIGKILL)
            deadline = time.monotonic() + 2
            # A killed process closes its sockets before its children are reparented.
            while os.getppid() == parent and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            try:
                await asyncio.wait_for(compute_pool().run(operator.add, 1, 1, timeout=10), 5)
            except ComputePoolUnavailableError as exc:
                return str(exc)
            except TimeoutError:
                return "no reply within 5 s"
            return "ran"

        try:
            return asyncio.run(job())
        finally:
            _kill_the_pid_read_from(pids)

    assert _a_child_ends_its_sharing_parent(arm, child) == {
        "seen": "the process holding the compute pool is gone; _operator.add cannot run",
        "sharing_exit": -signal.SIGKILL,
    }
