"""A forking backend's fork guard: the gate's span around every fork, and an evicted monitoring writer in each child."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from tai42_contract.app import tai42_app

from tai42_kit.backend import fork_safety
from tai42_kit.fork_gate import ForkGate

_WAIT = 5.0


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
    writer: Mock, gate: ForkGate, registrations: list[dict[str, Any]]
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
    writer: Mock, gate: ForkGate, registrations: list[dict[str, Any]]
) -> None:
    fork_safety.guard_forks(gate, timeout=_WAIT)
    registered = len(registrations)
    fork_safety.guard_forks(gate, timeout=_WAIT)
    assert len(registrations) == registered

    gate._fork_hooks.after_in_child()
    writer.shutdown.assert_called_once_with()


def test_installing_another_guard_is_refused(gate: ForkGate, registrations: list[dict[str, Any]]) -> None:
    fork_safety.guard_forks(gate, timeout=_WAIT)
    with pytest.raises(RuntimeError, match="already guards forks"):
        fork_safety.guard_forks(gate, timeout=_WAIT + 1)
