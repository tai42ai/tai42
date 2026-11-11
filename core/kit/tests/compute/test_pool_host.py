"""The compute jobs of a process started through a prelude run in a pool host it starts.

Each test runs an owner script in a fresh process started through a prelude, in a process group of
its own, within a bound: a hang fails the test instead of holding it, and whatever the owner left
running is killed with its group when the test ends. The preludes are the two thread-reshaping
ones the backend plugins declare and one of the tests' own that names no library.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from tai42_contract.backend import PROCESS_PRELUDE_ENV

from ..backend.preludes import PROCESS_BOUND_SECONDS, RESHAPING_PRELUDES  # noqa: TID252 shared with the backend tests

PRELUDES = [*RESHAPING_PRELUDES, "prelude_without_poll:remove_poll"]

# The owner script's opening: argv[1] names its prelude, argv[2] the directory of the tests' own
# prelude. A process ended (reaped, or a zombie) reads as ``ended``.
_OWNER_OPENING = """
import importlib
import sys

sys.path.insert(0, sys.argv[2])
module_name, _, callable_name = sys.argv[1].partition(":")
getattr(importlib.import_module(module_name), callable_name)()

import asyncio
import json
import logging
import operator
import os
import signal
import time

from tai42_kit.backend.reshaped_threads import bind_future_chaining
from tai42_kit.compute import ComputePool, ComputePoolUnavailableError, compute_pool, stop_compute_pool

bind_future_chaining()
logging.basicConfig(level=logging.WARNING)


def report(seen):
    print(json.dumps(seen), flush=True)


def ended(pid):
    # A main thread that ended shows as a zombie while the process's other threads still run.
    try:
        with open(f"/proc/{pid}/stat") as stat:
            zombie = stat.read().rpartition(")")[2].split()[0] == "Z"
        return zombie and len(os.listdir(f"/proc/{pid}/task")) == 1
    except FileNotFoundError:
        return True


def ended_within(pid, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if ended(pid):
            return True
        time.sleep(0.01)
    return False


def run(fn, *args):
    return asyncio.run(compute_pool().run(fn, *args, timeout=30))
"""


@dataclass
class Owner:
    """An owner process that finished: its exit code, its output, and its process group."""

    returncode: int
    stdout: str
    stderr: str
    group: int

    def seen(self) -> object:
        """What the owner reported last."""
        assert self.stdout.strip(), self.stderr
        return json.loads(self.stdout.strip().splitlines()[-1])


def _group_alive(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    return True


def _group_ends_within(group: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _group_alive(group):
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def owner() -> Iterator[Callable[[str, str], Owner]]:
    """Run an owner script body under a prelude; every group an owner left is killed at the end."""
    groups: list[int] = []

    def run(prelude: str, body: str) -> Owner:
        process = subprocess.Popen(
            [sys.executable, "-c", _OWNER_OPENING + textwrap.dedent(body), prelude, str(Path(__file__).parent)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
            env={**os.environ, PROCESS_PRELUDE_ENV: prelude, "COMPUTE_WORKERS": "1"},
        )
        groups.append(process.pid)
        try:
            stdout, stderr = process.communicate(timeout=PROCESS_BOUND_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            pytest.fail(
                f"the owner under {prelude} did not finish within {PROCESS_BOUND_SECONDS} s:\n{stdout}\n{stderr}"
            )
        return Owner(process.returncode, stdout, stderr, process.pid)

    yield run
    for group in groups:
        if _group_alive(group):
            os.killpg(group, signal.SIGKILL)


@pytest.mark.parametrize("prelude", PRELUDES)
def test_a_job_returns_its_result(owner: Callable[[str, str], Owner], prelude: str) -> None:
    done = owner(
        prelude,
        """
        report({"result": run(operator.add, 20, 22)})
        stop_compute_pool()
        """,
    )

    assert done.returncode == 0, done.stderr
    assert done.seen() == {"result": 42}, done.stderr


@pytest.mark.parametrize("prelude", PRELUDES)
def test_a_job_past_its_timeout_raises_without_stalling_the_loop_or_the_job_behind_it(
    owner: Callable[[str, str], Owner], prelude: str
) -> None:
    done = owner(
        prelude,
        """
        async def main():
            pool = compute_pool()
            await pool.run(operator.add, 1, 1, timeout=30)
            worst = 0.0

            async def tick():
                nonlocal worst
                last = time.monotonic()
                while True:
                    await asyncio.sleep(0.05)
                    now = time.monotonic()
                    worst = max(worst, now - last - 0.05)
                    last = now

            async def sleeping():
                started = time.monotonic()
                try:
                    await pool.run(time.sleep, 30, timeout=2)
                except TimeoutError:
                    return ["TimeoutError", time.monotonic() - started]
                return ["returned", time.monotonic() - started]

            ticker = asyncio.ensure_future(tick())
            first = asyncio.ensure_future(sleeping())
            await asyncio.sleep(0.3)
            behind = await pool.run(len, b"x" * (1 << 20), timeout=30)
            first = await first
            ticker.cancel()
            return {"first": first[0], "after": first[1], "behind": behind, "worst_stall": worst}

        report(asyncio.run(main()))
        stop_compute_pool()
        """,
    )

    assert done.returncode == 0, done.stderr
    seen = done.seen()
    assert isinstance(seen, dict), done.stderr
    assert seen["first"] == "TimeoutError", done.stderr
    assert 1.5 < seen["after"] < 5, done.stderr
    assert seen["behind"] == 1 << 20, done.stderr
    assert seen["worst_stall"] < 0.5, done.stderr


@pytest.mark.parametrize("prelude", PRELUDES)
def test_the_owner_leaves_through_the_interpreter_and_its_host_ends_with_it(
    owner: Callable[[str, str], Owner], prelude: str
) -> None:
    done = owner(
        prelude,
        """
        report({"result": run(operator.add, 1, 1)})
        """,
    )

    assert done.returncode == 0, done.stderr
    assert done.seen() == {"result": 2}, done.stderr
    assert _group_ends_within(done.group, 5), "the owner's host or its workers outlived it"


@pytest.mark.parametrize("prelude", PRELUDES)
def test_a_host_killed_mid_job_fails_the_job_and_the_next_job_runs_in_a_fresh_host(
    owner: Callable[[str, str], Owner], prelude: str
) -> None:
    done = owner(
        prelude,
        """
        async def main():
            host = await compute_pool().run(os.getppid, timeout=30)
            if host == os.getpid():
                return {"host": "none"}
            # The killed host's worker finishes its job before it reads its closed task pipe and
            # ends, holding the owner's output open until then: the job is short.
            job = asyncio.ensure_future(compute_pool().run(time.sleep, 3))
            await asyncio.sleep(0.5)
            os.kill(host, signal.SIGKILL)
            try:
                await job
            except ComputePoolUnavailableError:
                lost = "ComputePoolUnavailableError"
            else:
                lost = "returned"
            host_ended = ended_within(host, 10)
            replacement = await compute_pool().run(os.getppid, timeout=30)
            return {"lost": lost, "host_ended": host_ended, "fresh_host": replacement not in (host, os.getpid())}

        report(asyncio.run(main()))
        stop_compute_pool()
        """,
    )

    assert done.returncode == 0, done.stderr
    assert done.seen() == {"lost": "ComputePoolUnavailableError", "host_ended": True, "fresh_host": True}, done.stderr
    assert "compute pool host replaced after it ended" in done.stderr


@pytest.mark.parametrize("prelude", PRELUDES)
def test_jobs_past_the_hosts_descriptor_limit_raise_alone_and_the_next_job_runs_in_the_same_host(
    owner: Callable[[str, str], Owner], prelude: str
) -> None:
    # The host inherits the descriptor limit its owner had when it started it; a burst of jobs
    # holds more job sockets in the host at once than that limit leaves free.
    done = owner(
        prelude,
        """
        import collections
        import resource

        from tai42_kit.compute.job import job_name

        async def main():
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            resource.setrlimit(resource.RLIMIT_NOFILE, (48, hard))
            try:
                host = await compute_pool().run(os.getppid, timeout=30)
            finally:
                resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
            if host == os.getpid():
                return {"host": "none"}
            outcomes = await asyncio.gather(
                *[compute_pool().run(time.sleep, 0.1, timeout=60) for _ in range(60)], return_exceptions=True
            )
            kinds = collections.Counter(
                "returned" if outcome is None else f"{type(outcome).__name__}: {outcome}" for outcome in outcomes
            )
            next_host = await compute_pool().run(os.getppid, timeout=30)
            return {"job": job_name(time.sleep), "kinds": dict(kinds), "same_host": next_host == host}

        report(asyncio.run(main()))
        stop_compute_pool()
        """,
    )

    assert done.returncode == 0, done.stderr
    seen = done.seen()
    assert isinstance(seen, dict), done.stderr
    kinds = seen["kinds"]
    no_result = (
        f"ComputePoolUnavailableError: the process holding the compute pool gave no result for {seen['job']}: "
        "it ended, or it could not take or serve the job"
    )
    assert set(kinds) == {"returned", no_result}, done.stderr
    assert sum(kinds.values()) == 60, done.stderr
    assert seen["same_host"] is True, done.stderr
    assert (
        "a compute job was not taken: this process has no file descriptor free for its socket; its caller raises"
        in done.stderr
    )
    assert "compute pool host replaced after it ended" not in done.stderr


@pytest.mark.parametrize("prelude", PRELUDES)
def test_a_first_job_while_the_owner_has_no_descriptor_free_raises_that_the_host_did_not_start_and_leaves_nothing(
    owner: Callable[[str, str], Owner], prelude: str
) -> None:
    done = owner(
        prelude,
        """
        from descriptor_limit import at_descriptor_limit
        from tai42_kit.compute.wire_sockets import wire_sockets

        def opened():
            return len(os.listdir("/proc/self/fd"))

        async def main():
            before, recorded = opened(), set(wire_sockets._held)
            with at_descriptor_limit():
                try:
                    await compute_pool().run(os.getppid, timeout=30)
                except Exception as exc:
                    raised = f"{type(exc).__name__}: {exc}; cause {type(exc.__cause__).__name__}"
                else:
                    raised = "nothing"
            left = {"descriptors": opened() - before, "recorded": len(set(wire_sockets._held) ^ recorded)}
            host = await compute_pool().run(os.getppid, timeout=30)
            return {"raised": raised, "left": left, "next_job_in_a_host": host != os.getpid()}

        report(asyncio.run(main()))
        stop_compute_pool()
        """,
    )

    assert done.returncode == 0, done.stderr
    assert done.seen() == {
        "raised": "ComputePoolUnavailableError: the compute pool host did not start; cause OSError",
        "left": {"descriptors": 0, "recorded": 0},
        "next_job_in_a_host": True,
    }, done.stderr


@pytest.mark.parametrize("prelude", PRELUDES)
def test_a_pool_taken_before_its_host_was_killed_runs_a_later_job_in_a_fresh_host(
    owner: Callable[[str, str], Owner], prelude: str
) -> None:
    done = owner(
        prelude,
        """
        async def main():
            pool = compute_pool()
            host = await pool.run(os.getppid, timeout=30)
            if host == os.getpid():
                return {"host": "none"}
            os.kill(host, signal.SIGKILL)
            host_ended = ended_within(host, 10)
            replacement = await pool.run(os.getppid, timeout=30)
            return {"host_ended": host_ended, "fresh_host": replacement not in (host, os.getpid())}

        report(asyncio.run(main()))
        stop_compute_pool()
        """,
    )

    assert done.returncode == 0, done.stderr
    assert done.seen() == {"host_ended": True, "fresh_host": True}, done.stderr
    assert "compute pool host replaced after it ended" in done.stderr


@pytest.mark.parametrize("prelude", PRELUDES)
def test_a_child_forked_by_another_thread_while_the_host_starts_keeps_a_killed_host_replaceable(
    owner: Callable[[str, str], Owner], prelude: str
) -> None:
    # The start's call to ``subprocess.Popen`` waits for another thread to fork, so the fork lands
    # while the start holds its sockets. The child blocks in ``signal.pause``, which no prelude
    # makes yield, until the owner kills it.
    done = owner(
        prelude,
        """
        import subprocess
        import threading

        children = []

        def fork_a_paused_child():
            child = os.fork()
            if child == 0:
                try:
                    signal.pause()
                finally:
                    os._exit(0)
            children.append(child)

        start_process = subprocess.Popen

        def start_process_after_a_fork(*args, **kwargs):
            if not children:
                forker = threading.Thread(target=fork_a_paused_child)
                forker.start()
                forker.join()
            return start_process(*args, **kwargs)

        subprocess.Popen = start_process_after_a_fork

        async def main():
            host = await compute_pool().run(os.getppid, timeout=30)
            if host == os.getpid():
                return {"host": "none"}
            os.kill(host, signal.SIGKILL)
            host_ended = ended_within(host, 10)
            try:
                replacement = await asyncio.wait_for(compute_pool().run(os.getppid, timeout=30), 10)
            except TimeoutError:
                return {"forked": len(children), "host_ended": host_ended, "next_job": "no reply within 10 s"}
            fresh = replacement not in (host, os.getpid())
            return {"forked": len(children), "host_ended": host_ended, "next_job": fresh}

        try:
            report(asyncio.run(main()))
        finally:
            for child in children:
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
            stop_compute_pool()
        """,
    )

    assert done.returncode == 0, done.stderr
    assert done.seen() == {"forked": 1, "host_ended": True, "next_job": True}, done.stderr
    assert "compute pool host replaced after it ended" in done.stderr
    assert _group_ends_within(done.group, 5), "the owner left a process running"


# An owner script part: ``fork_a_paused_child()`` forks at most one child, from any thread, which
# marks itself first, never forks again, writes nothing to the owner's output and blocks in
# ``signal.pause`` (which no prelude makes yield) until it is killed.
_PAUSED_CHILD = """
import threading

IN_CHILD = False
FORKS = 0
children = []


def fork_a_paused_child(before_pause=None):
    global IN_CHILD, FORKS
    if IN_CHILD or FORKS >= 1:
        return
    FORKS += 1
    child = os.fork()
    if child == 0:
        IN_CHILD = True
        try:
            # The owner's output stays the owner's: the test reads it to its end.
            null = os.open(os.devnull, os.O_WRONLY)
            os.dup2(null, 1)
            os.dup2(null, 2)
            if before_pause is not None:
                before_pause()
            signal.pause()
        finally:
            os._exit(0)
    children.append(child)


def fork_a_paused_child_from_another_thread(before_pause=None):
    if IN_CHILD or FORKS >= 1:
        return
    forker = threading.Thread(target=fork_a_paused_child, args=(before_pause,))
    forker.start()
    forker.join()
"""


@pytest.mark.parametrize("prelude", PRELUDES)
def test_a_child_forked_by_another_thread_while_a_job_is_sent_leaves_a_killed_hosts_job_failing_at_once(
    owner: Callable[[str, str], Owner], prelude: str
) -> None:
    # The job's call to ``socket.send_fds`` waits for another thread to fork, so the fork lands
    # while the job holds both ends of its socket pair.
    done = owner(
        prelude,
        _PAUSED_CHILD
        + textwrap.dedent(
            """
        import socket

        armed = []
        send_job = socket.send_fds

        def send_job_after_a_fork(*args, **kwargs):
            if armed:
                fork_a_paused_child_from_another_thread()
            return send_job(*args, **kwargs)

        socket.send_fds = send_job_after_a_fork

        async def main():
            host = await compute_pool().run(os.getppid, timeout=30)
            if host == os.getpid():
                return {"host": "none"}
            armed.append(True)
            # The killed host's worker finishes its job before it reads its closed task pipe and
            # ends, holding the owner's output open until then: the job is short.
            job = asyncio.ensure_future(compute_pool().run(time.sleep, 3, timeout=30))
            await asyncio.sleep(0.5)
            os.kill(host, signal.SIGKILL)
            started = time.monotonic()
            try:
                await asyncio.wait_for(job, 10)
            except ComputePoolUnavailableError:
                lost = "ComputePoolUnavailableError"
            except TimeoutError:
                lost = "no reply within 10 s"
            else:
                lost = "returned"
            return {"forked": FORKS, "lost": lost, "at_once": time.monotonic() - started < 5}

        try:
            report(asyncio.run(main()))
        finally:
            if not IN_CHILD:
                for child in children:
                    os.kill(child, signal.SIGKILL)
                    os.waitpid(child, 0)
                stop_compute_pool()
        """
        ),
    )

    assert done.returncode == 0, done.stderr
    assert done.seen() == {"forked": 1, "lost": "ComputePoolUnavailableError", "at_once": True}, done.stderr
    assert _group_ends_within(done.group, 5), "the owner left a process running"


@pytest.mark.parametrize("prelude", PRELUDES)
def test_a_child_forked_by_another_thread_while_a_job_waits_for_its_outcome_leaves_the_job_cancellable(
    owner: Callable[[str, str], Owner], prelude: str
) -> None:
    # The fork lands while the owner's loop watches the job's socket end for its outcome; the child
    # never yields, so only what its fork hooks did before the fork returned counts.
    done = owner(
        prelude,
        _PAUSED_CHILD
        + textwrap.dedent(
            """
        async def main():
            pool = compute_pool()
            worker = await pool.run(os.getpid, timeout=30)
            if await pool.run(os.getppid, timeout=30) == os.getpid():
                return {"host": "none"}
            job = asyncio.ensure_future(pool.run(time.sleep, 20, timeout=30))
            await asyncio.sleep(0.5)
            fork_a_paused_child_from_another_thread()
            worker_ran = not ended(worker)
            job.cancel()
            try:
                await job
            except asyncio.CancelledError:
                pass
            worker_stopped = ended_within(worker, 5)
            child_lives = bool(children) and not ended(children[0])
            return {
                "forked": FORKS,
                "worker_ran": worker_ran,
                "worker_stopped": worker_stopped,
                "child_lives": child_lives,
            }

        try:
            report(asyncio.run(main()))
        finally:
            if not IN_CHILD:
                for child in children:
                    os.kill(child, signal.SIGKILL)
                    os.waitpid(child, 0)
                stop_compute_pool()
        """
        ),
    )

    assert done.returncode == 0, done.stderr
    assert done.seen() == {"forked": 1, "worker_ran": True, "worker_stopped": True, "child_lives": True}, done.stderr
    assert _group_ends_within(done.group, 5), "the owner left a process running"


@pytest.mark.parametrize("prelude", PRELUDES)
def test_a_child_forked_by_another_thread_as_the_host_is_published_reaches_that_host(
    owner: Callable[[str, str], Owner], prelude: str
) -> None:
    # The start's call to ``atexit.register`` waits for another thread to fork, so the fork lands
    # after the host started and before the process holds it. The child reports what its jobs
    # would go to, then pauses until the owner kills it.
    done = owner(
        prelude,
        _PAUSED_CHILD
        + textwrap.dedent(
            """
        import atexit

        from tai42_kit.compute import ComputePoolClient

        reads, writes = os.pipe()

        def report_what_the_childs_jobs_go_to():
            os.write(writes, b"1" if isinstance(compute_pool(), ComputePoolClient) else b"0")

        register_at_exit = atexit.register

        def register_at_exit_after_a_fork(*args, **kwargs):
            fork_a_paused_child_from_another_thread(report_what_the_childs_jobs_go_to)
            return register_at_exit(*args, **kwargs)

        atexit.register = register_at_exit_after_a_fork

        try:
            run(operator.add, 1, 1)
            reaches = os.read(reads, 1) == b"1" if FORKS else None
            report({"forked": FORKS, "child_reaches_the_host": reaches})
        finally:
            if not IN_CHILD:
                for child in children:
                    os.kill(child, signal.SIGKILL)
                    os.waitpid(child, 0)
                stop_compute_pool()
        """
        ),
    )

    assert done.returncode == 0, done.stderr
    assert done.seen() == {"forked": 1, "child_reaches_the_host": True}, done.stderr
    assert _group_ends_within(done.group, 5), "the owner left a process running"


@pytest.mark.parametrize("prelude", PRELUDES)
def test_a_job_whose_module_the_owner_added_to_its_search_path_runs_in_the_host(
    owner: Callable[[str, str], Owner], prelude: str, tmp_path: Path
) -> None:
    # Not the owner's working directory, which its search path already holds.
    added = tmp_path / "added"
    added.mkdir()
    (added / "added_jobs.py").write_text("import os\n\n\ndef where():\n    return os.getppid()\n")
    done = owner(
        prelude,
        f"""
        import site

        site.addsitedir({str(added)!r})
        import added_jobs

        report({{"host_apart": run(added_jobs.where) != os.getpid()}})
        stop_compute_pool()
        """,
    )

    assert done.returncode == 0, done.stderr
    assert done.seen() == {"host_apart": True}, done.stderr


@pytest.mark.parametrize("prelude", PRELUDES)
def test_a_search_path_longer_than_one_argument_reaches_the_host(
    owner: Callable[[str, str], Owner], prelude: str, tmp_path: Path
) -> None:
    done = owner(
        prelude,
        f"""
        sys.path.extend(f"{{{str(tmp_path)!r}}}/absent/{{i:04d}}/" + "x" * 110 for i in range(1200))
        size = len(json.dumps([entry for entry in sys.path if isinstance(entry, str)]))
        # The kernel's bound on one argument of a new program.
        one_argument = 32 * os.sysconf("SC_PAGE_SIZE")
        report({{"longer_than_one_argument": size > one_argument, "host_apart": run(os.getppid) != os.getpid()}})
        stop_compute_pool()
        """,
    )

    assert done.returncode == 0, done.stderr
    assert done.seen() == {"longer_than_one_argument": True, "host_apart": True}, done.stderr


@pytest.mark.parametrize("prelude", PRELUDES)
def test_a_host_that_ends_before_it_reads_the_search_path_fails_its_start_and_leaves_no_child(
    owner: Callable[[str, str], Owner], prelude: str, tmp_path: Path
) -> None:
    # The host's interpreter cannot initialise without its standard library, so it exits before it
    # reads the search path; the path is longer than a socket buffer, so the owner's send meets it.
    done = owner(
        prelude,
        f"""
        sys.path.extend(f"{{{str(tmp_path)!r}}}/absent/{{i:05d}}/" + "x" * 110 for i in range(20000))
        os.environ["PYTHONHOME"] = {str(tmp_path / "absent")!r}
        try:
            run(os.getppid)
        except ComputePoolUnavailableError as exc:
            seen = {{"raised": str(exc), "refused_by_the_host": isinstance(exc.__cause__, ConnectionError)}}
        else:
            seen = {{"raised": None}}
        children = []
        for entry in filter(str.isdigit, os.listdir("/proc")):
            try:
                with open(f"/proc/{{entry}}/stat") as stat:
                    if stat.read().rpartition(")")[2].split()[1] == str(os.getpid()):
                        children.append(entry)
            except FileNotFoundError:
                continue  # the process ended as the list was read
        seen["children"] = children
        report(seen)
        """,
    )

    assert done.returncode == 0, done.stderr
    assert done.seen() == {
        "raised": "the compute pool host did not start",
        "refused_by_the_host": True,
        "children": [],
    }, done.stderr


@pytest.mark.parametrize("prelude", PRELUDES)
def test_a_start_interrupted_once_the_host_exists_raises_that_interrupt_and_leaves_no_end_and_no_host(
    owner: Callable[[str, str], Owner], prelude: str
) -> None:
    # The send of the search path raises an exception that is not an ``OSError``, as an interrupt
    # of the starting thread or greenlet would, once the host process's start returned.
    done = owner(
        prelude,
        """
        import socket
        import subprocess

        from tai42_kit.compute import process_pool
        from tai42_kit.compute.pool_host import PoolHost
        from tai42_kit.compute.wire_sockets import wire_sockets


        class Interrupted(BaseException):
            pass


        started, made = [], []
        real_popen, real_socketpair, real_sendall = subprocess.Popen, socket.socketpair, socket.socket.sendall

        def popen(*args, **kwargs):
            started.append(real_popen(*args, **kwargs))
            return started[-1]

        def socketpair(*args, **kwargs):
            made.append(real_socketpair(*args, **kwargs))
            return made[-1]

        def sendall(self, *args, **kwargs):
            raise Interrupted("the start was interrupted")

        subprocess.Popen, socket.socketpair, socket.socket.sendall = popen, socketpair, sendall
        try:
            PoolHost(process_pool._serve_as_host)
        except Interrupted as exc:
            raised = str(exc)
        else:
            raised = None
        finally:
            subprocess.Popen, socket.socketpair, socket.socket.sendall = real_popen, real_socketpair, real_sendall
        report({
            "raised": raised,
            "ends_left_open": sum(end.fileno() != -1 for pair in made for end in pair),
            "kept_in_children": wire_sockets.kept_in_children,
            "hosts_reaped": [process.returncode for process in started],
            "hosts_ended": [ended(process.pid) for process in started],
        })
        """,
    )

    assert done.returncode == 0, done.stderr
    assert done.seen() == {
        "raised": "the start was interrupted",
        "ends_left_open": 0,
        "kept_in_children": None,
        "hosts_reaped": [-signal.SIGKILL],
        "hosts_ended": [True],
    }, done.stderr


@pytest.mark.parametrize("prelude", PRELUDES)
def test_a_killed_owner_leaves_neither_its_host_nor_its_workers(
    owner: Callable[[str, str], Owner], prelude: str
) -> None:
    done = owner(
        prelude,
        """
        report({"host_apart": run(os.getppid) != os.getpid()})
        os.kill(os.getpid(), signal.SIGKILL)
        """,
    )

    assert done.returncode == -signal.SIGKILL, done.stderr
    assert done.seen() == {"host_apart": True}, done.stderr
    assert _group_ends_within(done.group, 5), "the killed owner's host or its workers kept running"


@pytest.mark.parametrize("prelude", PRELUDES)
def test_stopping_the_pool_ends_the_host_and_its_workers_and_a_later_job_starts_a_fresh_host(
    owner: Callable[[str, str], Owner], prelude: str
) -> None:
    done = owner(
        prelude,
        """
        host = run(os.getppid)
        if host == os.getpid():
            report({"host": "none"})
            sys.exit(0)
        worker = run(os.getpid)
        stop_compute_pool()
        stopped = ended_within(host, 5) and ended_within(worker, 5)
        fresh = run(os.getppid)
        report({"stopped": stopped, "fresh_host": fresh not in (host, os.getpid())})
        stop_compute_pool()
        """,
    )

    assert done.returncode == 0, done.stderr
    assert done.seen() == {"stopped": True, "fresh_host": True}, done.stderr


@pytest.mark.parametrize("prelude", PRELUDES)
def test_a_stop_whose_signal_reaches_the_host_after_its_serving_ended_still_ends_the_worker_mid_job(
    owner: Callable[[str, str], Owner], prelude: str
) -> None:
    # The owner's stop is held between closing its end and signalling the host. The worker ignores
    # ``SIGTERM``, so the host's stop of its pool waits the library's kill grace, and the signal
    # reaches the host inside it.
    done = owner(
        prelude,
        """
        import subprocess

        real_terminate = subprocess.Popen.terminate

        def terminate(self):
            time.sleep(1)
            real_terminate(self)

        subprocess.Popen.terminate = terminate


        async def main():
            pool = compute_pool()
            host = await pool.run(os.getppid, timeout=30)
            if host == os.getpid():
                return {"host": "none"}
            worker = await pool.run(os.getpid, timeout=30)
            await pool.run(signal.signal, signal.SIGTERM, signal.SIG_IGN, timeout=30)
            job = asyncio.ensure_future(pool.run(time.sleep, 60, timeout=20))
            await asyncio.sleep(0.5)
            stop_compute_pool()
            worker_ended = ended_within(worker, 5)
            try:
                await job
            except ComputePoolUnavailableError:
                lost = "ComputePoolUnavailableError"
            else:
                lost = "returned"
            return {"worker_ended": worker_ended, "lost": lost}

        report(asyncio.run(main()))
        """,
    )

    assert done.returncode == 0, done.stderr
    assert done.seen() == {"worker_ended": True, "lost": "ComputePoolUnavailableError"}, done.stderr


@pytest.mark.parametrize("prelude", PRELUDES)
def test_a_forked_child_runs_its_job_in_the_owners_host_and_its_exit_leaves_the_host_running(
    owner: Callable[[str, str], Owner], prelude: str
) -> None:
    done = owner(
        prelude,
        """
        host = run(os.getppid)
        read_end, write_end = os.pipe()
        child = os.fork()
        if child == 0:
            code = 1
            try:
                os.close(read_end)
                os.write(write_end, str(run(os.getppid)).encode())
                code = 0
            finally:
                os._exit(code)
        os.close(write_end)
        child_host = int(os.read(read_end, 64) or b"0")
        _, status = os.waitpid(child, 0)
        after = run(os.getppid)
        report({
            "host_apart": host != os.getpid(),
            "child_reached_the_host": child_host == host,
            "child_exit": os.waitstatus_to_exitcode(status),
            "host_kept": after == host,
        })
        stop_compute_pool()
        """,
    )

    assert done.returncode == 0, done.stderr
    assert done.seen() == {
        "host_apart": True,
        "child_reached_the_host": True,
        "child_exit": 0,
        "host_kept": True,
    }, done.stderr


@pytest.mark.parametrize("prelude", PRELUDES)
def test_a_pool_taken_before_a_fork_runs_the_childs_job_in_the_owners_host(
    owner: Callable[[str, str], Owner], prelude: str
) -> None:
    done = owner(
        prelude,
        """
        pool = compute_pool()
        host = asyncio.run(pool.run(os.getppid, timeout=30))
        read_end, write_end = os.pipe()
        child = os.fork()
        if child == 0:
            code = 1
            try:
                os.close(read_end)
                os.write(write_end, str(asyncio.run(pool.run(os.getppid, timeout=30))).encode())
                code = 0
            finally:
                os._exit(code)
        os.close(write_end)
        child_host = int(os.read(read_end, 64) or b"0")
        _, status = os.waitpid(child, 0)
        report({
            "host_apart": host != os.getpid(),
            "child_reached_the_host": child_host == host,
            "child_exit": os.waitstatus_to_exitcode(status),
        })
        stop_compute_pool()
        """,
    )

    assert done.returncode == 0, done.stderr
    assert done.seen() == {"host_apart": True, "child_reached_the_host": True, "child_exit": 0}, done.stderr


@pytest.mark.parametrize("prelude", PRELUDES)
def test_a_pool_started_directly_in_the_process_raises(owner: Callable[[str, str], Owner], prelude: str) -> None:
    done = owner(
        prelude,
        """
        pool = ComputePool(1)
        try:
            pool.start()
        except ComputePoolUnavailableError as exc:
            report({"refused": str(exc)})
        else:
            pool.stop()
            report({"refused": None})
        """,
    )

    assert done.returncode == 0, done.stderr
    assert done.seen() == {
        "refused": "a compute pool cannot run in a process its prelude reshaped; its jobs run in a pool host "
        "(compute_pool())"
    }, done.stderr
