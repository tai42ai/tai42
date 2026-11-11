"""The ``app.compute`` facade over what runs the process's compute jobs (``tai42_kit.compute.compute_pool``)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_kit.compute import compute_pool

from .base import _Facet

if TYPE_CHECKING:
    from collections.abc import Callable


class ComputeFacet(_Facet):
    """``app.compute``: jobs run where ``tai42_kit.compute.compute_pool`` says.

    In the process's own pool, the pool or pool host of a process it was forked from, or a pool host of its own.
    """

    async def run[T, *Ts](self, fn: Callable[[*Ts], T], /, *args: *Ts, timeout: float | None = None) -> T:
        """Run ``fn(*args)`` in a worker process of that compute pool; see ``AppCompute.run``."""
        return await compute_pool().run(fn, *args, timeout=timeout)
