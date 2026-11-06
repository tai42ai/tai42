"""A test's timeout that cuts a wait names what the wait saw, and a wait keeps its own deadline up to the timeout."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tai42_e2e import readiness, waiting
from tai42_e2e.stack import TaiStack
from tai42_e2e.waiting import WaitTimeoutError, wait_for

InnerPytest = Callable[..., str]

# A listening socket the inner test closes a beat after its cleanup starts, as a killed process's socket closes.
_CLOSING_LISTENER = """
import socket
import threading


def listener():
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    return sock, sock.getsockname()[1]
"""

# A process with a SIGALRM handler of its own that runs pytest off its main thread.
_PYTEST_OFF_THE_MAIN_THREAD = """
import signal
import sys
import threading
import pytest

signal.signal(signal.SIGALRM, lambda signum, frame: None)
exit_codes = []
args = ["-p", "no:cacheprovider", "-q", "--timeout=30", "test_inner.py"]
thread = threading.Thread(target=lambda: exit_codes.append(pytest.main(args)))
thread.start()
thread.join()
sys.exit(exit_codes[0])
"""

# A plugin that makes the report of one phase of ``test_cut`` outlast a two-second timer.
_SLOW_REPORT_OF_TEST_CUT = """
import time


def pytest_runtest_logreport(report):
    if report.when == "{phase}" and report.nodeid.endswith("test_cut"):
        time.sleep(4)
"""


def test_a_message_given_as_a_callable_is_read_when_the_wait_ends() -> None:
    seen: list[int] = []

    def looks() -> bool:
        seen.append(len(seen))
        return False

    with pytest.raises(WaitTimeoutError, match=r"saw \[0, 1"):
        wait_for(looks, deadline=0.05, interval=0.01, message=lambda: f"saw {seen}")


async def test_a_wait_for_workers_names_the_pids_that_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    async def answers(_stack: object, _port: int, _call: object) -> dict[str, Any]:
        return {"pid": 7, "state_digest": "d"}

    monkeypatch.setattr(readiness, "_unless_its_process_exits", answers)
    stack = cast(TaiStack, SimpleNamespace(app_ports=[1]))
    with pytest.raises(WaitTimeoutError, match=r"only saw \[7\] of 4 workers"):
        await readiness.wait_workers(stack, 4, deadline=0.01)


def _names_the_condition() -> bool:
    return any(said.startswith("the condition (after ") for said in waiting.running_waits())


def test_a_running_wait_is_named_with_its_last_result_until_it_ends() -> None:
    done = threading.Event()

    def looks() -> str | bool:
        return done.is_set() or ""

    thread = threading.Thread(target=wait_for, args=(looks,), kwargs={"deadline": 60.0, "message": "the condition"})
    thread.start()
    try:
        wait_for(_names_the_condition, deadline=30.0, interval=0.01)
    finally:
        done.set()
        thread.join(30.0)
    assert not _names_the_condition()


def test_a_running_wait_whose_message_raises_is_named_with_that_error() -> None:
    def message() -> str:
        raise KeyError("gone")

    said: list[str] = []

    def reads_itself() -> bool:
        said.extend(waiting.running_waits())
        return True

    wait_for(reads_itself, deadline=1.0, message=message)
    assert "a wait whose message raised KeyError('gone')" in said[0]


def test_a_message_that_raises_at_the_waits_own_deadline_raises_its_own_error() -> None:
    def message() -> str:
        raise KeyError("gone")

    with pytest.raises(KeyError, match="gone"):
        wait_for(lambda: False, deadline=0.0, message=message)


class _FalsyWithARaisingRepr:
    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        raise ValueError("no repr")


def test_a_running_wait_whose_result_has_a_repr_that_raises_keeps_its_message() -> None:
    said: list[str] = []
    results = [_FalsyWithARaisingRepr(), True]

    def reads_itself() -> object:
        said.extend(waiting.running_waits())
        return results.pop(0)

    wait_for(reads_itself, deadline=1.0, interval=0.01, message="the condition")
    assert said[1].startswith("the condition (after "), said
    assert said[1].endswith("last result: <its repr raised ValueError>)"), said


def test_a_running_wait_whose_message_raises_an_error_with_a_repr_that_raises_is_still_named() -> None:
    class ReprRaisesError(Exception):
        def __repr__(self) -> str:
            raise ValueError("no repr")

    def message() -> str:
        raise ReprRaisesError

    said: list[str] = []

    def reads_itself() -> bool:
        said.extend(waiting.running_waits())
        return True

    wait_for(reads_itself, deadline=1.0, message=message)
    assert said[0].startswith("a wait whose message raised <its repr raised ValueError> (after "), said


def test_running_waits_started_since_a_moment_leave_out_the_waits_older_than_it() -> None:
    said: list[str] = []

    def reads_the_newer_waits() -> bool:
        said.extend(waiting.running_waits(started_since=moment))
        return True

    def starts_the_newer_wait() -> bool:
        nonlocal moment
        moment = time.monotonic()
        wait_for(reads_the_newer_waits, deadline=1.0, message="the newer wait")
        return True

    moment = 0.0
    wait_for(starts_the_newer_wait, deadline=1.0, message="the older wait")
    assert len(said) == 1, said
    assert said[0].startswith("the newer wait (after "), said


def test_a_test_timeout_cutting_a_wait_fails_the_test_with_what_the_wait_saw(inner_pytest: InnerPytest) -> None:
    test = (
        "from tai42_e2e.waiting import wait_for\n"
        "def test_waits():\n"
        "    wait_for(lambda: 0, deadline=300.0, message='the awaited condition')\n"
    )
    output = inner_pytest(test, "--timeout=3")
    assert "Failed: the awaited condition (after " in output, output
    assert "last result: 0" in output, output
    assert "1 failed" in output, output


def test_a_test_timeout_cutting_an_async_wait_fails_the_test_with_what_the_wait_saw(inner_pytest: InnerPytest) -> None:
    test = (
        "import asyncio\n"
        "from tai42_e2e.waiting import wait_for_async\n"
        "async def never():\n"
        "    return False\n"
        "def test_waits():\n"
        "    asyncio.run(wait_for_async(never, deadline=300.0, message='the awaited condition'))\n"
    )
    output = inner_pytest(test, "--timeout=3")
    assert "Failed: the awaited condition (after " in output, output
    assert "1 failed" in output, output


def test_a_timeout_on_the_test_function_only_names_the_wait_it_cuts(inner_pytest: InnerPytest) -> None:
    test = (
        "import pytest\n"
        "from tai42_e2e.waiting import wait_for\n"
        "@pytest.mark.timeout(2, func_only=True)\n"
        "def test_waits():\n"
        "    wait_for(lambda: False, deadline=300.0, message='the awaited condition')\n"
    )
    output = inner_pytest(test)
    assert "Failed: the awaited condition (after " in output, output
    assert "1 failed" in output, output


def test_a_test_timeout_names_a_wait_running_on_the_readiness_thread(inner_pytest: InnerPytest) -> None:
    test = (
        "from tai42_e2e.readiness import run_readiness_coro\n"
        "from tai42_e2e.waiting import wait_for_async\n"
        "seen = [7]\n"
        "async def never():\n"
        "    return False\n"
        "def test_boots():\n"
        "    run_readiness_coro(wait_for_async(never, deadline=6.0, message=lambda: f'only saw {seen}'))\n"
    )
    output = inner_pytest(test, "--timeout=3")
    assert "Failed: only saw [7] (after " in output, output
    assert "1 failed" in output, output


def test_a_test_timeout_names_a_wait_whose_predicate_is_blocked(inner_pytest: InnerPytest) -> None:
    test = (
        "import time\n"
        "from tai42_e2e.waiting import wait_for\n"
        "def stalls():\n"
        "    time.sleep(300)\n"
        "def test_waits():\n"
        "    wait_for(stalls, deadline=300.0, message='the awaited condition')\n"
    )
    output = inner_pytest(test, "--timeout=3")
    assert "Failed: the awaited condition (after " in output, output
    assert "1 failed" in output, output


def test_a_test_timeout_outside_any_wait_names_no_wait_of_an_earlier_test(inner_pytest: InnerPytest) -> None:
    test = (
        "import time\n"
        "from tai42_e2e.waiting import wait_for\n"
        "def test_waits():\n"
        "    wait_for(lambda: False, deadline=300.0, message='the awaited condition')\n"
        "def test_sleeps():\n"
        "    time.sleep(300)\n"
    )
    output = inner_pytest(test, "--timeout=2")
    assert "Failed: the awaited condition (after " in output, output
    assert "Failed: Timeout (>2.0s) from pytest-timeout." in output, output
    assert "2 failed" in output, output


def test_a_test_timeout_names_no_wait_on_a_thread_an_earlier_test_left(inner_pytest: InnerPytest) -> None:
    test = (
        "import contextlib\n"
        "import threading\n"
        "import time\n"
        "from tai42_e2e.waiting import WaitTimeoutError, wait_for\n"
        "def keeps_starting_waits():\n"
        "    while True:\n"
        "        with contextlib.suppress(WaitTimeoutError):\n"
        "            wait_for(lambda: False, deadline=0.2, interval=0.01, message='the leftover wait')\n"
        "def test_leaves_a_thread():\n"
        "    threading.Thread(target=keeps_starting_waits, daemon=True).start()\n"
        "def test_sleeps():\n"
        "    time.sleep(300)\n"
    )
    output = inner_pytest(test, "--timeout=2")
    assert "Failed: Timeout (>2.0s) from pytest-timeout." in output, output
    assert "Failed: the leftover wait" not in output, output
    assert "1 failed, 1 passed" in output, output


def test_a_test_timeout_names_no_readiness_wait_a_thread_an_earlier_test_left_goes_on_starting(
    inner_pytest: InnerPytest,
) -> None:
    test = (
        "import contextlib\n"
        "import threading\n"
        "import time\n"
        "from tai42_e2e.readiness import run_readiness_coro\n"
        "from tai42_e2e.waiting import WaitTimeoutError, wait_for_async\n"
        "async def never():\n"
        "    return False\n"
        "def keeps_starting_waits():\n"
        "    while True:\n"
        "        with contextlib.suppress(WaitTimeoutError):\n"
        "            run_readiness_coro(wait_for_async(never, deadline=0.2, message='the leftover wait'))\n"
        "def test_leaves_a_thread():\n"
        "    threading.Thread(target=keeps_starting_waits, daemon=True).start()\n"
        "def test_sleeps():\n"
        "    time.sleep(300)\n"
    )
    output = inner_pytest(test, "--timeout=2")
    assert "Failed: Timeout (>2.0s) from pytest-timeout." in output, output
    assert "Failed: the leftover wait" not in output, output
    assert "1 failed, 1 passed" in output, output


def test_a_test_timeout_names_no_wait_a_readiness_coroutine_that_ignores_its_cancel_goes_on_starting(
    inner_pytest: InnerPytest,
) -> None:
    test = (
        "import contextlib\n"
        "import asyncio\n"
        "import time\n"
        "from tai42_e2e.readiness import run_readiness_coro\n"
        "from tai42_e2e.waiting import WaitTimeoutError, wait_for_async\n"
        "async def never():\n"
        "    return False\n"
        "async def ignores_its_cancel():\n"
        "    for _ in range(40):\n"
        "        with contextlib.suppress(WaitTimeoutError, asyncio.CancelledError):\n"
        "            await wait_for_async(never, deadline=0.2, message='the leftover wait')\n"
        "def test_cut():\n"
        "    run_readiness_coro(ignores_its_cancel())\n"
        "def test_sleeps():\n"
        "    time.sleep(300)\n"
    )
    output = inner_pytest(test, "--timeout=2")
    assert "FAILED test_inner.py::test_cut - Failed: the leftover wait" in output, output
    assert "FAILED test_inner.py::test_sleeps - Failed: Timeout" in output, output


def test_a_test_timeout_names_the_readiness_wait_of_a_thread_the_test_started(inner_pytest: InnerPytest) -> None:
    test = (
        "import threading\n"
        "from tai42_e2e.readiness import run_readiness_coro\n"
        "from tai42_e2e.waiting import wait_for_async\n"
        "async def never():\n"
        "    return False\n"
        "def waits():\n"
        "    run_readiness_coro(wait_for_async(never, deadline=6.0, message='the awaited condition'))\n"
        "def test_waits_on_its_own_thread():\n"
        "    thread = threading.Thread(target=waits, daemon=True)\n"
        "    thread.start()\n"
        "    thread.join()\n"
    )
    output = inner_pytest(test, "--timeout=2")
    assert "Failed: the awaited condition (after " in output, output
    assert "1 failed" in output, output


def test_a_test_timeout_names_no_wait_an_earlier_test_left_on_a_shared_event_loop(inner_pytest: InnerPytest) -> None:
    test = (
        "import asyncio\n"
        "import pytest\n"
        "from tai42_e2e.waiting import wait_for_async\n"
        "pytestmark = pytest.mark.asyncio(loop_scope='module')\n"
        "async def never():\n"
        "    return False\n"
        "async def test_waits():\n"
        "    await wait_for_async(never, deadline=300.0, message='the awaited condition')\n"
        "async def test_sleeps():\n"
        "    await asyncio.sleep(300)\n"
    )
    output = inner_pytest(test, "--timeout=2")
    assert "Failed: the awaited condition (after " in output, output
    assert "Failed: Timeout (>2.0s) from pytest-timeout." in output, output
    assert "2 failed" in output, output


def test_a_test_timeout_ends_the_polling_on_the_readiness_thread(inner_pytest: InnerPytest) -> None:
    test = (
        "import threading\n"
        "import pytest\n"
        "from tai42_e2e.readiness import run_readiness_coro\n"
        "from tai42_e2e.waiting import wait_for, wait_for_async\n"
        "async def never():\n"
        "    return False\n"
        "def readiness_thread_gone():\n"
        "    return not any(thread.name == 'tai-e2e-readiness' for thread in threading.enumerate())\n"
        "@pytest.mark.timeout(2)\n"
        "def test_boots():\n"
        "    run_readiness_coro(wait_for_async(never, deadline=20.0, message='the awaited condition'))\n"
        "@pytest.mark.timeout(30)\n"
        "def test_its_thread_ended():\n"
        "    wait_for(readiness_thread_gone, deadline=10.0, message='the readiness thread still polls')\n"
    )
    output = inner_pytest(test)
    assert "Failed: the awaited condition (after " in output, output
    assert "1 failed, 1 passed" in output, output


def test_a_readiness_coroutines_error_is_raised_on_the_calling_thread() -> None:
    async def fails() -> None:
        raise KeyError("gone")

    with pytest.raises(KeyError, match="gone"):
        readiness.run_readiness_coro(fails())


@pytest.mark.parametrize("error", ["SystemExit", "KeyboardInterrupt"])
def test_a_readiness_coroutine_ending_with_an_error_that_ends_its_loop_raises_it_on_the_caller_and_nowhere_else(
    inner_pytest: InnerPytest, error: str
) -> None:
    test = (
        "import threading\n"
        "import pytest\n"
        "from tai42_e2e.readiness import run_readiness_coro\n"
        "from tai42_e2e.waiting import wait_for\n"
        "async def ends_the_loop():\n"
        f"    raise {error}\n"
        "def readiness_thread_gone():\n"
        "    return not any(thread.name == 'tai-e2e-readiness' for thread in threading.enumerate())\n"
        "def test_raises():\n"
        f"    with pytest.raises({error}):\n"
        "        run_readiness_coro(ends_the_loop())\n"
        "def test_later():\n"
        "    wait_for(readiness_thread_gone, deadline=10.0, message='the readiness thread still runs')\n"
    )
    output = inner_pytest(test, "-W", "error")
    assert "2 passed" in output, output


def test_a_readiness_coroutines_cancelled_error_reaches_the_caller_as_it_is() -> None:
    async def cancelled() -> None:
        raise asyncio.CancelledError("its own")

    with pytest.raises(asyncio.CancelledError, match="its own"):
        readiness.run_readiness_coro(cancelled())


def test_a_system_exit_in_a_readiness_coroutines_child_task_reaches_the_caller() -> None:
    async def child() -> None:
        raise SystemExit(3)

    async def parent() -> None:
        await asyncio.create_task(child())

    with pytest.raises(SystemExit):
        readiness.run_readiness_coro(parent())


def test_a_readiness_coroutine_runs_for_a_caller_on_a_running_event_loop() -> None:
    ran: list[str] = []

    async def succeeds() -> None:
        ran.append("ran")

    async def caller() -> None:
        readiness.run_readiness_coro(succeeds())

    asyncio.run(caller())
    assert ran == ["ran"]


def test_a_readiness_coroutine_ignoring_its_cancel_does_not_keep_the_process_from_exiting(
    inner_pytest: InnerPytest,
) -> None:
    test = (
        "import asyncio\n"
        "import contextlib\n"
        "from tai42_e2e.readiness import run_readiness_coro\n"
        "async def ignores_its_cancel():\n"
        "    while True:\n"
        "        with contextlib.suppress(asyncio.CancelledError):\n"
        "            await asyncio.sleep(0.05)\n"
        "def test_cut():\n"
        "    run_readiness_coro(ignores_its_cancel())\n"
    )
    output = inner_pytest(test, "--timeout=2")
    assert "Failed: Timeout (>2.0s) from pytest-timeout." in output, output
    assert "1 failed" in output, output


def test_a_cut_landing_as_the_readiness_loop_closes_reaches_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    class Cut(BaseException):
        pass

    join = waiting.ThreadForCaller.join

    def join_then_cut(self: waiting.ThreadForCaller, timeout: float | None = None) -> None:
        join(self, timeout)
        raise Cut

    monkeypatch.setattr(waiting.ThreadForCaller, "join", join_then_cut)

    async def succeeds() -> None:
        pass

    with pytest.raises(Cut):
        readiness.run_readiness_coro(succeeds())


def test_a_thread_for_a_caller_that_such_a_thread_creates_is_for_the_same_caller() -> None:
    created: list[waiting.ThreadForCaller] = []
    thread = waiting.ThreadForCaller(target=lambda: created.append(waiting.ThreadForCaller()))
    thread.start()
    thread.join()
    assert created[0].caller is threading.current_thread()


def test_a_test_timeout_reads_a_timeout_failure_when_a_waits_message_and_result_raise(
    inner_pytest: InnerPytest,
) -> None:
    test = (
        "from tai42_e2e.waiting import wait_for\n"
        "class Falsy:\n"
        "    def __bool__(self):\n"
        "        return False\n"
        "    def __repr__(self):\n"
        "        raise ValueError('no repr')\n"
        "def message():\n"
        "    raise KeyError('gone')\n"
        "def test_waits():\n"
        "    wait_for(Falsy, deadline=300.0, message=message)\n"
    )
    output = inner_pytest(test, "--timeout=2")
    assert "Failed: a wait whose message raised KeyError('gone') (after " in output, output
    assert "last result: <its repr raised ValueError>): Timeout (>2.0s) from pytest-timeout." in output, output
    assert "1 failed" in output, output


def test_a_timer_firing_while_a_failed_tests_report_is_made_fails_it_and_the_run_goes_on(
    inner_pytest: InnerPytest,
) -> None:
    test = (
        "import time\n"
        "import pytest\n"
        "from tai42_e2e import diagnostics\n"
        "@pytest.fixture\n"
        "def cleaned_up():\n"
        "    yield\n"
        "    print('FIXTURE TORN DOWN')\n"
        "def test_fails(cleaned_up, monkeypatch):\n"
        "    monkeypatch.setattr(diagnostics, 'report', lambda: time.sleep(4) or 'the dump')\n"
        "    assert False, 'its own error'\n"
        "def test_later():\n"
        "    print('LATER TEST RAN')\n"
    )
    output = inner_pytest(test, "--timeout=2", "-s")
    assert "INTERNALERROR" not in output, output
    assert "its own error" in output, output
    assert "Failed: Timeout (>2.0s) from pytest-timeout." in output, output
    assert "FIXTURE TORN DOWN" in output, output
    assert "LATER TEST RAN" in output, output
    assert "1 failed, 1 passed, 1 error" in output, output


def test_a_timer_firing_before_a_tests_setup_fails_it_without_running_the_setup(
    inner_pytest: InnerPytest, tmp_path: Path
) -> None:
    (tmp_path / "conftest.py").write_text(
        "import time\n"
        "def pytest_runtest_logstart(nodeid):\n"
        "    if nodeid.endswith('test_cut'):\n"
        "        time.sleep(4)\n"
    )
    test = (
        "import pytest\n"
        "@pytest.fixture\n"
        "def set_up():\n"
        "    print('SETUP RAN')\n"
        "def test_cut(set_up):\n"
        "    pass\n"
        "def test_later():\n"
        "    print('LATER TEST RAN')\n"
    )
    output = inner_pytest(test, "--timeout=2", "-s")
    assert "INTERNALERROR" not in output, output
    assert "SETUP RAN" not in output, output
    assert "Failed: Timeout (>2.0s) from pytest-timeout." in output, output
    assert "LATER TEST RAN" in output, output
    assert "1 passed, 1 error" in output, output


def test_a_timer_firing_in_a_fixtures_teardown_cuts_it(inner_pytest: InnerPytest) -> None:
    test = (
        "import time\n"
        "import pytest\n"
        "@pytest.fixture\n"
        "def hangs_at_the_end():\n"
        "    yield\n"
        "    time.sleep(8)\n"
        "    print('THE HANG ENDED')\n"
        "def test_passes(hangs_at_the_end):\n"
        "    pass\n"
    )
    output = inner_pytest(test, "--timeout=2", "-s")
    assert "THE HANG ENDED" not in output, output
    assert "Failed: Timeout (>2.0s) from pytest-timeout." in output, output
    assert "1 passed, 1 error" in output, output


def test_a_timer_firing_while_a_setup_report_is_made_cuts_the_test_before_its_body(
    inner_pytest: InnerPytest, tmp_path: Path
) -> None:
    (tmp_path / "conftest.py").write_text(_SLOW_REPORT_OF_TEST_CUT.format(phase="setup"))
    test = "def test_cut():\n    print('BODY RAN')\ndef test_later():\n    print('LATER TEST RAN')\n"
    output = inner_pytest(test, "--timeout=2", "-s")
    assert "INTERNALERROR" not in output, output
    assert "BODY RAN" not in output, output
    assert "Failed: Timeout (>2.0s) from pytest-timeout." in output, output
    assert "LATER TEST RAN" in output, output
    assert "1 failed, 1 passed" in output, output


def test_a_timer_firing_while_a_calls_report_is_made_arrives_on_that_tests_teardown_report(
    inner_pytest: InnerPytest, tmp_path: Path
) -> None:
    (tmp_path / "conftest.py").write_text(_SLOW_REPORT_OF_TEST_CUT.format(phase="call"))
    test = (
        "import pytest\n"
        "@pytest.fixture\n"
        "def released():\n"
        "    yield\n"
        "    print('TEARDOWN RAN')\n"
        "def test_cut(released):\n"
        "    pass\n"
        "def test_later():\n"
        "    pass\n"
    )
    output = inner_pytest(test, "--timeout=2", "-s", "--junitxml=inner.xml")
    assert "TEARDOWN RAN" in output, output
    assert "ERROR at teardown of test_cut" in output, output
    assert "2 passed, 1 error" in output, output
    junit = (tmp_path / "inner.xml").read_text()
    assert 'errors="1"' in junit, junit
    assert 'tests="2"' in junit, junit


def test_a_teardowns_own_error_and_a_timeout_held_for_its_report_share_that_report(
    inner_pytest: InnerPytest, tmp_path: Path
) -> None:
    (tmp_path / "conftest.py").write_text(_SLOW_REPORT_OF_TEST_CUT.format(phase="call"))
    test = (
        "import pytest\n"
        "@pytest.fixture\n"
        "def fails_to_release():\n"
        "    yield\n"
        "    raise RuntimeError('its own teardown error')\n"
        "def test_cut(fails_to_release):\n"
        "    pass\n"
    )
    output = inner_pytest(test, "--timeout=2", "--junitxml=inner.xml")
    assert "its own teardown error" in output, output
    assert "Failed: Timeout (>2.0s) from pytest-timeout." in output, output
    assert 'tests="1"' in (tmp_path / "inner.xml").read_text()


@pytest.mark.parametrize(
    ("ending", "reads"),
    [("raise KeyboardInterrupt", "KeyboardInterrupt"), ("pytest.exit('the run is over')", "the run is over")],
)
def test_a_teardown_ending_the_run_ends_it_while_a_timeout_is_held(
    inner_pytest: InnerPytest, tmp_path: Path, ending: str, reads: str
) -> None:
    (tmp_path / "conftest.py").write_text(_SLOW_REPORT_OF_TEST_CUT.format(phase="call"))
    test = (
        "import pytest\n"
        "@pytest.fixture\n"
        "def ends_the_run():\n"
        "    yield\n"
        f"    {ending}\n"
        "def test_cut(ends_the_run):\n"
        "    pass\n"
        "def test_later():\n"
        "    print('LATER TEST RAN')\n"
    )
    output = inner_pytest(test, "--timeout=2", "-s")
    assert reads in output, output
    assert "LATER TEST RAN" not in output, output


def test_a_timer_firing_while_the_last_report_is_made_is_reported_and_the_run_goes_on(
    inner_pytest: InnerPytest, tmp_path: Path
) -> None:
    (tmp_path / "conftest.py").write_text(_SLOW_REPORT_OF_TEST_CUT.format(phase="teardown"))
    test = "def test_cut():\n    pass\ndef test_later():\n    print('LATER TEST RAN')\n"
    output = inner_pytest(test, "--timeout=2", "-s")
    assert "INTERNALERROR" not in output, output
    assert "ERROR at teardown of test_cut" in output, output
    assert "Failed: Timeout (>2.0s) from pytest-timeout." in output, output
    assert "LATER TEST RAN" in output, output
    assert "2 passed, 1 error" in output, output


def test_a_pytest_run_off_the_main_thread_leaves_the_processes_signal_handler_alone(tmp_path: Path) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_inner.py").write_text("def test_passes():\n    pass\n")
    (tmp_path / "off_the_main_thread.py").write_text(_PYTEST_OFF_THE_MAIN_THREAD)
    run = subprocess.run(
        [sys.executable, "off_the_main_thread.py"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=90,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "1 passed" in run.stdout, run.stdout


def test_a_timeout_by_thread_leaves_a_signal_handler_of_the_process_in_place(inner_pytest: InnerPytest) -> None:
    test = (
        "import signal\n"
        "def foreign(signum, frame):\n"
        "    pass\n"
        "signal.signal(signal.SIGALRM, foreign)\n"
        "def test_first():\n"
        "    assert signal.getsignal(signal.SIGALRM) is foreign\n"
        "def test_second():\n"
        "    assert signal.getsignal(signal.SIGALRM) is foreign\n"
    )
    output = inner_pytest(test, "--timeout=30", "--timeout-method=thread")
    assert "2 passed" in output, output


def test_a_passed_tests_fixture_teardown_near_its_timeout_keeps_a_waits_deadline(inner_pytest: InnerPytest) -> None:
    test = _CLOSING_LISTENER + (
        "import time\n"
        "from types import SimpleNamespace\n"
        "import pytest\n"
        "from tai42_e2e.stack import TaiStack\n"
        "@pytest.fixture\n"
        "def stack():\n"
        "    sock, port = listener()\n"
        "    ports = SimpleNamespace(allocated_ports=[port])\n"
        "    yield ports\n"
        "    threading.Timer(0.1, sock.close).start()\n"
        "    errors = TaiStack._release_ports(ports)\n"
        "    assert not errors, errors\n"
        "def test_ends_near_its_timeout(stack):\n"
        "    time.sleep(4)\n"
    )
    output = inner_pytest(test, "--timeout=5")
    assert "1 passed" in output, output
    assert "error" not in output, output


def test_a_passed_tests_finally_near_its_timeout_keeps_a_waits_deadline(inner_pytest: InnerPytest) -> None:
    test = _CLOSING_LISTENER + (
        "import time\n"
        "from tai42_e2e import ports\n"
        "from tai42_e2e.waiting import wait_for\n"
        "def test_ends_near_its_timeout():\n"
        "    sock, port = listener()\n"
        "    try:\n"
        "        time.sleep(4)\n"
        "    finally:\n"
        "        threading.Timer(0.1, sock.close).start()\n"
        "        wait_for(lambda: ports.is_free(port), deadline=5.0, message='port never freed')\n"
    )
    output = inner_pytest(test, "--timeout=5")
    assert "1 passed" in output, output


def test_a_respawn_near_a_tests_timeout_waits_for_its_port_to_free(inner_pytest: InnerPytest) -> None:
    test = _CLOSING_LISTENER + (
        "import time\n"
        "from types import SimpleNamespace\n"
        "from tai42_e2e import ports, spawning\n"
        "from tai42_e2e.supervisor import RespawnSupervisor\n"
        "def test_respawns_near_its_timeout(monkeypatch):\n"
        "    sock, port = listener()\n"
        "    free_at_spawn = []\n"
        "    monkeypatch.setattr(spawning, 'spawn', lambda stack, spec: free_at_spawn.append(ports.is_free(port)))\n"
        "    stack = SimpleNamespace(procs={}, specs={'serve': object()}, ports_for=lambda name: [port])\n"
        "    respawn = threading.Thread(target=RespawnSupervisor(stack)._respawn, args=('serve',))\n"
        "    time.sleep(4)\n"
        "    respawn.start()\n"
        "    threading.Timer(0.1, sock.close).start()\n"
        "    respawn.join()\n"
        "    assert free_at_spawn == [True]\n"
    )
    output = inner_pytest(test, "--timeout=5")
    assert "1 passed" in output, output


def test_a_fixture_cleaning_up_a_failed_boot_runs_its_cleanup_to_the_end(inner_pytest: InnerPytest) -> None:
    test = (
        "import contextlib\n"
        "import time\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "import httpx\n"
        "import pytest\n"
        "from tai42_e2e.marketplace import MarketplaceService\n"
        "class Service(MarketplaceService):\n"
        "    def __init__(self, root):\n"
        "        self._infra = SimpleNamespace(settings=SimpleNamespace(boot_timeout=300.0))\n"
        "        self.host, self.port, self.root = '127.0.0.1', 1, root\n"
        "        self._logs_dir = root / 'logs'\n"
        "        self._procs, self._specs = {}, {}\n"
        "    def _apply_ddl(self):\n"
        "        pass\n"
        "    def _spawn_all(self):\n"
        "        pass\n"
        "    def _bin(self):\n"
        "        return 'unused'\n"
        "    def teardown(self):\n"
        "        time.sleep(2)\n"
        "        print('TEARDOWN FINISHED')\n"
        "def refused(*args, **kwargs):\n"
        "    raise httpx.ConnectError('refused')\n"
        "@pytest.fixture\n"
        "def service(tmp_path, monkeypatch):\n"
        "    monkeypatch.setattr(httpx, 'get', refused)\n"
        "    mp = Service(tmp_path)\n"
        "    try:\n"
        "        mp.boot()\n"
        "    except BaseException:\n"
        "        with contextlib.suppress(Exception):\n"
        "            mp.teardown()\n"
        "        raise\n"
        "    yield mp\n"
        "def test_x(service):\n"
        "    pass\n"
    )
    output = inner_pytest(test, "--timeout=3", "-s")
    assert "TEARDOWN FINISHED" in output, output
    assert "Failed: marketplace registry never became ready" in output, output
    assert "1 error" in output, output


def test_a_stack_context_in_a_test_body_runs_its_teardown_to_the_end(inner_pytest: InnerPytest) -> None:
    test = (
        "import time\n"
        "from tai42_e2e.stack import TaiStack\n"
        "from tai42_e2e.waiting import wait_for\n"
        "class Booted(TaiStack):\n"
        "    def __init__(self):\n"
        "        pass\n"
        "    def boot(self):\n"
        "        pass\n"
        "    def teardown(self):\n"
        "        time.sleep(2)\n"
        "        print('TEARDOWN FINISHED')\n"
        "def test_body():\n"
        "    with Booted():\n"
        "        wait_for(lambda: False, deadline=300.0, message='the awaited condition')\n"
    )
    output = inner_pytest(test, "--timeout=3", "-s")
    assert "TEARDOWN FINISHED" in output, output
    assert "Failed: the awaited condition (after " in output, output
    assert "1 failed" in output, output


def test_a_stack_whose_boot_is_cut_runs_its_teardown_to_the_end(inner_pytest: InnerPytest) -> None:
    test = (
        "import time\n"
        "from tai42_e2e import diagnostics\n"
        "from tai42_e2e.stack import TaiStack\n"
        "from tai42_e2e.waiting import wait_for\n"
        "class SlowToEnd(TaiStack):\n"
        "    def __init__(self):\n"
        "        pass\n"
        "    def _start(self):\n"
        "        wait_for(lambda: False, deadline=300.0, message='the stack never came up')\n"
        "    def teardown(self):\n"
        "        time.sleep(2)\n"
        "        print('TEARDOWN FINISHED')\n"
        "def test_boots(monkeypatch):\n"
        "    monkeypatch.setattr(diagnostics, 'stack_report', lambda stack: 'the stack report')\n"
        "    with SlowToEnd():\n"
        "        pass\n"
    )
    output = inner_pytest(test, "--timeout=3", "-s")
    assert "TEARDOWN FINISHED" in output, output
    assert "Failed: the stack never came up (after " in output, output
    assert "1 failed" in output, output


def test_a_finally_after_a_cut_wait_keeps_its_waits_deadline_and_runs_to_the_end(inner_pytest: InnerPytest) -> None:
    test = (
        "import time\n"
        "from tai42_e2e.waiting import wait_for\n"
        "def test_body():\n"
        "    try:\n"
        "        wait_for(lambda: False, deadline=300.0, message='the awaited condition')\n"
        "    finally:\n"
        "        started = time.monotonic()\n"
        "        wait_for(lambda: time.monotonic() - started > 2, deadline=30.0, message='the cleanup condition')\n"
        "        print('CLEANUP FINISHED')\n"
    )
    output = inner_pytest(test, "--timeout=3", "-s")
    assert "CLEANUP FINISHED" in output, output
    assert "Failed: the awaited condition (after " in output, output
    assert "1 failed" in output, output
