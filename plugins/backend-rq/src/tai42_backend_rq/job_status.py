"""The RQ job statuses the backend reads as a job's end."""

from __future__ import annotations

from rq.job import JobStatus

#: The statuses of a job that has ended: it runs no further and nothing is left to cancel or stop.
#: ``FINISHED`` carries the job's result and ``FAILED`` its stored failure; ``STOPPED`` and
#: ``CANCELED`` end it without either.
ENDED_STATUSES = frozenset({JobStatus.FINISHED, JobStatus.FAILED, JobStatus.STOPPED, JobStatus.CANCELED})
