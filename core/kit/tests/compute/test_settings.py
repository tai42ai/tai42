"""``ComputeSettings`` — ``COMPUTE_WORKERS`` sizes the compute pool, at least one worker."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tai42_kit.compute import ComputeSettings, compute_settings
from tai42_kit.settings import registered_settings


def test_default_is_two_workers() -> None:
    assert ComputeSettings().workers == 2


def test_compute_workers_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPUTE_WORKERS", "5")

    assert compute_settings().workers == 5


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_fewer_than_one_worker_is_refused(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("COMPUTE_WORKERS", bad)

    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        compute_settings()


def test_registered_with_workers_converging_through_a_recycle() -> None:
    group = next(info for info in registered_settings() if info.name == "ComputeSettings")

    assert group.env_prefix == "COMPUTE_"
    assert {field.name: field.reload_class for field in group.fields} == {"workers": "recycle"}
