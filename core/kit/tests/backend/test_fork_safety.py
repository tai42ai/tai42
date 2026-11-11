"""A forking backend's fork guard: the gate's span around every fork; in each child, an evicted monitoring writer
and, in a process whose threads are OS threads, the compute pool the parent shares."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.backend import PROCESS_PRELUDE_ENV

from tai42_kit.backend import fork_safety
from tai42_kit.compute import ComputePoolClient, compute_pool, stop_compute_pool
from tai42_kit.fork_gate import ForkGate

from .preludes import RESHAPING_PRELUDES, run_reshaped

_WAIT = 5.0


@pytest.fixture(autouse=True)
def _no_prelude(monkeypatch: pytest.MonkeyPatch) -> None:
    """This process names no prelude, as a process whose threads are OS threads does."""
    monkeypatch.delenv(PROCESS_PRELUDE_ENV, raising=False)


@pytest.fixture
def writer() -> Iterator[Mock]:
    """The app's active monitoring writer, a mock that notes its shutdowns."""
    active = Mock()
    with tai42_app.bound(SimpleNamespace(monitoring=SimpleNamespace(active=SimpleNamespace(writer=active)))):
        yield active


@pytest.fixture
def gate() -> Iterator[ForkGate]:
    """A private gate per test; its guard is dropped afterwards, so its registered hooks do nothing."""
    private = ForkGate()
    yield private
    private._fork_hooks.guard = None


@pytest.fixture
def shares(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """The compute pool shares the guard asks for, noted instead of made, so this process never shares."""
    asked: list[None] = []
    monkeypatch.setattr(fork_safety, "share_compute_pool", lambda: asked.append(None))
    return asked


@pytest.fixture
def registrations(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Callable[[], None]]]:
    """The fork hooks registered with the interpreter, noted instead of registered."""
    registered: list[dict[str, Callable[[], None]]] = []
    monkeypatch.setattr(os, "register_at_fork", lambda **hooks: registered.append(hooks))
    return registered


def test_the_eviction_shuts_the_inherited_writer_down_and_says_so(
    writer: Mock, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=fork_safety.__name__):
        fork_safety.evict_inherited_monitoring()
    writer.shutdown.assert_called_once_with()
    assert any("evicting the inherited monitoring client" in record.getMessage() for record in caplog.records)


def test_every_child_evicts_the_writer_once_its_gate_is_reset(
    writer: Mock, gate: ForkGate, registrations: list[dict[str, Any]], shares: list[None]
) -> None:
    fork_safety.guard_forks(gate, timeout=_WAIT)
    child_hooks = [hooks["after_in_child"] for hooks in registrations]
    assert gate._fork_hooks.after_in_child in child_hooks
    assert gate._fork_hooks.guard is not None
    assert gate._fork_hooks.guard.after_in_child is fork_safety.evict_inherited_monitoring

    with gate.job_span(timeout=_WAIT):
        gate._fork_hooks.after_in_child()
        assert gate.live_spans == 0
    writer.shutdown.assert_called_once_with()


def test_installing_the_same_guard_again_registers_nothing_more(
    writer: Mock, gate: ForkGate, registrations: list[dict[str, Any]], shares: list[None]
) -> None:
    fork_safety.guard_forks(gate, timeout=_WAIT)
    registered = len(registrations)
    fork_safety.guard_forks(gate, timeout=_WAIT)
    assert len(registrations) == registered

    gate._fork_hooks.after_in_child()
    writer.shutdown.assert_called_once_with()


def test_installing_another_guard_is_refused(
    gate: ForkGate, registrations: list[dict[str, Any]], shares: list[None]
) -> None:
    fork_safety.guard_forks(gate, timeout=_WAIT)
    with pytest.raises(RuntimeError, match="already guards forks"):
        fork_safety.guard_forks(gate, timeout=_WAIT + 1)


def test_installing_the_guard_shares_the_compute_pool_first(
    gate: ForkGate, registrations: list[dict[str, Any]], shares: list[None]
) -> None:
    order: list[str] = []
    real_guard = gate.guard_forks

    def guarded(**kwargs: Any) -> None:
        order.append(f"guard after {len(shares)} share(s)")
        real_guard(**kwargs)

    gate.guard_forks = guarded  # type: ignore[method-assign]
    fork_safety.guard_forks(gate, timeout=_WAIT)

    assert order == ["guard after 1 share(s)"]


def test_a_process_started_through_a_prelude_asks_the_share_to_decide_and_is_still_guarded(
    gate: ForkGate, registrations: list[dict[str, Any]], shares: list[None], monkeypatch: pytest.MonkeyPatch
) -> None:
    # That such a share shares nothing is the next test's, under every reshaping prelude.
    monkeypatch.setenv(PROCESS_PRELUDE_ENV, "a.prelude:apply")

    fork_safety.guard_forks(gate, timeout=_WAIT)

    assert shares == [None]
    assert gate._fork_hooks.guard is not None
    assert gate._fork_hooks.guard.after_in_child is fork_safety.evict_inherited_monitoring


# A process started through the prelude in argv[1], which names it in its environment: inside its
# running loop it installs the fork guard, then asks for a share directly; it prints what it saw.
_GUARDED_UNDER_A_PRELUDE = """
import importlib
import sys

module_name, _, callable_name = sys.argv[1].partition(":")
getattr(importlib.import_module(module_name), callable_name)()

import asyncio
import json

from tai42_kit.backend import fork_safety
from tai42_kit.backend.reshaped_threads import bind_future_chaining
from tai42_kit.compute import share_compute_pool
from tai42_kit.compute import process_pool
from tai42_kit.fork_gate import ForkGate

bind_future_chaining()


async def main():
    gate = ForkGate()
    fork_safety.guard_forks(gate, timeout=5.0)
    seen = {"shared": process_pool._share is not None, "guarded": gate._fork_hooks.guard is not None}
    share_compute_pool()
    seen["shared_on_a_direct_call"] = process_pool._share is not None
    return seen


print(json.dumps(asyncio.run(main())), flush=True)
"""


@pytest.mark.parametrize("prelude", RESHAPING_PRELUDES)
def test_under_a_prelude_neither_the_guard_nor_a_direct_call_shares_the_pool(prelude: str) -> None:
    completed = run_reshaped(_GUARDED_UNDER_A_PRELUDE, prelude, env={PROCESS_PRELUDE_ENV: prelude})

    assert completed.returncode == 0, completed.stderr
    assert "Exception in thread" not in completed.stderr, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "shared": False,
        "guarded": True,
        "shared_on_a_direct_call": False,
    }, completed.stderr


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_guard_forks_shares_the_processs_compute_pool_with_every_forked_child(
    writer: Mock, gate: ForkGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMPUTE_WORKERS", "1")
    stop_compute_pool()  # the guarded process forked below inherits no running pool

    pid = os.fork()
    if pid == 0:  # pragma: no cover - the guarded process, which exits through os._exit
        code = 1
        try:
            code = _guarded_process(gate)
        finally:
            os._exit(code)

    assert _exit_code_within(pid, 30) == 0


def _guarded_process(gate: ForkGate) -> int:  # pragma: no cover - runs in a forked process
    """Install the guard, then check a forked child runs its job in this process's worker; 0 when it does."""
    fork_safety.guard_forks(gate, timeout=_WAIT)
    try:
        worker = asyncio.run(compute_pool().run(os.getpid))
        child = os.fork()
        if child == 0:
            code = 1
            try:
                reaches = isinstance(compute_pool(), ComputePoolClient)
                code = 0 if reaches and asyncio.run(compute_pool().run(os.getpid)) == worker else 2
            finally:
                os._exit(code)
        return _exit_code_within(child, 20)
    finally:
        stop_compute_pool()


def _exit_code_within(pid: int, seconds: float) -> int:
    """Reap ``pid``; past ``seconds`` it is killed and its exit code is the kill's."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.01)
    os.kill(pid, signal.SIGKILL)
    return os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
