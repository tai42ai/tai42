"""The ``queue`` task option: a label that picks the worker queue a task is sent to.

The task door derives the arq queue key
``<ARQ_QUEUE_NAME>.<label>`` from it and records every derived key it sends to in the
queue registry ``<ARQ_QUEUE_NAME>:queues``. Any other value is refused before Redis
is written.
"""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import SETTINGS_QUEUE, submit_job

_REGISTRY = f"{SETTINGS_QUEUE}:queues"


async def _keys(redis: Any) -> list[str]:
    return sorted(key.decode() for key in await redis.keys("*"))


@pytest.mark.parametrize("label", ["", "a:b", "a.b", "a b"])
async def test_enqueue_refuses_a_label_outside_the_rule_before_writing(arq_redis, label) -> None:
    with pytest.raises(ValueError, match=r"must match \[A-Za-z0-9_-\]\+"):
        await submit_job(queue=label)

    assert await _keys(arq_redis) == []


async def test_enqueue_with_a_label_sends_the_job_to_the_derived_queue(arq_redis) -> None:
    job = await submit_job(queue="gpu")

    assert await arq_redis.zscore(f"{SETTINGS_QUEUE}.gpu", job.job_id) is not None
    assert await arq_redis.zscore(SETTINGS_QUEUE, job.job_id) is None


async def test_enqueue_without_a_label_sends_the_job_to_the_settings_queue(arq_redis) -> None:
    job = await submit_job(queue=None)

    assert await arq_redis.zscore(SETTINGS_QUEUE, job.job_id) is not None


async def test_enqueue_with_a_label_records_the_derived_queue_in_the_registry(arq_redis) -> None:
    await submit_job(queue="gpu")

    assert await arq_redis.smembers(_REGISTRY) == {f"{SETTINGS_QUEUE}.gpu".encode()}


async def test_enqueue_without_a_label_leaves_the_registry_untouched(arq_redis) -> None:
    await submit_job()

    assert await arq_redis.exists(_REGISTRY) == 0
