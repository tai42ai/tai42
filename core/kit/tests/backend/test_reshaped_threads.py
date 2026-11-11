"""asyncio's future chaining bound to the threads a process prelude reshaped.

Under a prelude that runs the process's threads on one OS thread, every bridge that chains a future
to a loop must still wake that loop when another thread completes or cancels it. Each reshaped case
runs in a fresh process: it applies the prelude first (the two thread-reshaping preludes the backend
plugins declare), binds the chaining, and runs a host loop with no timers, so a missed wake-up is a
process that never finishes, which the bound on the subprocess turns into a failure. The real-thread
cases run in this unpatched process with the chaining bound for the test.
"""

from __future__ import annotations

import asyncio
import asyncio.futures
import concurrent.futures
import json
import threading
import time

import pytest

from tai42_kit.backend.reshaped_threads import bind_future_chaining, future_chaining_bound

from .preludes import RESHAPING_PRELUDES, run_reshaped

# The reshaped process: run the prelude named in argv[1], bind the chaining, then run the door
# named in argv[2] and print its outcome. Nothing on the host loop arms a timer, so only the
# chaining's own hand-off can wake it.
_RESHAPED = """
import importlib
import sys

prelude, door = sys.argv[1:3]
module_name, _, callable_name = prelude.partition(":")
getattr(importlib.import_module(module_name), callable_name)()

import asyncio
import concurrent.futures
import json
import threading
import time

from tai42_kit.backend.reshaped_threads import bind_future_chaining

bind_future_chaining()


def work():
    time.sleep(0.1)
    return "worked"


async def to_thread():
    return await asyncio.to_thread(work)


async def run_in_executor():
    return await asyncio.get_running_loop().run_in_executor(None, work)


async def wrap_future():
    bridge = concurrent.futures.Future()

    def complete():
        time.sleep(0.1)
        bridge.set_result("bridged")

    threading.Thread(target=complete, daemon=True).start()
    return await asyncio.wrap_future(bridge)


async def cancel_from_another_thread():
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    running = threading.Event()
    tasks = []

    async def wait_forever():
        tasks.append(asyncio.current_task())
        started.set()
        running.set()
        await asyncio.Event().wait()

    def submit_then_cancel():
        submitted = asyncio.run_coroutine_threadsafe(wait_forever(), loop)
        running.wait()
        # Cancel once the loop has gone back to waiting in its selector.
        time.sleep(0.1)
        submitted.cancel()

    threading.Thread(target=submit_then_cancel, daemon=True).start()
    await started.wait()
    try:
        await tasks[0]
    except asyncio.CancelledError:
        return "cancelled"
    return "not cancelled"


def own_loop_on_a_thread():
    outcome = []
    thread = threading.Thread(target=lambda: outcome.append(asyncio.run(to_thread())))
    thread.start()
    thread.join()
    return outcome[0]


DOORS = {
    "to_thread": lambda: asyncio.run(to_thread()),
    "run_in_executor": lambda: asyncio.run(run_in_executor()),
    "wrap_future": lambda: asyncio.run(wrap_future()),
    "cancel_from_another_thread": lambda: asyncio.run(cancel_from_another_thread()),
    "own_loop_on_a_thread": own_loop_on_a_thread,
}
print(json.dumps(DOORS[door]()), flush=True)
"""


@pytest.mark.parametrize(
    ("door", "outcome"),
    [
        ("to_thread", "worked"),
        ("run_in_executor", "worked"),
        ("wrap_future", "bridged"),
        ("cancel_from_another_thread", "cancelled"),
        ("own_loop_on_a_thread", "worked"),
    ],
)
@pytest.mark.parametrize("prelude", RESHAPING_PRELUDES)
def test_a_future_another_reshaped_thread_completes_or_cancels_wakes_its_loop(
    prelude: str, door: str, outcome: str
) -> None:
    completed = run_reshaped(_RESHAPED, prelude, door)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == outcome, completed.stderr


def _work() -> str:
    time.sleep(0.05)
    return "worked"


@pytest.mark.usefixtures("bound_future_chaining")
async def test_under_real_threads_every_bridge_completes() -> None:
    loop = asyncio.get_running_loop()
    bridge: concurrent.futures.Future[str] = concurrent.futures.Future()
    threading.Timer(0.05, bridge.set_result, args=("bridged",)).start()

    assert await asyncio.to_thread(_work) == "worked"
    assert await loop.run_in_executor(None, _work) == "worked"
    assert await asyncio.wrap_future(bridge) == "bridged"


@pytest.mark.usefixtures("bound_future_chaining")
async def test_under_real_threads_a_coroutine_submitted_from_another_thread_returns_its_result() -> None:
    loop = asyncio.get_running_loop()

    async def answer() -> str:
        return "answered"

    submitted = await asyncio.to_thread(asyncio.run_coroutine_threadsafe, answer(), loop)

    assert await asyncio.wrap_future(submitted) == "answered"


@pytest.mark.usefixtures("bound_future_chaining")
async def test_under_real_threads_a_cancel_from_another_thread_reaches_the_task_through_its_loop() -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []

    async def wait_forever() -> None:
        current = asyncio.current_task()
        assert current is not None
        tasks.append(current)
        started.set()
        await asyncio.Event().wait()

    submitted = asyncio.run_coroutine_threadsafe(wait_forever(), loop)
    await started.wait()
    await asyncio.to_thread(submitted.cancel)

    with pytest.raises(asyncio.CancelledError):
        await tasks[0]
    assert submitted.cancelled()


@pytest.mark.usefixtures("bound_future_chaining")
async def test_a_cancel_on_the_loop_s_own_thread_cancels_the_task_at_once() -> None:
    loop = asyncio.get_running_loop()
    tasks: list[asyncio.Task[None]] = []

    async def wait_forever() -> None:
        current = asyncio.current_task()
        assert current is not None
        tasks.append(current)
        await asyncio.Event().wait()

    submitted = asyncio.run_coroutine_threadsafe(wait_forever(), loop)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    (task,) = tasks

    submitted.cancel()

    assert task.cancelling() == 1
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.usefixtures("bound_future_chaining")
@pytest.mark.parametrize("which", ["source", "destination"])
def test_chaining_refuses_what_is_not_a_future(which: str) -> None:
    future: concurrent.futures.Future[None] = concurrent.futures.Future()
    source, destination = (object(), future) if which == "source" else (future, object())

    with pytest.raises(TypeError, match=f"A future is required for {which} argument"):
        asyncio.futures._chain_future(source, destination)  # type: ignore[attr-defined]


@pytest.mark.usefixtures("bound_future_chaining")
async def test_a_result_for_a_closed_loop_is_dropped() -> None:
    closed = asyncio.new_event_loop()
    closed.close()
    destination = closed.create_future()
    source: concurrent.futures.Future[str] = concurrent.futures.Future()
    asyncio.futures._chain_future(source, destination)  # type: ignore[attr-defined]

    await asyncio.to_thread(source.set_result, "late")

    assert not destination.done()


def test_binding_installs_the_platform_s_chaining_once(monkeypatch: pytest.MonkeyPatch) -> None:
    stock = asyncio.futures._chain_future  # type: ignore[attr-defined]
    monkeypatch.setattr(asyncio.futures, "_chain_future", stock)
    assert not future_chaining_bound()

    bind_future_chaining()
    bound = asyncio.futures._chain_future  # type: ignore[attr-defined]
    bind_future_chaining()

    assert future_chaining_bound()
    assert bound is not stock
    assert asyncio.futures._chain_future is bound  # type: ignore[attr-defined]


def test_binding_over_a_chaining_something_else_installed_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def foreign(source: object, destination: object) -> None:
        raise AssertionError("never chained")

    monkeypatch.setattr(asyncio.futures, "_chain_future", foreign)

    with pytest.raises(RuntimeError, match=r"cannot bind asyncio's future chaining: .*foreign.*test_reshaped_threads"):
        bind_future_chaining()
    assert asyncio.futures._chain_future is foreign  # type: ignore[attr-defined]
    assert not future_chaining_bound()
