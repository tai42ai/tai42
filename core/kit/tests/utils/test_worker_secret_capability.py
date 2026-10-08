"""The backend-worker secret-read capability bind and the access-control gate state the kit is handed."""

from __future__ import annotations

import inspect

import pytest
from tai42_contract.access_control import (
    caller_may_read_secrets,
    reset_request_secret_capability,
    set_request_secret_capability,
)

from tai42_kit.registry import StagedSlot
from tai42_kit.utils import worker_secret_capability as capability_module
from tai42_kit.utils.worker_secret_capability import (
    abort_staging,
    access_control_gate_state,
    begin_staging,
    bind_worker_secret_capability,
    commit_staging,
    set_access_control_gate_state,
)


@pytest.fixture(autouse=True)
def _undeclared_gate_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capability_module, "_GATE_STATE", StagedSlot())


def test_an_undeclared_gate_state_raises() -> None:
    with pytest.raises(RuntimeError, match="the access-control gate state was never declared to the kit"):
        access_control_gate_state()


@pytest.mark.parametrize("enabled", [True, False])
def test_the_declared_gate_state_is_read_back(enabled: bool) -> None:
    set_access_control_gate_state(enabled)
    assert access_control_gate_state() is enabled


def test_a_later_declaration_replaces_the_earlier_one() -> None:
    set_access_control_gate_state(True)
    set_access_control_gate_state(False)
    assert access_control_gate_state() is False


def test_a_staged_declaration_takes_effect_at_commit() -> None:
    set_access_control_gate_state(True)
    begin_staging()
    set_access_control_gate_state(False)
    assert access_control_gate_state() is True
    commit_staging()
    assert access_control_gate_state() is False


def test_an_aborted_rebuild_keeps_the_serving_gate_state() -> None:
    set_access_control_gate_state(True)
    begin_staging()
    set_access_control_gate_state(False)
    abort_staging()
    assert access_control_gate_state() is True


def test_a_rebuild_that_declares_nothing_keeps_the_serving_gate_state() -> None:
    set_access_control_gate_state(False)
    begin_staging()
    commit_staging()
    assert access_control_gate_state() is False


def test_the_bind_requires_a_capability() -> None:
    parameter = inspect.signature(bind_worker_secret_capability).parameters["capability"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.annotation == "bool"


@pytest.mark.parametrize("capability", [True, False])
def test_the_bind_binds_the_capability_verbatim(capability: bool) -> None:
    assert caller_may_read_secrets() is False
    with bind_worker_secret_capability(capability):
        assert caller_may_read_secrets() is capability
    assert caller_may_read_secrets() is False


def test_bind_restores_prior_capability() -> None:
    # The bind restores whatever capability was in force, not a hardcoded default,
    # so a nested bind never clobbers an outer one.
    token = set_request_secret_capability(True)
    try:
        with bind_worker_secret_capability(False):
            assert caller_may_read_secrets() is False
        assert caller_may_read_secrets() is True
    finally:
        reset_request_secret_capability(token)
