"""``BackendRuntime`` liveness: the declared signal kinds and the progress-reporting seam a loop runtime calls."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Self

import pytest

from tai42_contract.backend import BackendRuntime, ExecutionMode, HttpLiveness, ProgressLiveness


class _Reports:
    """A progress reporter that records every promise it receives."""

    def __init__(self) -> None:
        self.promises: list[float] = []

    def __call__(self, *, next_within: float) -> None:
        self.promises.append(next_within)


class _LoopRuntime(BackendRuntime):
    name = "loop"
    mode = ExecutionMode.inline
    liveness = ProgressLiveness()

    @classmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        return cls()


def test_an_unbound_report_raises() -> None:
    runtime = _LoopRuntime.from_args([])

    with pytest.raises(RuntimeError, match="_LoopRuntime reported progress with no reporter bound"):
        runtime.report_progress(next_within=1.0)


def test_a_bound_report_forwards_the_promise() -> None:
    runtime = _LoopRuntime.from_args([])
    reports = _Reports()
    runtime.bind_progress(reports)

    runtime.report_progress(next_within=1.5)
    runtime.report_progress(next_within=60.0)

    assert reports.promises == [1.5, 60.0]


@pytest.mark.parametrize("next_within", [0.0, -2.0, float("nan"), float("inf")])
def test_a_report_without_a_positive_promise_raises(next_within: float) -> None:
    runtime = _LoopRuntime.from_args([])
    reports = _Reports()
    runtime.bind_progress(reports)

    with pytest.raises(ValueError, match="next_within must be positive seconds"):
        runtime.report_progress(next_within=next_within)
    assert reports.promises == []


def test_a_second_bind_raises() -> None:
    runtime = _LoopRuntime.from_args([])
    runtime.bind_progress(_Reports())

    with pytest.raises(RuntimeError, match="_LoopRuntime already has a progress reporter bound"):
        runtime.bind_progress(_Reports())


def test_a_binding_is_per_instance() -> None:
    bound = _LoopRuntime.from_args([])
    bound.bind_progress(_Reports())

    with pytest.raises(RuntimeError, match="no reporter bound"):
        _LoopRuntime.from_args([]).report_progress(next_within=1.0)


def test_http_liveness_keeps_its_path() -> None:
    assert HttpLiveness("/healthcheck").path == "/healthcheck"


def test_the_liveness_values_are_frozen_and_comparable() -> None:
    assert ProgressLiveness() == ProgressLiveness()
    assert HttpLiveness("/a") == HttpLiveness("/a")
    assert HttpLiveness("/a") != HttpLiveness("/b")
    assert ProgressLiveness() != HttpLiveness("/a")
    with pytest.raises(dataclasses.FrozenInstanceError):
        HttpLiveness("/a").path = "/b"  # type: ignore[misc]
