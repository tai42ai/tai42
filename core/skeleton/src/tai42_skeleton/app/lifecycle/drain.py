"""The declared drain budget: how long a retiring generation may take to finish its in-flight work.

The budget bounds the work that can finish on its own. Two kinds of work cannot. A request
or tool run that drives or waits on the reload gate cannot finish before the gate is
released, which happens only after the retire it drives has returned; the retire never
counts it against the budget and never severs it, and waits for a driver to answer — its
own bounds (the rebuild, the fleet apply timeout, a recycle step) bound that wait. A stream
that lives as long as its session (the interactions inbox stream, an MCP transport's
server-to-client stream) exempts itself and is never waited for.

A module whose in-flight work a retire or recycle must wait for declares its own budget
on the app's :class:`DrainBudgetRegistry`; the budget used is the largest declared one,
read at call time so a settings change applies to the next drain. Each app owns its
registry, so a rebuilt app declares afresh; within one app a duplicate owner is refused.
"""

from __future__ import annotations

from collections.abc import Callable


class DrainBudgetRegistry:
    """The drain budgets one app declares."""

    def __init__(self) -> None:
        """Start with no declared budget."""
        self._budgets: dict[str, Callable[[], float]] = {}

    def register(self, owner: str, seconds: Callable[[], float]) -> None:
        """Declare ``owner``'s drain budget in seconds; a second declaration under one owner raises ``ValueError``."""
        if owner in self._budgets:
            raise ValueError(f"a drain budget is already declared for {owner!r}")
        self._budgets[owner] = seconds

    def budget(self) -> float:
        """The largest declared drain budget in seconds; raises ``RuntimeError`` when none is declared."""
        if not self._budgets:
            raise RuntimeError("no drain budget is declared")
        return max(seconds() for seconds in self._budgets.values())


__all__ = ["DrainBudgetRegistry"]
