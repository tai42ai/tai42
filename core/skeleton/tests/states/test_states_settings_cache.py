"""The state-store settings are built once per settings epoch and re-read on every settings reset."""

from __future__ import annotations

import pytest
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.states.db import StatesSettings, states_settings


def _count_inits(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Record one entry per ``StatesSettings`` construction."""
    calls: list[None] = []
    original = StatesSettings.__init__

    def counting_init(self, *args, **kwargs):
        calls.append(None)
        original(self, *args, **kwargs)

    monkeypatch.setattr(StatesSettings, "__init__", counting_init)
    return calls


def test_the_states_settings_are_built_once_per_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    builds = _count_inits(monkeypatch)
    first = states_settings()
    for _ in range(10):
        assert states_settings() is first
    assert len(builds) == 1


def test_a_settings_reset_re_reads_the_states_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STATES_OUTBOX_SWEEP_SECONDS", "7")
    first = states_settings()
    assert first.outbox_sweep_seconds == 7
    builds = _count_inits(monkeypatch)
    monkeypatch.setenv("STATES_OUTBOX_SWEEP_SECONDS", "9")
    # Inside one epoch the snapshot holds.
    assert states_settings() is first
    assert states_settings().outbox_sweep_seconds == 7
    reset_all_settings()
    rebuilt = states_settings()
    assert rebuilt is not first
    assert rebuilt.outbox_sweep_seconds == 9
    assert states_settings() is rebuilt
    assert len(builds) == 1
