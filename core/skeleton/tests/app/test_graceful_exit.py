"""The graceful-exit primitive.

It delivers SIGTERM to the worker process it names, so that process's already-installed
graceful handler (uvicorn's for a serve worker, the backend's drain for a backend worker)
finishes in-flight work before a clean exit — never a hard kill. The pid it takes is the
bus member's, so a forked job child signals the member it acts for.
"""

from __future__ import annotations

import signal

import pytest

import tai42_skeleton.app.graceful_exit as graceful_exit
from tai42_skeleton.app.graceful_exit import request_graceful_exit


@pytest.fixture
def kill_recorder(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Capture ``os.kill`` instead of actually signalling a process."""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(graceful_exit.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    return calls


def test_the_primitive_delivers_sigterm_to_the_named_worker_pid(kill_recorder: list[tuple[int, int]]) -> None:
    request_graceful_exit(4242)
    assert kill_recorder == [(4242, signal.SIGTERM)]
