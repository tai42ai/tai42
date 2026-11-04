"""The run of a dispatched backend job: detached, bounded and admitted.

A worker executes a dequeued job with no live caller holding a connection, so the
job is a detached run (:mod:`tai42_kit.utils.detached_util`: the turn budget does not
apply); it runs under the backend's job budget, fixed when the job was dispatched,
so it is a bounded run (:mod:`tai42_kit.utils.run_bound`); and it is admitted through
the app's ``admitted_job`` door, so a job running on the serving loop is counted as
the live serving generation's work, which a reload's retire drains before it closes
that generation's loop-bound resources. :func:`dispatched_job` is the one place that
sets and resets these facts. :func:`run_dispatched_tool` is every backend's job body: it
runs the job's tool inside :func:`dispatched_job`.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from tai42_contract.app import tai42_app

from tai42_kit.backend.schedule_fire import backend_fire
from tai42_kit.utils.detached_util import mark_detached_run, reset_detached_run
from tai42_kit.utils.run_bound import bounded_run
from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG, bind_worker_secret_capability


@contextmanager
def dispatched_job() -> Iterator[None]:
    """Run the scope as a dispatched job's run: detached, bounded and admitted on the app, for its duration."""
    detached_token = mark_detached_run()
    try:
        with bounded_run(), tai42_app.lifecycle.admitted_job():
            yield
    finally:
        reset_detached_run(detached_token)


async def run_dispatched_tool(kwargs: dict[str, Any], *, tool_name_arg: str) -> Any:
    """Run the tool a dequeued job names, with the job's kwargs, as a dispatched job.

    Pops the tool name from ``kwargs[tool_name_arg]`` and the secret-read capability the job
    carried from ``kwargs[WORKER_SECRET_CAPABILITY_ARG]``; a job without either raises
    ``KeyError``. No HTTP request bound the capability in a worker, so the job's own is bound for
    the run. :func:`~tai42_kit.backend.schedule_fire.backend_fire` then pops the reserved
    ``backend_schedule_*`` kwargs and either drives the schedule door under the stamped firing
    identity or runs the tool plainly; a park comes back as the re-park sentinel it returns.
    """
    tool_name = kwargs.pop(tool_name_arg)
    secret_capability = kwargs.pop(WORKER_SECRET_CAPABILITY_ARG)
    with dispatched_job(), bind_worker_secret_capability(secret_capability):
        return await backend_fire(tool_name, kwargs)
