"""The ``app.compute`` facade over the process's compute pool."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_kit.compute import compute_pool

from .base import _Facet

if TYPE_CHECKING:
    from collections.abc import Callable


class ComputeFacet(_Facet):
    """``app.compute``: jobs run in the process's compute pool (``tai42_kit.compute.compute_pool``)."""

    async def run[T, *Ts](self, fn: Callable[[*Ts], T], /, *args: *Ts, timeout: float | None = None) -> T:
        """Run ``fn(*args)`` in a worker process of the process's compute pool; see ``AppCompute.run``."""
        return await compute_pool().run(fn, *args, timeout=timeout)
