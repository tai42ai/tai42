"""Graceful self-exit primitives.

Each primitive delivers SIGTERM to its own process so the already-installed graceful
handler (uvicorn's for serve, the run_backend/plugin handler for backend) drains
in-flight work before a clean exit — never a hard kill. ``graceful_exit_for`` dispatches
by origin kind; the serve primitive is imported by the profile-apply response.
"""

from __future__ import annotations

import os
import signal
from typing import cast

import pytest

import tai42_skeleton.app.graceful_exit as graceful_exit
from tai42_skeleton.app.bus import WorkerKind
from tai42_skeleton.app.graceful_exit import (
    graceful_exit_for,
    request_backend_graceful_exit,
    request_serve_graceful_exit,
)


@pytest.fixture
def kill_recorder(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Capture ``os.kill`` instead of actually signalling this test process."""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(graceful_exit.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    return calls


def test_serve_primitive_self_delivers_sigterm(kill_recorder: list[tuple[int, int]]) -> None:
    request_serve_graceful_exit()
    assert kill_recorder == [(os.getpid(), signal.SIGTERM)]


def test_backend_primitive_self_delivers_sigterm(kill_recorder: list[tuple[int, int]]) -> None:
    request_backend_graceful_exit()
    assert kill_recorder == [(os.getpid(), signal.SIGTERM)]


def test_dispatch_by_kind() -> None:
    assert graceful_exit_for(WorkerKind.serve) is request_serve_graceful_exit
    assert graceful_exit_for(WorkerKind.backend) is request_backend_graceful_exit


def test_dispatch_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="graceful-exit primitive"):
        graceful_exit_for(cast("WorkerKind", "metrics"))
