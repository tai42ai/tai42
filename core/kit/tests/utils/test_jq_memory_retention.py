"""A compiled jq program keeps no evaluation's input once its run ends, in this process and in a pool worker."""

import importlib
import json
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

pytest.importorskip("jq")

from tai42_kit.compute import ComputePool
from tai42_kit.utils.data import jq_worker
from tai42_kit.utils.data.jq_errors import JqEvaluationError
from tai42_kit.utils.data.jq_worker import JqSource, compile_and_evaluate_job, envelope

pytestmark = pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="VmRSS is read from procfs")

_JOB_MODULES = Path(__file__).parent / "jq_worker_modules"

# Twenty programs, each run over its own ~1.7 MB envelope: a program that kept its input would
# keep each parsed envelope, about 12 MB apiece.
_PROGRAMS = 20
_KEPT_INPUT_BOUND_KIB = 30 * 1024


def _envelope_text(items: int, count: int) -> str:
    return json.dumps(envelope({"items": [{"k": i, "t": "x" * 50} for i in range(items)]}, {}, count))


@pytest.fixture
def jobs(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The job module, importable by name here and in a spawned worker, which starts with this path."""
    monkeypatch.syspath_prepend(str(_JOB_MODULES))
    return importlib.import_module("jq_evaluation_round")


@pytest.fixture(autouse=True)
def _empty_cache() -> Iterator[None]:
    jq_worker._compiled.clear()
    yield
    jq_worker._compiled.clear()


def test_a_first_value_run_keeps_no_input_alive(jobs: ModuleType) -> None:
    text = _envelope_text(20_000, 1)
    before = jobs.resident_kib()
    for i in range(_PROGRAMS):
        source = f".items[] | .k + {i}"
        assert compile_and_evaluate_job(source, JqSource(source, "", ()), text) == [i]
    assert jobs.resident_kib() - before < _KEPT_INPUT_BOUND_KIB


def test_an_error_before_the_first_value_raises_and_keeps_no_input(jobs: ModuleType) -> None:
    text = _envelope_text(20_000, 1)
    before = jobs.resident_kib()
    for i in range(_PROGRAMS):
        source = f'.items[] | error("stop {i}")'
        with pytest.raises(JqEvaluationError, match=f"stop {i}"):
            compile_and_evaluate_job(source, JqSource(source, "", ()), text)
    assert jobs.resident_kib() - before < _KEPT_INPUT_BOUND_KIB


def test_a_halting_program_keeps_no_input(jobs: ModuleType) -> None:
    text = _envelope_text(20_000, 1)
    before = jobs.resident_kib()
    for i in range(_PROGRAMS):
        source = f".items[] | if .k == {i} then halt else empty end"
        assert compile_and_evaluate_job(source, JqSource(source, "", ()), text) == []
    assert jobs.resident_kib() - before < _KEPT_INPUT_BOUND_KIB


def test_a_halt_error_raises_and_keeps_no_input(jobs: ModuleType) -> None:
    text = _envelope_text(20_000, 1)
    before = jobs.resident_kib()
    for i in range(_PROGRAMS):
        source = f".items[] | if .k == {i} then halt_error else empty end"
        with pytest.raises(JqEvaluationError) as exc:
            compile_and_evaluate_job(source, JqSource(source, "", ()), text)
        assert '{"k":' in str(exc.value)
    assert jobs.resident_kib() - before < _KEPT_INPUT_BOUND_KIB


async def test_a_worker_running_new_programs_round_after_round_stays_flat(jobs: ModuleType) -> None:
    # One worker that is never replaced runs three rounds of a hundred programs it has not seen,
    # each over a ~200 KB envelope; the second and third rounds add no kept input.
    text = _envelope_text(3_000, 1)
    pool = ComputePool(1, max_tasks=1_000_000)
    pool.start()
    try:
        resident = [
            await pool.run(jobs.evaluate_round, [f".items[] | .k + {round_ * 100 + i}" for i in range(100)], text)
            for round_ in range(3)
        ]
    finally:
        pool.stop()
    assert resident[2] - resident[0] < 25 * 1024
