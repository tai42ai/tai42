"""The declared drain budget, proven with synthetic declarers."""

from __future__ import annotations

import pytest

from tai42_skeleton.app.lifecycle.drain import DrainBudgetRegistry


def test_the_budget_is_the_largest_declared_one() -> None:
    budgets = DrainBudgetRegistry()
    budgets.register("widgets", lambda: 4.0)
    budgets.register("gadgets", lambda: 9.5)
    assert budgets.budget() == 9.5


def test_a_declared_budget_is_read_on_every_call() -> None:
    budgets = DrainBudgetRegistry()
    seconds = [3.0]
    budgets.register("widgets", lambda: seconds[0])
    assert budgets.budget() == 3.0
    seconds[0] = 7.0
    assert budgets.budget() == 7.0


def test_no_declared_budget_raises() -> None:
    with pytest.raises(RuntimeError, match="no drain budget is declared"):
        DrainBudgetRegistry().budget()


def test_a_duplicate_owner_is_refused() -> None:
    budgets = DrainBudgetRegistry()
    budgets.register("widgets", lambda: 1.0)
    with pytest.raises(ValueError, match="widgets"):
        budgets.register("widgets", lambda: 2.0)


def test_the_platform_declares_the_tool_runs_shutdown_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_kit.settings import reset_all_settings

    from tai42_skeleton.app import epoch_retire, instance
    from tai42_skeleton.config import recycle_policy

    monkeypatch.setenv("TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS", "17")
    reset_all_settings()
    try:
        assert instance.build_app().drain_budgets.budget() == 17.0
        # Both readers — the epoch retire and the recycle step — read the app's declared budget.
        assert epoch_retire._drain_budget(None) == 17.0
        assert recycle_policy._recycle_step_timeout() == 17.0
    finally:
        monkeypatch.delenv("TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS")
        reset_all_settings()
