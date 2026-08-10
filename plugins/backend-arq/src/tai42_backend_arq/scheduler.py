"""Redis-hash scheduler for recurring tool runs.

Each schedule lives in one hash (``arq:schedule:{name}``) holding its target,
args/kwargs, schedule dict, compact ``cron_or_interval`` form, enabled flag, and
runtime state (pending job id, last scheduled timestamp, abort marker). The
self-rescheduling ``task_scheduler`` enqueues the target when due and defers its
own replacement. Every state change runs under the per-schedule
``schedule_lock`` so deletes, transitions, and flag writes serialize.
``recover_stalled_schedules`` restarts schedules whose pending job is lost.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import orjson
from arq.jobs import Job, JobStatus
from croniter import croniter

from tai42_backend_arq.pool import RedisPoolManager
from tai42_backend_arq.records import parse_cron_or_interval
from tai42_backend_arq.settings import TaskFailedError, arq_settings, job_deserializer

logger = logging.getLogger(__name__)


async def wait_job_result(job: Job, timeout: float | None = None) -> Any:
    """``Job.result``, but a replayed stored abort surfaces as ``TaskFailedError``
    rather than a raw ``CancelledError`` (which would read as a cancellation of
    the caller). A genuine cancellation of the CALLING task re-raises as-is."""
    try:
        return await job.result(timeout=timeout)
    except asyncio.CancelledError as exc:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise
        raise TaskFailedError("CancelledError", str(exc) or repr(exc), None) from exc


async def abort_job(job: Job, timeout: float) -> bool:
    """``Job.abort``, but a cancellation of the CALLING task re-raises instead of
    being swallowed as the job's confirmed-abort verdict."""
    aborted = await job.abort(timeout=timeout)
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError
    return aborted


async def request_job_abort(job: Job) -> bool:
    """Request cancellation of ``job`` without blocking on fleet liveness.

    Returns ``True`` when the job is guaranteed not to run (cancellation
    confirmed, or the job already finished/vanished — a finished job's
    ``Job.abort`` replays its stored outcome, translated back to ``True`` via a
    status re-check). Returns ``False`` when the request is recorded but
    confirmation is still pending (the worker cancels at pick-up). Every other
    failure (connection errors, undecodable payloads) propagates.
    """
    status = await job.status()
    if status in (JobStatus.not_found, JobStatus.complete):
        return True
    try:
        if await abort_job(job, timeout=0):
            return True
    except TimeoutError:
        logger.info("abort of job %s requested; cancellation will be confirmed by the worker", job.job_id)
        return False
    except Exception:
        if await job.status() in (JobStatus.not_found, JobStatus.complete):
            # Finished/vanished during the abort poll: guaranteed not to run.
            return True
        raise
    if await job.status() in (JobStatus.not_found, JobStatus.complete):
        # abort returned False on a replayed success / no retained result:
        # finished or gone, guaranteed not to run.
        return True
    logger.info("abort of job %s requested; cancellation will be confirmed by the worker", job.job_id)
    return False


# The full durable definition stored in a schedule hash. A transition may create
# a missing hash only when it writes all of these fields at once.
_SCHEDULE_DEFINITION_FIELDS = frozenset({"target", "args", "kwargs", "schedule", "cron_or_interval", "enabled"})


def schedule_lock(redis: Any, schedule_name: str) -> Any:
    """The per-schedule mutation lock: every state change (transition, flag write,
    delete) acquires it, so an existence check made under it stays true for the
    write that follows."""
    return redis.lock(arq_settings().arq_schedule_lock_key(schedule_name), timeout=5, blocking_timeout=20)


async def safe_schedule_transition(
    redis: Any,
    schedule_name: str,
    defer_by: float,
    last_scheduled_ts: float,
    mapping_updates: dict[str, Any] | None = None,
    enforce_job_id: str | None = None,
) -> Any:
    """Atomically replace a schedule's pending job and update its hash.

    ``enforce_job_id`` makes the transition conditional (self-reschedule path):
    it proceeds only while the hash still points at that job, else it is skipped.
    Without it, any pending job is aborted before its replacement is enqueued. A
    transition never resurrects a deleted schedule — a gone hash without a full
    definition in ``mapping_updates`` is skipped and returns ``None``.
    """
    settings = arq_settings()
    key = settings.arq_schedule_key(schedule_name)

    async with schedule_lock(redis, schedule_name):
        current_data = await redis.hgetall(key)
        current_job_id = current_data.get(b"job_id", b"").decode()

        if not current_data and not (mapping_updates and _SCHEDULE_DEFINITION_FIELDS.issubset(mapping_updates)):
            logger.info("Schedule '%s' transition skipped: schedule was deleted.", schedule_name)
            return None

        if enforce_job_id and current_job_id != enforce_job_id:
            logger.info("Schedule '%s' transition skipped: preempted by external update.", schedule_name)
            return None

        if not enforce_job_id and current_job_id:
            # Abort the job being replaced first; a failed abort propagates
            # rather than leave the old job live alongside its replacement.
            await request_job_abort(Job(current_job_id, redis=redis, _deserializer=job_deserializer))

        next_job_id = uuid.uuid4().hex
        new_mapping: dict[str, Any] = {
            "job_id": next_job_id,
            "last_scheduled_ts": str(last_scheduled_ts),
        }
        if mapping_updates:
            new_mapping.update(mapping_updates)

        # Name the replacement id in the hash BEFORE enqueueing the job under it,
        # so a worker never picks it up while the hash still points at its
        # predecessor. A crash between the two writes leaves a not-found job the
        # startup watchdog recovers.
        await redis.hset(key, mapping=new_mapping)
        next_job = await redis.enqueue_job("task_scheduler", schedule_name, _job_id=next_job_id, _defer_by=defer_by)
        if next_job is None:
            raise RuntimeError(f"Schedule '{schedule_name}': job id {next_job_id} already exists; nothing enqueued")
        return next_job


async def abort_schedule_task(key: str) -> None:
    """Abort the pending job of the schedule hash at ``key``. The ``aborted``
    marker is set to the job id first, so even a job whose cancellation is never
    processed exits without effect. Failures propagate."""
    arq_redis: Any = await RedisPoolManager.get()

    if await arq_redis.exists(key):
        data = await arq_redis.hgetall(key)
        prev_job_id = data.get(b"job_id", b"").decode()
        if prev_job_id:
            await arq_redis.hset(key, "aborted", prev_job_id)
            await request_job_abort(Job(prev_job_id, redis=arq_redis, _deserializer=job_deserializer))


async def recover_stalled_schedules(ctx: dict[str, Any]) -> None:
    """Startup watchdog: restart enabled schedules whose pending job is missing,
    not found, or complete, each under a short recovery lock so concurrent
    startups recover it once. A per-schedule failure is logged, not fatal."""
    redis = ctx["redis"]
    settings = arq_settings()
    logger.info("Watchdog: checking for stalled schedules...")

    schedule_keys = [key async for key in redis.scan_iter(match=settings.arq_schedule_pattern.encode())]

    recovered_count = 0

    for key in schedule_keys:
        try:
            name = key.decode().split(":")[-1]
            lock_key = settings.arq_schedule_recovery_lock_key(name)
            data = await redis.hgetall(key)

            enabled = data.get(b"enabled", b"false").decode("utf-8") == "true"
            if not enabled:
                continue

            job_id = data.get(b"job_id", b"").decode("utf-8")
            should_recover = False

            if not job_id:
                should_recover = True
            else:
                job = Job(job_id, redis=redis, _deserializer=job_deserializer)
                status = await job.status()

                # not_found: pending job lost. complete: ran but never rescheduled.
                if status in (JobStatus.not_found, JobStatus.complete):
                    should_recover = True

            if should_recover and await redis.set(lock_key, "locked", nx=True, ex=10):
                logger.warning("Watchdog: schedule '%s' is stalled. Restarting.", name)
                restarted = await safe_schedule_transition(
                    redis,
                    name,
                    defer_by=0,
                    last_scheduled_ts=datetime.now(UTC).timestamp(),
                    enforce_job_id=None,
                )
                # None: schedule deleted between scan and transition.
                if restarted is not None:
                    recovered_count += 1

        except Exception:
            # One broken schedule must not stop recovery of the rest.
            logger.error("Watchdog error checking schedule %s", key, exc_info=True)

    logger.info("Watchdog: recovery complete. Restarted %d schedules.", recovered_count)


async def task_scheduler(ctx: dict[str, Any], schedule_name: str) -> Any:
    """Self-rescheduling worker function driving one schedule. When due it
    enqueues the target (if enabled), waits for the result, then defers its own
    replacement conditioned on this job still being the recorded pending job. A
    stale invocation exits without running the target, so a schedule never fires
    twice."""
    settings = arq_settings()
    key = settings.arq_schedule_key(schedule_name)

    # Fast exit if the schedule was deleted while this job was pending.
    if not await ctx["redis"].exists(key):
        return None

    data = await ctx["redis"].hgetall(key)

    # Abort marker naming this job: it was replaced/deleted — no run.
    if not data or data.get(b"aborted", b"").decode("utf-8") == ctx["job_id"]:
        return None

    # Only the recorded pending job drives the schedule.
    if data.get(b"job_id", b"").decode("utf-8") != ctx["job_id"]:
        logger.info("Schedule '%s': job %s is no longer the pending job; skipping.", schedule_name, ctx["job_id"])
        return None

    enabled = data.get(b"enabled", b"false").decode("utf-8") == "true"
    target = data.get(b"target", b"").decode("utf-8")

    args = orjson.loads(data.get(b"args", b"[]"))
    kwargs = orjson.loads(data.get(b"kwargs", b"{}"))

    cron_or_interval = parse_cron_or_interval(data.get(b"cron_or_interval", b"").decode("utf-8"))

    last_scheduled_ts_str = data.get(b"last_scheduled_ts", b"0").decode("utf-8")
    try:
        last_scheduled_ts = float(last_scheduled_ts_str)
    except ValueError:
        last_scheduled_ts = 0.0

    now = datetime.now(UTC)
    now_ts = now.timestamp()

    base_time = now if last_scheduled_ts == 0 else datetime.fromtimestamp(last_scheduled_ts, tz=UTC)

    if isinstance(cron_or_interval, str):
        next_time = croniter(cron_or_interval, base_time).get_next(datetime)
        if next_time.timestamp() < now_ts:
            # Computed slot is in the past (missed runs): realign to the next slot.
            next_time = croniter(cron_or_interval, now).get_next(datetime)
    else:
        # ``parse_cron_or_interval`` yields a number for anything non-crontab.
        next_ts = base_time.timestamp() + cron_or_interval
        if next_ts < (now_ts - 60):
            # More than a minute behind: realign relative to now.
            next_time = now + timedelta(seconds=cron_or_interval)
        else:
            next_time = datetime.fromtimestamp(next_ts, tz=UTC)

    next_run_ts = next_time.timestamp()
    defer_by = next_run_ts - datetime.now(UTC).timestamp()

    try:
        if enabled:
            job = await ctx["redis"].enqueue_job(target, *args, **kwargs)
            # wait_job_result surfaces an aborted target as TaskFailedError, not
            # a raw CancelledError that would read as a cancellation of THIS job.
            return await wait_job_result(job)
    finally:
        await safe_schedule_transition(
            ctx["redis"],
            schedule_name,
            defer_by=defer_by,
            last_scheduled_ts=next_run_ts,
            enforce_job_id=ctx["job_id"],
        )
