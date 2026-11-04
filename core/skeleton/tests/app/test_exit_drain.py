"""The exit drain a process announces: its runtime's stop phase plus the shutdown bounds its modules declare."""

from __future__ import annotations

import pytest

from tai42_skeleton.app.lifecycle.exit_drain import ExitDrain


def test_nothing_declared_announces_no_drain() -> None:
    assert ExitDrain().seconds() is None


def test_a_runtime_phase_alone_is_the_drain() -> None:
    exit_drain = ExitDrain()
    exit_drain.declare(lambda: 45.0)

    assert exit_drain.seconds() == 45.0


def test_a_runtime_without_a_bound_makes_the_whole_stop_unbounded() -> None:
    exit_drain = ExitDrain()
    exit_drain.declare(lambda: None)
    exit_drain.declare_shutdown("first", lambda: 7.0)

    assert exit_drain.seconds() is None


def test_shutdown_bounds_without_a_runtime_phase_announce_no_drain() -> None:
    exit_drain = ExitDrain()
    exit_drain.declare_shutdown("first", lambda: 7.0)

    assert exit_drain.seconds() is None


def test_the_drain_sums_the_runtime_phase_and_every_shutdown_bound_read_at_call_time() -> None:
    exit_drain = ExitDrain()
    exit_drain.declare(lambda: 10.0)
    second = [20.0]
    exit_drain.declare_shutdown("first", lambda: 40.0)
    exit_drain.declare_shutdown("second", lambda: second[0])

    assert exit_drain.seconds() == 70.0
    second[0] = 5.0
    assert exit_drain.seconds() == 55.0


def test_a_second_runtime_declaration_raises() -> None:
    exit_drain = ExitDrain()
    exit_drain.declare(lambda: 10.0)

    with pytest.raises(ValueError, match="already declared"):
        exit_drain.declare(lambda: 20.0)


def test_a_second_shutdown_declaration_under_one_owner_raises() -> None:
    exit_drain = ExitDrain()
    exit_drain.declare_shutdown("first", lambda: 7.0)

    with pytest.raises(ValueError, match="'first'"):
        exit_drain.declare_shutdown("first", lambda: 8.0)


_PLATFORM_SHUTDOWN_ENV = (
    "HOOKS_RUN_SHUTDOWN_GRACE_SECONDS",
    "STATES_OUTBOX_SHUTDOWN_GRACE_SECONDS",
    "TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS",
)


@pytest.fixture
def platform_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform's shutdown settings at their defaults."""
    from tai42_kit.settings import reset_all_settings

    for name in _PLATFORM_SHUTDOWN_ENV:
        monkeypatch.delenv(name, raising=False)
    reset_all_settings()


def test_the_platform_declares_each_shutdown_steps_bound(platform_defaults: None) -> None:
    from tai42_skeleton.app import instance
    from tai42_skeleton.connectors.runtime import refresh_owner
    from tai42_skeleton.hooks.runs import sweep as hook_runs_sweep
    from tai42_skeleton.operations.tool_runs import supervisor
    from tai42_skeleton.states.outbox import sweep as outbox_sweep

    exit_drain = instance.build_app().exit_drain
    declared = {owner: seconds() for owner, seconds in exit_drain._shutdown.items()}

    assert declared == {
        "hook_runs": 40.0,
        "states_outbox": 20.0,
        "tool_runs": 10.0,
        "credential_refresh": 30.0,
    }
    # Each figure is the bound the stop that spends it waits.
    assert declared["hook_runs"] == hook_runs_sweep.shutdown_drain_seconds()
    assert declared["states_outbox"] == outbox_sweep.shutdown_drain_seconds()
    assert declared["tool_runs"] == supervisor.shutdown_drain_seconds()
    assert declared["credential_refresh"] == refresh_owner.ACLOSE_TIMEOUT_SECONDS


def test_the_platform_exit_drain_is_the_runtime_phase_plus_its_shutdown_bounds(
    platform_defaults: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_kit.settings import reset_all_settings

    from tai42_skeleton.app import instance

    app = instance.build_app()
    # The runtime phase a serve or backend process declares, stood in for here.
    monkeypatch.setattr(app.exit_drain, "_seconds", lambda: 5.0)
    retire_budget = app.drain_budgets.budget()

    assert app.exit_drain.seconds() == 5.0 + 40.0 + 20.0 + 10.0 + 30.0

    monkeypatch.setenv("TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS", "13")
    reset_all_settings()
    assert app.exit_drain.seconds() == 5.0 + 40.0 + 20.0 + 13.0 + 30.0
    # The exit declarations leave the retire's budget its own figure.
    assert retire_budget == 10.0
    assert app.drain_budgets.budget() == 13.0
