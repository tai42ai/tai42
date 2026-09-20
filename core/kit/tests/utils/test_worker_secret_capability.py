"""The backend-worker secret-read capability bind and its gate-state read."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from tai42_contract.access_control import caller_may_read_secrets

from tai42_kit.settings import reset_all_settings
from tai42_kit.utils.worker_secret_capability import bind_worker_secret_capability


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[bool], None]]:
    """Set ``ACCESS_CONTROL_ENABLE`` for a test and restore the settings cache after."""

    def _set(enabled: bool) -> None:
        monkeypatch.setenv("ACCESS_CONTROL_ENABLE", "true" if enabled else "false")
        reset_all_settings()

    yield _set
    reset_all_settings()


def test_gate_off_binds_secret_capable(gate: Callable[[bool], None]) -> None:
    # Access control OFF makes every principal the synthetic admin, so a worker run
    # binds the capability TRUE — a dev ``inject_env`` run works with no HTTP request.
    gate(False)
    assert caller_may_read_secrets() is False
    with bind_worker_secret_capability():
        assert caller_may_read_secrets() is True
    assert caller_may_read_secrets() is False


def test_gate_on_binds_fail_closed(gate: Callable[[bool], None]) -> None:
    # Access control ON keeps the capability FALSE — a detached worker run never
    # clears the secret fence off a caller it cannot re-authorize.
    gate(True)
    with bind_worker_secret_capability():
        assert caller_may_read_secrets() is False
    assert caller_may_read_secrets() is False


def test_default_gate_is_enabled_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # With the env unset the worker mirror defaults to the same enabled gate the
    # skeleton settings default to, so a stray worker process fail-closes.
    monkeypatch.delenv("ACCESS_CONTROL_ENABLE", raising=False)
    reset_all_settings()
    try:
        with bind_worker_secret_capability():
            assert caller_may_read_secrets() is False
    finally:
        reset_all_settings()


def test_propagated_capable_binds_true_even_under_gate_on(gate: Callable[[bool], None]) -> None:
    # An admin submitter's capability rides with the job as ``True``; the worker binds it
    # verbatim, so an admin's backend-submitted job clears the secret fence even gate ON.
    gate(True)
    with bind_worker_secret_capability(True):
        assert caller_may_read_secrets() is True
    assert caller_may_read_secrets() is False


def test_propagated_incapable_binds_false_even_under_gate_off(gate: Callable[[bool], None]) -> None:
    # A non-admin submitter's capability rides as ``False``; the worker binds it verbatim,
    # so a non-admin's backend-submitted job stays fenced regardless of the gate default.
    gate(False)
    with bind_worker_secret_capability(False):
        assert caller_may_read_secrets() is False
    assert caller_may_read_secrets() is False


def test_absent_capability_falls_back_to_the_gate_state(gate: Callable[[bool], None]) -> None:
    # No propagated value (``None``) falls back to the gate rule — OFF -> True, ON -> False.
    gate(False)
    with bind_worker_secret_capability(None):
        assert caller_may_read_secrets() is True
    gate(True)
    with bind_worker_secret_capability(None):
        assert caller_may_read_secrets() is False


def test_bind_restores_prior_capability(gate: Callable[[bool], None]) -> None:
    # The bind restores whatever capability was in force, not a hardcoded default,
    # so a nested bind never clobbers an outer one.
    from tai42_contract.access_control import reset_request_secret_capability, set_request_secret_capability

    gate(True)
    token = set_request_secret_capability(True)
    try:
        with bind_worker_secret_capability():
            assert caller_may_read_secrets() is False
        assert caller_may_read_secrets() is True
    finally:
        reset_request_secret_capability(token)
