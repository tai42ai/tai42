"""Fixtures the lifecycle suite shares: a bound app, and clean chain/runtime state.

The base reaches the running app through the ``tai42_app`` handle and the
process through the signal chain, both of which are process-wide. Every test
gets its own app binding, and the chain is asserted empty on the way out so a
leaked subscription cannot make the next test pass for the wrong reason.
"""

from __future__ import annotations

import signal
from collections.abc import Iterator
from pathlib import Path

import pytest
from tai42_contract.app import tai42_app

from tai42_kit.signals import signal_chain

from .fakes import (
    FakeApp,
    FakeOnLoopReporter,
    FakeOnLoopWorker,
    FakeSchedulerRuntime,
    FakeThreadReporter,
    FakeThreadWorker,
)


@pytest.fixture
def app() -> Iterator[FakeApp]:
    fake = FakeApp()
    with tai42_app.bound(fake):
        yield fake


@pytest.fixture(autouse=True)
def liveness_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The liveness file every launch in this suite binds, inside the test's own directory."""
    path = tmp_path / "liveness"
    monkeypatch.setenv("TAI_BACKEND_LIVENESS_PATH", str(path))
    return path


@pytest.fixture(autouse=True)
def _clean_runtime_state() -> Iterator[None]:
    for runtime_cls in (
        FakeOnLoopWorker,
        FakeThreadWorker,
        FakeSchedulerRuntime,
        FakeOnLoopReporter,
        FakeThreadReporter,
    ):
        runtime_cls.latest = None
    yield
    assert signal_chain.subscribers(signal.SIGTERM) == ()
    assert signal_chain.subscribers(signal.SIGINT) == ()
