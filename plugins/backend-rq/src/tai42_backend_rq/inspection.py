"""The RQ worker inspection: RQ's raw Redis structures, read directly.

The views read the worker hashes, the queue lists, the job hashes and the result streams RQ
keeps in Redis, over the pooled async client; a cancel goes through RQ's blocking job, scheduler
and command APIs on a thread. A worker is alive when its ``last_heartbeat`` is fresh
(:mod:`tai42_backend_rq.liveness`). RQ keeps no index of the task names its workers can run nor
a queryable record of failed tasks, so ``registered_tasks`` and ``failed_tasks`` keep the base's
refusal. A cancel is RQ's own ``Job.cancel`` for a job no worker has claimed. A job a worker
has claimed or is running is left to finish without ``terminate``, and with it is sent RQ's
stop-job command, which every pool carries to the running job (:attr:`RqInspection.terminates`
says when the job ends on it).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import pickle
import time
import zlib
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any

from redis.exceptions import WatchError
from rq import Queue
from rq.command import send_stop_job_command
from rq.exceptions import InvalidJobOperation, NoSuchJobError
from rq.job import Job, JobStatus
from tai42_kit.backend import BackendInspection
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient, SyncRedisClient

from tai42_backend_rq.job_status import ENDED_STATUSES
from tai42_backend_rq.liveness import heartbeat_fresh
from tai42_backend_rq.schedules import scheduler_for
from tai42_backend_rq.settings import RqSettings, rq_client_kwargs, rq_settings

#: How often a ``task_result`` with a timeout re-reads the task's status.
_POLL_SECONDS = 0.25


def _async_redis() -> AbstractAsyncContextManager[Any]:
    """The pooled async Redis client, typed ``Any``.

    redis-py shares one command stub between its sync and async clients, so the erasure keeps every
    ``await r...`` call sound.
    """
    return client_ctx(RedisClient, **rq_client_kwargs())


def _sync_redis() -> AbstractAsyncContextManager[Any]:
    """The pooled sync Redis client for the blocking scheduler API, typed ``Any`` likewise."""
    return client_ctx(SyncRedisClient, **rq_client_kwargs())


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _decoded(mapping: dict[Any, Any]) -> dict[str, str]:
    return {k.decode(): v.decode() for k, v in mapping.items() if isinstance(k, bytes)}


async def _worker_names(r: Any, settings: RqSettings) -> list[str]:
    """Every registered worker's bare name (RQ's registry set stores ``rq:worker:<name>`` keys)."""
    members = await r.smembers(settings.rq_workers_key)
    prefix = settings.rq_worker_key("")
    return [_text(m).removeprefix(prefix) for m in members]


async def _queue_names(r: Any, settings: RqSettings) -> list[str]:
    """Every registered queue's bare name (RQ's registry set stores ``rq:queue:<name>`` keys)."""
    members = await r.smembers(settings.rq_queues_key)
    prefix = settings.rq_queue_key("")
    return [_text(m).removeprefix(prefix) for m in members]


async def _workers(r: Any, settings: RqSettings, worker_name: str | None) -> list[str]:
    return [worker_name] if worker_name is not None else await _worker_names(r, settings)


async def _read_failure_reason(r: Any, settings: RqSettings, task_id: str) -> str:
    """The failed task's stored traceback text (its newest result).

    RQ persists a failed job's exception only as the compressed ``exc_string`` of the result
    stream; when absent, no details are retained.
    """
    entries = await r.xrevrange(settings.rq_result_key(task_id), count=1)
    if entries:
        raw = entries[0][1].get(b"exc_string")
        if raw:
            return zlib.decompress(base64.b64decode(raw)).decode()
    return "no failure details retained"


async def _read_result(r: Any, settings: RqSettings, task_id: str) -> Any:
    """A finished task's stored result, the newest entry of its result stream.

    A recurring job reuses its id, so only the latest entry is the result.
    """
    stream_key = settings.rq_result_key(task_id)
    stream_entries = await r.xrevrange(stream_key, count=1)
    if not stream_entries:
        return f"No result found for task {task_id} in stream {stream_key}"
    result_bytes = stream_entries[0][1].get(b"return_value")
    if result_bytes:
        try:
            return pickle.loads(base64.b64decode(result_bytes))  # noqa: S301 deserializes this worker's own result payload, not external input
        except (binascii.Error, pickle.UnpicklingError) as e:
            raise ValueError(f"Failed to decode result for task {task_id}: {e}") from e
    return None


def _cancel_unclaimed(r: Any, job: Job) -> tuple[bool, JobStatus | None]:
    """Cancel ``job`` with RQ's own ``Job.cancel`` while no worker has claimed it.

    Returns whether it was cancelled, and the status it read (``None`` once RQ no longer holds the
    job). A worker claims a job by moving its id off the queue list, and it still reads ``queued``
    until the worker marks it started; RQ's worker runs a claimed job whatever its status says. So
    a ``queued`` job off its queue list counts as claimed, and the read and the cancel are one
    transaction watching the job's hash and its queue list: a claim, a start or another change to
    the job landing meanwhile fails the transaction, whose retry then sees it. ``Job.cancel`` reads
    the job again outside the transaction and refuses one that another cancel has taken or whose
    record has gone; that refusal is retried the same way.
    """
    queue_key = Queue(job.origin, connection=r).key
    with r.pipeline() as pipe:
        while True:
            try:
                pipe.watch(job.key, queue_key)
                raw = pipe.hget(job.key, "status")
                if raw is None:
                    return False, None
                status = JobStatus(_text(raw))
                claimed = status == JobStatus.QUEUED and pipe.lpos(queue_key, job.id) is None
                if claimed or status == JobStatus.STARTED or status in ENDED_STATUSES:
                    return False, status
                pipe.multi()
                job.cancel(pipeline=pipe)
                pipe.execute()
            except WatchError:
                continue
            except InvalidJobOperation:
                pipe.reset()
                continue
            else:
                return True, JobStatus.CANCELED


async def _status_once_not(task_id: str, status: JobStatus, deadline: float) -> JobStatus | None:
    """The job's status once it is no longer ``status``, or the status still read at ``deadline``.

    ``None`` once RQ no longer holds the job.
    """
    job_key = rq_settings().rq_job_key(task_id)
    async with _async_redis() as r:
        while True:
            raw = await r.hget(job_key, "status")
            if raw is None:
                return None
            current = JobStatus(_text(raw))
            if current != status or time.monotonic() >= deadline:
                return current
            await asyncio.sleep(_POLL_SECONDS)


def _left_to_finish(task_id: str) -> str:
    """The reply for a claimed or running job a cancel without ``terminate`` leaves to finish."""
    return f"Task {task_id} is running and is left to finish"


def _stop_unconfirmed(task_id: str, status: JobStatus) -> str:
    return f"Task {task_id} stop requested but not confirmed (status: {status.value})"


async def _stop(task_id: str, status: JobStatus) -> str:
    """Stop the claimed or running job ``task_id`` with RQ's stop-job command; the reply.

    A claimed job is first awaited until it starts. The stop is awaited until the job leaves
    ``started``; both waits are bounded by the configured task timeout, and a job still claimed or
    running when it runs out is reported as a stop not confirmed. A job that ends before the stop
    applies is reported with the status it ended in.
    """
    deadline = time.monotonic() + rq_settings().task_timeout
    current: JobStatus | None = status
    if current == JobStatus.QUEUED:
        current = await _status_once_not(task_id, JobStatus.QUEUED, deadline)
        if current == JobStatus.QUEUED:
            return _stop_unconfirmed(task_id, current)
    if current == JobStatus.STARTED:
        try:
            async with _sync_redis() as r:
                await asyncio.to_thread(send_stop_job_command, r, task_id)
        except InvalidJobOperation:
            # RQ refuses a stop for a job no worker runs: it ended meanwhile.
            current = await _status_once_not(task_id, JobStatus.STARTED, deadline)
        except NoSuchJobError:
            current = None
        else:
            current = await _status_once_not(task_id, JobStatus.STARTED, deadline)
            if current == JobStatus.STARTED:
                return _stop_unconfirmed(task_id, current)
    if current is None:
        return f"Task {task_id} not found"
    if current == JobStatus.STOPPED:
        return f"Task {task_id} stopped"
    return f"Task {task_id} cannot be stopped (status: {current.value})"


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def _running_job(job: Job) -> dict[str, Any]:
    """The ``backend_active_tasks`` entry of a running ``job``; see :meth:`RqInspection.active_tasks`."""
    return {
        "id": job.id,
        "description": job.description,
        "origin": job.origin,
        "status": job.get_status(refresh=False).value,
        "enqueued_at": _iso(job.enqueued_at),
        "started_at": _iso(job.started_at),
    }


class RqInspection(BackendInspection):
    """RQ's views of its workers and their tasks, each worker addressable by its name."""

    @property
    def addresses_workers(self) -> bool:
        """``True``: each worker has its own registry hash."""
        return True

    async def ping(self, worker_name: str | None) -> dict[str, str]:
        """``"pong"`` for each worker whose heartbeat is fresh."""
        settings = rq_settings()
        ping: dict[str, str] = {}
        async with _async_redis() as r:
            for w in await _workers(r, settings, worker_name):
                hb = await r.hget(settings.rq_worker_key(w), "last_heartbeat")
                if hb and heartbeat_fresh(datetime.fromisoformat(_text(hb))):
                    ping[w] = "pong"
        return ping

    async def active_workers(self) -> list[str]:
        """The names of the workers whose heartbeat is fresh."""
        return list(await self.ping(None))

    async def task_status(self, task_id: str) -> str:
        """The job hash's ``status`` (``unknown`` for a job RQ does not hold)."""
        settings = rq_settings()
        async with _async_redis() as r:
            status_raw = await r.hget(settings.rq_job_key(task_id), "status")
        return "unknown" if status_raw is None else _text(status_raw)

    async def task_result(self, task_id: str, timeout: float | None) -> tuple[bool, str, Any]:
        """The task's ``(ready, status, value)``, re-reading its status until ``timeout`` elapses.

        A job RQ does not hold is ready with the text ``Task <id> not found``; a stopped or
        cancelled one is ready with ``Task <id> did not complete (status: <status>)``. A failed
        task raises ``RuntimeError`` carrying its stored traceback text.
        """
        settings = rq_settings()
        deadline = None if timeout is None else time.monotonic() + timeout
        async with _async_redis() as r:
            while True:
                status_raw = await r.hget(settings.rq_job_key(task_id), "status")
                if status_raw is None:
                    return True, "unknown", f"Task {task_id} not found"
                status = _text(status_raw)
                if status == JobStatus.FINISHED:
                    return True, status, await _read_result(r, settings, task_id)
                if status == JobStatus.FAILED:
                    raise RuntimeError(f"Task {task_id} failed: {await _read_failure_reason(r, settings, task_id)}")
                if status in ENDED_STATUSES:
                    return True, status, f"Task {task_id} did not complete (status: {status})"
                if deadline is None or time.monotonic() >= deadline:
                    return False, status, None
                await asyncio.sleep(_POLL_SECONDS)

    @property
    def terminates(self) -> bool:
        """``True``: every pool carries RQ's stop-job command to the running job.

        A job blocked in C code, or holding the CPU on ``gevent``, is stopped only once that returns,
        so one that never returns is not stopped. On ``solo`` and ``gevent`` a job whose async work
        catches the stop runs on: it ends on the stop once that work returns, and one still running
        a second past its task timeout is not stopped; the liveness probe then ends its worker. On
        those pools a stop sent once the job's task timeout has fired raises nothing into the job:
        the job is marked ``stopped`` only if it then ends in rq's failure handling, and one still
        running a second past its task timeout has its worker ended by the liveness probe.
        """
        return True

    async def cancel(self, task_id: str, terminate: bool) -> str:
        """Cancel a task: a job no worker has claimed with RQ's own ``Job.cancel``.

        A job a worker has claimed or is running is left to finish without ``terminate``, its
        record untouched (``Job.cancel`` would rewrite it). With ``terminate`` it is sent RQ's
        stop-job command, awaited until the job leaves ``started`` within the configured task
        timeout; a stop not seen within it is reported as requested but not confirmed. A
        ``prefork`` worker kills the job's work-horse with ``SIGKILL``, so the job's cleanup does
        not run; a ``solo`` or ``gevent`` worker raises the stop into the job the way its task
        timeout is raised, and the job's async work is cancelled and its cleanup run. A recurring
        job is taken off the scheduler first, and its current run then follows the same rules, the
        reply for the run following the scheduler's. A job that has ended is reported with its
        status, and a job RQ does not hold as not found.
        """
        async with _sync_redis() as r:
            scheduler = scheduler_for(r)

            def _cancel() -> tuple[bool, bool, JobStatus | None]:
                recurring = task_id in scheduler
                if recurring:
                    scheduler.cancel(task_id)
                try:
                    job = Job.fetch(task_id, connection=r)
                except NoSuchJobError:
                    return recurring, False, None
                return recurring, *_cancel_unclaimed(r, job)

            recurring, canceled, status = await asyncio.to_thread(_cancel)
        if canceled or status is None or status in ENDED_STATUSES:
            if recurring:
                return f"Scheduled Task {task_id} canceled"
            if canceled:
                return f"Task {task_id} canceled"
            if status is None:
                return f"Task {task_id} not found"
            return f"Task {task_id} cannot be canceled (status: {status.value})"
        run = await _stop(task_id, status) if terminate else _left_to_finish(task_id)
        return f"Scheduled Task {task_id} canceled; {run}" if recurring else run

    async def active_tasks(self, worker_name: str | None) -> dict[str, list[dict[str, Any]]]:
        """Each busy worker's current job, read through RQ's ``Job`` API (one job per worker at a time).

        A job is ``{"id", "description", "origin", "status", "enqueued_at", "started_at"}``: its call
        description, the queue it came from, its status and its ISO 8601 times, as RQ's job record
        holds them; its arguments, which RQ keeps pickled, are not read. Only a ``started`` job is
        listed: a worker whose job ends or goes between the two reads is not, as a worker with no job
        is not.
        """
        settings = rq_settings()
        current: dict[str, str] = {}
        async with _async_redis() as r:
            for w in await _workers(r, settings, worker_name):
                cj = await r.hget(settings.rq_worker_key(w), "current_job")
                if cj:
                    current[w] = _text(cj)
        if not current:
            return {}
        async with _sync_redis() as r:
            jobs = await asyncio.to_thread(Job.fetch_many, list(current.values()), connection=r)
        return {
            w: [_running_job(job)]
            for w, job in zip(current, jobs, strict=True)
            if job is not None and job.get_status(refresh=False) == JobStatus.STARTED
        }

    async def reserved_tasks(self) -> dict[str, list[str]]:
        """Each queue's waiting job ids, keyed by queue (RQ reserves work per queue, not per worker)."""
        settings = rq_settings()
        reserved: dict[str, list[str]] = {}
        async with _async_redis() as r:
            for q in await _queue_names(r, settings):
                reserved[q] = [_text(jid) for jid in await r.lrange(settings.rq_queue_key(q), 0, -1)]
        return reserved

    async def scheduled_tasks(self) -> dict[str, Any]:
        """Each waiting job id with its ``{"next_run_at_ts"}``.

        RQ keeps the recurring schedules in the scheduler's zset and the ``eta``/``countdown`` jobs
        in each queue's scheduled-job registry, so both are read (each scores in epoch seconds).
        """
        settings = rq_settings()
        scheduled: dict[str, Any] = {}
        async with _async_redis() as r:
            keys = [settings.rq_scheduler_zset]
            keys += [settings.rq_scheduled_registry_key(q) for q in await _queue_names(r, settings)]
            for key in keys:
                for member, score in await r.zrange(key, 0, -1, withscores=True):
                    scheduled[_text(member)] = {"next_run_at_ts": float(score)}
        return scheduled

    async def worker_stats(self, worker_name: str | None) -> dict[str, Any]:
        """Each worker's registry hash (state, heartbeat, queues, counters), decoded to text."""
        settings = rq_settings()
        async with _async_redis() as r:
            return {
                w: _decoded(await r.hgetall(settings.rq_worker_key(w)))
                for w in await _workers(r, settings, worker_name)
            }

    async def worker_queues(self, worker_name: str | None) -> dict[str, list[str]]:
        """Each worker's consumed queue names, from its registry hash."""
        settings = rq_settings()
        queues_by_worker: dict[str, list[str]] = {}
        async with _async_redis() as r:
            for w in await _workers(r, settings, worker_name):
                queues = await r.hget(settings.rq_worker_key(w), "queues")
                if queues:
                    text = _text(queues)
                    queues_by_worker[w] = json.loads(text) if text.startswith("[") else text.split(",")
        return queues_by_worker
