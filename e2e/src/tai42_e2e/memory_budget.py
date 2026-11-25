"""The memory budget a serve is held to, as checks over :class:`~tai42_e2e.rss.ServeMemory` samples.

Each check raises :class:`MemoryBudgetError` with the full account of the breach."""

from __future__ import annotations

from collections.abc import Sequence

from tai42_e2e.rss import ServeMemory

# A compute-pool child at or above this resident set (KiB) breaks the budget.
MEMORY_CHILD_BOUND_KB = 300 * 1024
# The serve's total resident set at the end of a load may be at most this multiple of its
# total after the first pass of that load.
MEMORY_GROWTH_FACTOR = 1.5
# The share of the burst's growth still held after the idle wait.
MEMORY_IDLE_RETURN_FRACTION = 0.5


class MemoryBudgetError(AssertionError):
    """A serve broke its memory budget."""


def check_child_bound(samples: Sequence[ServeMemory], *, bound_kb: int) -> None:
    """Every compute-pool child of every sample stays below ``bound_kb``."""
    breaches = [
        f"sample {index}: child {child.pid} at {child.rss_kb} kB"
        for index, sample in enumerate(samples)
        for child in sample.children
        if child.rss_kb >= bound_kb
    ]
    if breaches:
        raise MemoryBudgetError(
            f"{len(breaches)} compute-pool child sample(s) at or above the {bound_kb} kB bound: " + "; ".join(breaches)
        )


def check_growth(warm: ServeMemory, end: ServeMemory, *, factor: float) -> None:
    """The serve's total at the end is at most ``factor`` times its total after warm-up."""
    if end.total_kb > factor * warm.total_kb:
        raise MemoryBudgetError(
            f"the serve grew from {warm.total_kb} kB after warm-up to {end.total_kb} kB at the end "
            f"(x{end.total_kb / warm.total_kb:.2f}), above the x{factor} bound"
        )


def check_idle_return(baseline: ServeMemory, peak: ServeMemory, end: ServeMemory, *, fraction: float) -> None:
    """Of the burst's growth above the idle ``baseline`` taken before the load
    (``peak - baseline``), the idle end still holds less than ``fraction`` (``end - baseline``).
    A peak not above the baseline raises: the load grew nothing, so the check has nothing to
    judge."""
    growth_kb = peak.total_kb - baseline.total_kb
    if growth_kb <= 0:
        raise MemoryBudgetError(
            f"the load never raised the serve above its idle baseline: peak {peak.total_kb} kB, "
            f"baseline {baseline.total_kb} kB, so the idle return proves nothing"
        )
    held_kb = end.total_kb - baseline.total_kb
    if held_kb >= fraction * growth_kb:
        raise MemoryBudgetError(
            f"the serve still held {held_kb} kB above its idle baseline of {baseline.total_kb} kB "
            f"once idle (end {end.total_kb} kB) out of a growth of {growth_kb} kB "
            f"(peak {peak.total_kb} kB): x{held_kb / growth_kb:.2f} of the growth, not below the x{fraction} bound"
        )


def check_turnover(warm: ServeMemory, end: ServeMemory) -> None:
    """At least one compute-pool child present at the end was not present at warm-up: the pool
    replaced a child after warm-up."""
    warm_pids = {child.pid for child in warm.children}
    end_pids = {child.pid for child in end.children}
    if end_pids <= warm_pids:
        raise MemoryBudgetError(
            f"no compute-pool child was replaced after warm-up: children at warm-up {sorted(warm_pids)}, "
            f"at the end {sorted(end_pids)}"
        )
