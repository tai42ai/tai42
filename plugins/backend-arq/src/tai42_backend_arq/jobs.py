"""Locating an arq job by id across this deployment's queues.

arq stores no queue on a queued job, and a ``Job`` handle reads only its own queue's
sorted set, so a handle on another queue reads a queued job as not found. Every
derived queue a task is sent to is recorded in the queue registry
(:attr:`~tai42_backend_arq.settings.ArqSettings.arq_queue_registry_key`), and every
handle built from a bare id is pinned to the queue holding that id.

``redis`` is typed ``Any``: redis-py types each command's return as the union of its
sync and async shapes, while the asyncio client always returns an awaitable.
"""

from __future__ import annotations

from typing import Any

from arq.jobs import Job

from tai42_backend_arq.settings import arq_settings, job_deserializer


async def register_queue(redis: Any, queue: str) -> None:
    """Record ``queue`` in the queue registry; the settings queue is implied and never stored."""
    settings = arq_settings()
    if queue != settings.queue_name:
        await redis.sadd(settings.arq_queue_registry_key, queue)


async def known_queues(redis: Any) -> list[str]:
    """The settings queue, then every registered derived queue in sorted order."""
    settings = arq_settings()
    registered = {member.decode() for member in await redis.smembers(settings.arq_queue_registry_key)}
    return [settings.queue_name, *sorted(registered - {settings.queue_name})]


async def job_handle(redis: Any, job_id: str) -> Job:
    """A ``Job`` handle for ``job_id`` pinned to the known queue holding it.

    An id on no known queue (finished, result only, or never sent) gets the settings
    queue: arq then answers from its per-id result and in-progress keys alone. An id on
    more than one queue raises ``RuntimeError``.
    """
    queues = await known_queues(redis)
    async with redis.pipeline(transaction=False) as pipe:
        for queue in queues:
            pipe.zscore(queue, job_id)
        scores = await pipe.execute()
    hits = [queue for queue, score in zip(queues, scores, strict=True) if score is not None]
    if len(hits) > 1:
        raise RuntimeError(f"job {job_id} is queued on more than one queue: {hits}")
    queue = hits[0] if hits else queues[0]
    return Job(job_id, redis, _queue_name=queue, _deserializer=job_deserializer)
