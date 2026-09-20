"""The periodic sandbox reap loop: reaps + logs, and survives a reap-cycle exception."""

from __future__ import annotations

import asyncio
import logging

import pytest
from tai42_contract.app import tai42_app

import tai42_skeleton.sandbox.reaper as reaper_mod
from tai42_skeleton.app.instance import build_app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.sandbox.reaper import run_sandbox_reap_loop


class _ReapStub:
    """A stub provider whose ``reap`` is scripted: it fires ``fired`` after each call and
    either returns ids or raises, so the loop's log + survival behavior is observable."""

    def __init__(self, *, raise_error: bool) -> None:
        self._raise = raise_error
        self.calls = 0
        self.fired = asyncio.Event()

    async def reap(self) -> list[str]:
        self.calls += 1
        self.fired.set()
        if self._raise:
            raise RuntimeError("reap boom")
        return [f"sess-{self.calls}"]


@pytest.fixture
def bound_app(monkeypatch: pytest.MonkeyPatch):
    app = build_app()
    tai42_app.bind(app)
    monkeypatch.setattr(app, "_manifest", Manifest.model_validate({}), raising=False)
    # A fast tick so the loop reaps promptly under test.
    monkeypatch.setattr(reaper_mod, "_reap_interval_seconds", lambda: 0.01)
    return app


async def _run_until_fired(stub: _ReapStub) -> asyncio.Task[None]:
    task = asyncio.create_task(run_sandbox_reap_loop())
    try:
        await asyncio.wait_for(stub.fired.wait(), timeout=2.0)
    except TimeoutError:
        task.cancel()
        raise
    return task


async def test_reaper_reaps_and_logs_ids(bound_app, monkeypatch, caplog) -> None:
    stub = _ReapStub(raise_error=False)
    monkeypatch.setattr(bound_app._sandbox_holder, "_sandbox", stub)
    with caplog.at_level(logging.INFO, logger=reaper_mod.__name__):
        task = await _run_until_fired(stub)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert any("reaped expired session sess-1" in rec.message for rec in caplog.records)


async def test_reaper_logs_exception_loudly_and_continues(bound_app, monkeypatch, caplog) -> None:
    stub = _ReapStub(raise_error=True)
    monkeypatch.setattr(bound_app._sandbox_holder, "_sandbox", stub)
    with caplog.at_level(logging.ERROR, logger=reaper_mod.__name__):
        task = await _run_until_fired(stub)
        # The loop must survive the reap-cycle exception — still running, never dead.
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert any(rec.levelno == logging.ERROR and "reap cycle failed" in rec.message for rec in caplog.records)
    assert stub.calls >= 2  # it kept ticking after the first failure
