"""How a test's timeout fails it, whenever in the test's run it fires: the failure lands on one of that test's reports,
never on pytest itself, and names the waits this test was running. These hooks wrap pytest-timeout's signal method."""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable, Collection, Generator, Iterator
from types import FrameType

import pytest

from tai42_e2e import waiting

# Whether a test's setup, call or teardown is running. Only there does pytest turn a raised failure into that test's
# report; a raise anywhere else in a test's run, as in the making of a report, ends the whole pytest run.
_PHASE_RUNNING = pytest.StashKey[bool]()
# A timeout's failure from a moment no phase was running, until a test report carries it.
_HELD_TIMEOUT = pytest.StashKey[pytest.fail.Exception]()


def _message_naming_running_waits(
    message: str | None, armed: float, older_threads: Collection[threading.Thread]
) -> str | None:
    """A timeout's message with what the running waits saw ahead of it: those started since it was ``armed`` and not on
    a thread older than it. A task an earlier test left on a shared event loop runs on this test's own thread, so a new
    wait it starts is not told apart and is named."""
    running = waiting.running_waits(started_since=armed, not_on=older_threads)
    return f"{'; '.join(running)}: {message}" if running else message


def _raising_or_holding_the_timeout(
    item: pytest.Item,
    fire: Callable[[int, FrameType | None], object],
    armed: float,
    older_threads: Collection[threading.Thread],
) -> Callable[[int, FrameType | None], None]:
    """A timeout's signal handler. Its failure stays a pytest failure, a ``BaseException``, so no cleanup's ``except``
    catches it as it would a wait's error. It is raised while a phase of the test runs; between phases it is held for
    the test's next report."""

    def handler(signum: int, frame: FrameType | None) -> None:
        __tracebackhide__ = True
        try:
            fire(signum, frame)
        except pytest.fail.Exception as fired:
            message = _message_naming_running_waits(fired.msg, armed, older_threads)
            failure = pytest.fail.Exception(message, pytrace=fired.pytrace)
            if item.stash.get(_PHASE_RUNNING, False):
                raise failure from None
            item.stash[_HELD_TIMEOUT] = failure

    return handler


@pytest.hookimpl(hookwrapper=True, optionalhook=True)
def pytest_timeout_set_timer(item: pytest.Item) -> Iterator[None]:
    """Make the timeout starting now name the waits it cuts, when it fires as a signal: the handler it set is what
    says so. Its other method (``thread``), which is also what a signal timeout becomes off the main thread, sets no
    handler and ends the whole process with a stack dump, leaving no failure to name them in, and any handler the
    process has stays as it is."""
    armed = time.monotonic()
    older_threads = set(threading.enumerate()) - {threading.current_thread()}
    before = signal.getsignal(signal.SIGALRM)
    yield
    fire = signal.getsignal(signal.SIGALRM)
    if fire is not before and callable(fire):
        signal.signal(signal.SIGALRM, _raising_or_holding_the_timeout(item, fire, armed, older_threads))


def _raise_held_timeout(item: pytest.Item) -> None:
    held = item.stash.get(_HELD_TIMEOUT, None)
    if held is not None:
        del item.stash[_HELD_TIMEOUT]
        raise held


def _phase(item: pytest.Item) -> Generator[None, object, object]:
    """The innermost wrapper of a phase's hook: every other plugin's wrapper has begun, so a held timeout raised on the
    way in skips only the phase's own work, and those wrappers end as they expect."""
    item.stash[_PHASE_RUNNING] = True
    try:
        return (yield)
    finally:
        item.stash[_PHASE_RUNNING] = False


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_setup(item: pytest.Item) -> Generator[None, object, object]:
    """A setup a held timeout has overtaken fails without running."""
    _raise_held_timeout(item)
    return (yield from _phase(item))


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    """A call a held timeout has overtaken fails without running."""
    _raise_held_timeout(item)
    return (yield from _phase(item))


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_teardown(item: pytest.Item) -> Generator[None, object, object]:
    """A teardown runs to its end whatever timeout is held, as it releases what the test holds, and its report then
    carries that timeout, with the teardown's own error chained under it. What ends the whole run still ends it."""
    try:
        result = yield from _phase(item)
    except (KeyboardInterrupt, pytest.exit.Exception):
        raise
    except BaseException:
        _raise_held_timeout(item)
        raise
    _raise_held_timeout(item)
    return result


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, object, object]:
    """Report a timeout that fired once the teardown had ended, as one more teardown error. Outermost, so that no timer
    of this test can fire after it."""
    result = yield
    if item.stash.get(_HELD_TIMEOUT, None) is not None:
        call = pytest.CallInfo.from_call(lambda: _raise_held_timeout(item), when="teardown")
        report = item.ihook.pytest_runtest_makereport(item=item, call=call)
        item.ihook.pytest_runtest_logreport(report=report)
    return result
