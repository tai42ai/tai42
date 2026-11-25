"""Harness self-test: the memory budget's rules over synthetic serve samples.

Each rule passes inside its bound and raises :class:`MemoryBudgetError` with the full
account of the breach outside it."""

from __future__ import annotations

import pytest

from tai42_e2e.memory_budget import (
    MemoryBudgetError,
    check_child_bound,
    check_growth,
    check_idle_return,
    check_turnover,
)
from tai42_e2e.rss import ProcessMemory, ServeMemory

pytestmark = pytest.mark.needs("no-stack")

_BOUND_KB = 1000


def _sample(children: dict[int, int], *, total_kb: int = 10_000) -> ServeMemory:
    return ServeMemory(
        master=ProcessMemory(pid=1, ppid=0, rss_kb=100, threads=3),
        workers=(ProcessMemory(pid=10, ppid=1, rss_kb=200, threads=19),),
        children=tuple(ProcessMemory(pid=pid, ppid=10, rss_kb=rss_kb, threads=1) for pid, rss_kb in children.items()),
        total_kb=total_kb,
    )


def test_children_below_the_bound_pass() -> None:
    check_child_bound([_sample({20: 999}), _sample({21: 10})], bound_kb=_BOUND_KB)


def test_a_child_at_the_bound_raises_naming_the_sample_pid_and_size() -> None:
    samples = [_sample({20: 999}), _sample({20: 999, 21: _BOUND_KB})]

    with pytest.raises(MemoryBudgetError, match=rf"sample 1: child 21 at {_BOUND_KB} kB") as raised:
        check_child_bound(samples, bound_kb=_BOUND_KB)

    assert "child 20" not in str(raised.value)


def test_growth_at_the_factor_passes() -> None:
    check_growth(_sample({}, total_kb=1000), _sample({}, total_kb=1500), factor=1.5)


def test_growth_above_the_factor_raises_with_both_totals() -> None:
    with pytest.raises(MemoryBudgetError, match=r"from 1000 kB .* to 1501 kB .*x1\.50") as raised:
        check_growth(_sample({}, total_kb=1000), _sample({}, total_kb=1501), factor=1.5)

    assert "x1.5 bound" in str(raised.value)


def test_an_idle_end_holding_less_than_the_fraction_of_the_growth_passes() -> None:
    check_idle_return(_sample({}, total_kb=1000), _sample({}, total_kb=5000), _sample({}, total_kb=2999), fraction=0.5)


def test_an_idle_end_holding_the_fraction_of_the_growth_raises_with_the_totals() -> None:
    with pytest.raises(
        MemoryBudgetError,
        match=r"held 2000 kB above its idle baseline of 1000 kB once idle \(end 3000 kB\) "
        r"out of a growth of 4000 kB \(peak 5000 kB\): x0\.50 of the growth",
    ) as raised:
        check_idle_return(
            _sample({}, total_kb=1000), _sample({}, total_kb=5000), _sample({}, total_kb=3000), fraction=0.5
        )

    assert "not below the x0.5 bound" in str(raised.value)


@pytest.mark.parametrize("peak_kb", [1000, 900])
def test_a_peak_not_above_the_baseline_raises(peak_kb: int) -> None:
    with pytest.raises(MemoryBudgetError, match=rf"never raised the serve.*peak {peak_kb} kB, baseline 1000 kB"):
        check_idle_return(
            _sample({}, total_kb=1000), _sample({}, total_kb=peak_kb), _sample({}, total_kb=900), fraction=0.5
        )


def test_a_new_child_after_warm_up_passes_turnover() -> None:
    check_turnover(_sample({20: 1, 21: 1}), _sample({20: 1, 22: 1}))


def test_unchanged_children_raise_listing_both_sets() -> None:
    with pytest.raises(MemoryBudgetError, match=r"warm-up \[20, 21\], at the end \[20\]"):
        check_turnover(_sample({20: 1, 21: 1}), _sample({20: 1}))
