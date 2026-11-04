"""Fixtures the backend suite shares: a bound app, clean chain/runtime state, a declared gate state.

The base reaches the running app through the ``tai42_app`` handle and the
process through the signal chain, both of which are process-wide. Every test
gets its own app binding, and the chain is asserted empty on the way out so a
leaked subscription cannot make the next test pass for the wrong reason.
"""

from __future__ import annotations

import asyncio.futures
import signal
from collections.abc import Iterator
from pathlib import Path

import pytest
from tai42_contract.app import tai42_app

from tai42_kit.backend.reshaped_threads import bind_future_chaining
from tai42_kit.registry import StagedSlot
from tai42_kit.signals import signal_chain
from tai42_kit.utils import worker_secret_capability

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


@pytest.fixture
def gate_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    """The access-control gate declared on in a slot of the test's own, as a serving process declares it."""
    monkeypatch.setattr(worker_secret_capability, "_GATE_STATE", StagedSlot())
    worker_secret_capability.set_access_control_gate_state(True)


#: The standard library's own future chaining, taken when the suite loads, before any test binds it.
_STOCK_CHAIN_FUTURE = asyncio.futures._chain_future  # type: ignore[attr-defined]


@pytest.fixture
def stock_future_chaining(monkeypatch: pytest.MonkeyPatch) -> None:
    """asyncio's future chaining set to the standard library's own for the test, whatever it was before."""
    monkeypatch.setattr(asyncio.futures, "_chain_future", _STOCK_CHAIN_FUTURE)


@pytest.fixture
def bound_future_chaining(stock_future_chaining: None) -> None:
    """asyncio's future chaining bound to reshaped threads for the test, the chaining before it restored after it."""
    bind_future_chaining()


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
