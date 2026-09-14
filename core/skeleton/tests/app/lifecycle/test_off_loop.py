"""The off-loop ``run_blocking`` runner: coroutine completion + pooled-client cleanup."""

from __future__ import annotations

from tai42_skeleton.app.lifecycle.off_loop import run_blocking


def test_run_blocking_runs_coroutine_to_completion():
    async def coro():
        return 21 * 2

    assert run_blocking(coro) == 42


def test_run_blocking_shuts_down_pooled_clients(monkeypatch):
    # The ephemeral loop must close its pooled clients before teardown, otherwise
    # reload handlers that open pooled clients leak one pool per reload.
    calls: list[str] = []

    async def fake_shutdown():
        calls.append("shutdown")

    monkeypatch.setattr("tai42_skeleton.app.lifecycle.shutdown_all_clients", fake_shutdown)

    async def coro():
        calls.append("body")
        return "done"

    assert run_blocking(coro) == "done"
    assert calls == ["body", "shutdown"]


def test_run_blocking_cleanup_failure_does_not_mask_result(monkeypatch):
    # A failing client shutdown is logged, never raised — the coroutine's result
    # stands.
    async def boom_shutdown():
        raise RuntimeError("shutdown failed")

    monkeypatch.setattr("tai42_skeleton.app.lifecycle.shutdown_all_clients", boom_shutdown)

    async def coro():
        return 7

    assert run_blocking(coro) == 7
