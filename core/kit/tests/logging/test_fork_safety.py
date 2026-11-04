"""Logging's fork safety: a child forked while another thread is inside a stream's write gets its own streams.

Every test that forks runs its scenario (``fork_scenarios.py``) in its own interpreter, killed at
its bound, so a fork that never completes, in the parent or in the child, fails the test instead of
hanging the suite. Each scenario forks through one door while a writer thread is provably blocked
inside a pipe stream's ``write``, holding that stream's buffer lock.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tai42_kit.fork_gate import ForkGate
from tai42_kit.logging import LoggingSettings, fork_safety, setup_logging
from tai42_kit.logging.fork_report_text import _headline
from tai42_kit.logging.fork_safety import install_logging_fork_safety

_SCENARIOS = Path(__file__).with_name("fork_scenarios.py")
# The bound one scenario's interpreter has to finish in (generous: each finishes in well under a second).
_BOUND = 60.0
# Budget the guard-order test gives the fork guard's wait.
_GUARD_WAIT = 10.0


def _run_scenario(name: str, *arguments: str) -> dict[str, object]:
    """Run scenario ``name`` with ``arguments`` in its own interpreter within the bound and return what it observed."""
    try:
        # On timeout ``subprocess.run`` kills the interpreter and reaps it.
        done = subprocess.run(
            [sys.executable, str(_SCENARIOS), name, *arguments],
            capture_output=True,
            text=True,
            timeout=_BOUND,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"the fork scenario {name} did not finish within {_BOUND}s")
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


def _assert_the_child_logged(result: dict[str, object], *, prints: int = 0) -> None:
    """The child exited 0 with its line written once, and the parent's line held at the fork was written once, whole."""
    assert result["child_exit"] == 0, (
        "the child hung at its log line and was killed" if result["child_exit"] is None else result
    )
    assert result["child_lines"] == 1
    assert result["child_prints"] == prints
    assert result["writer_finished"]
    assert result["payload_written_once"], "the line the parent was writing at the fork was lost or duplicated"


def test_a_child_forked_while_another_thread_holds_its_streams_lock_logs_and_exits():
    _assert_the_child_logged(_run_scenario("a_child_os_fork_makes_logs_and_exits"))


def test_a_child_that_finalises_through_sys_exit_flushes_the_stderr_another_thread_held():
    _assert_the_child_logged(_run_scenario("a_child_os_fork_makes_logs_prints_and_finalises"), prints=1)


def test_a_preexec_fn_logs_through_a_stream_another_thread_held_and_popen_returns():
    _assert_the_child_logged(_run_scenario("a_subprocess_logs_from_its_preexec_fn"))


def test_a_fork_context_process_logs_through_a_stream_another_thread_held_and_exits():
    _assert_the_child_logged(_run_scenario("a_fork_context_process_logs_and_returns"))


def test_a_child_logs_through_last_resort_while_another_thread_held_stderr():
    _assert_the_child_logged(_run_scenario("a_child_logs_through_last_resort"))


def _assert_the_child_reported(result: dict[str, object], failure: str) -> None:
    """The child, which writes nothing, exited 0, and the hook's one report holds one failure matching ``failure``.

    The parent's line held at the fork is written once, whole.
    """
    assert result["child_exit"] == 0, "the child hung and was killed" if result["child_exit"] is None else result
    assert result["writer_finished"]
    assert result["payload_written_once"], "the line the parent was writing at the fork was lost or duplicated"
    reports = result["reports"]
    assert isinstance(reports, list)
    [report] = reports
    assert isinstance(report, str)
    headline, *failures = report.splitlines()
    assert headline == _headline(fork_safety.__name__)
    [reported] = failures
    assert re.fullmatch(failure, reported), reported


_BYTE_ORDER_MARK_STDERR = (
    r"  TypeError: sys\.stderr, .*: not rebuilt, .* starts with a byte-order mark the parent may already have written"
)


def test_a_child_forked_while_another_thread_holds_a_stderr_it_cannot_rebuild_reports_it_and_exits():
    _assert_the_child_reported(
        _run_scenario("a_child_os_fork_exits_while_another_thread_holds_a_stderr_it_cannot_rebuild"),
        _BYTE_ORDER_MARK_STDERR,
    )


def test_popen_with_a_preexec_fn_returns_while_another_thread_holds_a_stderr_it_cannot_rebuild():
    _assert_the_child_reported(
        _run_scenario("a_subprocess_starts_while_another_thread_holds_a_stderr_it_cannot_rebuild"),
        _BYTE_ORDER_MARK_STDERR,
    )


_OVER_A_SOCKET = (
    r"  TypeError: the StreamHandler named None: not rebuilt, its buffered writer is a BufferedWriter over a SocketIO"
)


@pytest.mark.parametrize(
    "scenario",
    [
        "a_child_os_fork_exits_while_another_thread_holds_the_buffer_beneath_stderr",
        "a_subprocess_starts_while_another_thread_holds_the_buffer_beneath_stderr",
    ],
)
@pytest.mark.parametrize("held", ["STDERR_BUFFER_THROUGH_A_CODECS_WRITER", "STDERR_BUFFER_THROUGH_A_PROXY"])
def test_a_fork_while_another_thread_writes_through_a_stderr_of_no_standard_shape_reports_and_returns(
    scenario: str, held: str
):
    _assert_the_child_reported(_run_scenario(scenario, held), _OVER_A_SOCKET)


def test_install_registers_the_fork_hooks_once(monkeypatch):
    registered: list[dict[str, object]] = []
    monkeypatch.setattr(fork_safety, "_installed", False)
    monkeypatch.setattr(os, "register_at_fork", lambda **hooks: registered.append(hooks))

    install_logging_fork_safety()
    install_logging_fork_safety()

    assert registered == [{"after_in_child": fork_safety._after_in_child}]


def test_the_fork_guard_registers_its_hooks_after_the_logging_hooks(monkeypatch):
    # Child hooks run in registration order, so the child's streams are rebuilt before the
    # guard's child hook runs.
    registered: list[object] = []
    monkeypatch.setattr(fork_safety, "_installed", False)
    monkeypatch.setattr(os, "register_at_fork", lambda **hooks: registered.append(hooks["after_in_child"]))
    gate = ForkGate()

    try:
        gate.guard_forks(timeout=_GUARD_WAIT)
    finally:
        gate._fork_hooks.guard = None

    assert registered == [fork_safety._after_in_child, gate._fork_hooks.after_in_child]


@pytest.fixture
def root_logger_restored():
    """Snapshot the root logger; setup_logging (force=True) replaces its handlers."""
    root = logging.getLogger()
    level, handlers = root.level, root.handlers[:]
    try:
        yield root
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


def test_setup_logging_registers_the_fork_hooks_once_across_repeat_calls(monkeypatch, root_logger_restored):
    registered: list[dict[str, object]] = []
    monkeypatch.setattr(fork_safety, "_installed", False)
    monkeypatch.setattr(os, "register_at_fork", lambda **hooks: registered.append(hooks))

    setup_logging(LoggingSettings(log_level="INFO"))
    setup_logging(LoggingSettings(log_level="INFO"))

    assert registered == [{"after_in_child": fork_safety._after_in_child}]
