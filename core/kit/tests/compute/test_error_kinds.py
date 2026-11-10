"""The compute errors classify through the contract's error taxonomy."""

from __future__ import annotations

import operator

import pytest
from tai42_contract.errors import ErrorKind, error_kind, is_transport_error

from tai42_kit.compute import (
    ComputeJobFailedError,
    ComputePool,
    ComputePoolUnavailableError,
    ComputeWorkerLostError,
    WorkerTraceback,
)


@pytest.mark.parametrize("error_type", [ComputePoolUnavailableError, ComputeWorkerLostError])
def test_a_platform_compute_failure_is_an_unavailable_transport_failure(error_type: type[Exception]) -> None:
    error = error_type("the pool cannot answer")

    assert error_kind(error) is ErrorKind.UNAVAILABLE
    assert is_transport_error(error) is True


def test_a_job_failure_that_does_not_rebuild_is_the_jobs_own_and_not_a_transport_failure() -> None:
    error = ComputeJobFailedError("compute job m.f raised m.E: bad")
    error.__cause__ = WorkerTraceback("Traceback (most recent call last): ...")

    assert error_kind(error) is ErrorKind.UNKNOWN
    assert is_transport_error(error) is False


async def test_a_job_on_a_stopped_pool_raises_an_unavailable_transport_failure() -> None:
    pool = ComputePool(1)
    pool.start()
    pool.stop()

    with pytest.raises(ComputePoolUnavailableError, match="was stopped") as raised:
        await pool.run(operator.add, 1, 1)

    assert error_kind(raised.value) is ErrorKind.UNAVAILABLE
    assert is_transport_error(raised.value) is True
