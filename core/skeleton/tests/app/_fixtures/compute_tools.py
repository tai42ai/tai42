"""Fixture tool module imported by a manifest ``tools:`` entry: a tool that runs its work through ``app.compute``.

The jobs live in their own module (imported by its absolute ``tests.*`` name), which a worker
process imports without registering this tool.
"""

from typing import Literal

from tai42_contract.app import tai42_app
from tests.app._fixtures import compute_jobs

_JOBS = {"backtrack": compute_jobs.backtrack, "refuse": compute_jobs.refuse, "linger": compute_jobs.linger}


@tai42_app.tools.tool
async def compute_probe(job: Literal["backtrack", "refuse", "linger"], arg: int, timeout: float | None = None) -> bool:
    """Run the named job in a compute worker and return its result."""
    return await tai42_app.compute.run(_JOBS[job], arg, timeout=timeout)
