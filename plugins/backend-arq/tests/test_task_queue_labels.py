"""The ``queue`` task option: a label that picks the worker queue a task is sent to.

A label is letters, digits, ``_`` or ``-``. The plugin derives the arq queue key
``<ARQ_QUEUE_NAME>.<label>`` from it and records every derived key it sends to in the
queue registry ``<ARQ_QUEUE_NAME>:queues``. Any other value is refused before Redis
is written.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from tai42_backend_arq import extensions, tasks
from tai42_backend_arq.queues import check_queue_label, derived_queue

from .conftest import SETTINGS_QUEUE

_REGISTRY = f"{SETTINGS_QUEUE}:queues"


async def sample_tool(text: str) -> str:
    """Echo ``text``."""
    return text


async def _keys(redis: Any) -> list[str]:
    return sorted(key.decode() for key in await redis.keys("*"))


@pytest.mark.parametrize("label", ["", "a:b", "a.b", "a b"])
async def test_enqueue_refuses_a_label_outside_the_rule_before_writing(arq_redis, label) -> None:
    with pytest.raises(ValueError, match=r"must match \[A-Za-z0-9_-\]\+"):
        await tasks.enqueue_task(arq_redis, queue=label, backend_tool_name="t")

    assert await _keys(arq_redis) == []


async def test_enqueue_with_a_label_sends_the_job_to_the_derived_queue(arq_redis) -> None:
    job = await tasks.enqueue_task(arq_redis, queue="gpu", backend_tool_name="t")

    assert await arq_redis.zscore(f"{SETTINGS_QUEUE}.gpu", job.job_id) is not None
    assert await arq_redis.zscore(SETTINGS_QUEUE, job.job_id) is None


async def test_enqueue_without_a_label_sends_the_job_to_the_settings_queue(arq_redis) -> None:
    job = await tasks.enqueue_task(arq_redis, queue=None, backend_tool_name="t")

    assert await arq_redis.zscore(SETTINGS_QUEUE, job.job_id) is not None


async def test_enqueue_with_a_label_records_the_derived_queue_in_the_registry(arq_redis) -> None:
    await tasks.enqueue_task(arq_redis, queue="gpu", backend_tool_name="t")

    assert await arq_redis.smembers(_REGISTRY) == {f"{SETTINGS_QUEUE}.gpu".encode()}


async def test_enqueue_without_a_label_leaves_the_registry_untouched(arq_redis) -> None:
    await tasks.enqueue_task(arq_redis, backend_tool_name="t")

    assert await arq_redis.exists(_REGISTRY) == 0


def test_a_label_is_letters_digits_underscore_or_dash() -> None:
    assert check_queue_label("gpu_2-b") == "gpu_2-b"


def test_no_label_derives_the_settings_queue(arq_redis) -> None:
    assert derived_queue(None) == SETTINGS_QUEUE
    assert derived_queue("batch-2") == f"{SETTINGS_QUEUE}.batch-2"


@pytest.mark.parametrize("factory", [extensions.sync_task, extensions.async_task])
def test_the_task_branches_take_an_optional_queue_label(factory) -> None:
    branch = factory(sample_tool, "sample_tool", "doc")

    queue = inspect.signature(branch).parameters["queue"]
    assert queue.kind is inspect.Parameter.KEYWORD_ONLY
    assert queue.default is None
    assert queue.annotation == str | None


async def _tool_with_its_own_queue(queue: str) -> str:
    """A tool whose own parameter is named ``queue``."""
    return queue


@pytest.mark.parametrize("factory", [extensions.sync_task, extensions.async_task])
def test_a_tool_owning_a_queue_parameter_cannot_take_a_task_branch(factory) -> None:
    with pytest.raises(ValueError, match="duplicate parameter name: 'queue'"):
        factory(_tool_with_its_own_queue, "own_queue", "doc")
