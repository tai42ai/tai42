"""The ``queue`` task option and the worker ``--queue`` flag: a label that picks the worker queue.

The kit's label grammar decides what a label is; the RQ door and worker derive the RQ queue
name ``<RQ_QUEUE_NAME>.<label>`` from it, on the enqueue side and on the worker side, and any
other value is refused before Redis is written or the worker is built.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import click
import fakeredis
import pytest
from rq.job import Job
from tai42_kit.backend import CallbackSchema
from tai42_kit.settings import reset_all_settings
from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG

from tai42_backend_rq import task_door
from tai42_backend_rq import worker as worker_module
from tai42_backend_rq.queue import TaskBudgetQueue
from tai42_backend_rq.task_door import RqTaskDoor

from .conftest import make_client_ctx

_SETTINGS_QUEUE = "ns-1:default"
_GPU = f"{_SETTINGS_QUEUE}.gpu"


def _no_report(*, next_within: float) -> None:
    """A progress reporter that drops every report."""


@pytest.fixture
def settings_queue(monkeypatch) -> Iterator[str]:
    monkeypatch.setenv("RQ_QUEUE_NAME", _SETTINGS_QUEUE)
    reset_all_settings()
    try:
        yield _SETTINGS_QUEUE
    finally:
        reset_all_settings()


@pytest.fixture
def redis(settings_queue, monkeypatch) -> fakeredis.FakeStrictRedis:
    """An in-memory Redis both the enqueue side and the worker build connect to."""
    conn = fakeredis.FakeStrictRedis(server=fakeredis.FakeServer())
    monkeypatch.setattr(task_door, "client_ctx", make_client_ctx(conn))
    monkeypatch.setattr(worker_module, "rq_redis_connection", lambda redis_url: conn)
    return conn


def _queue(redis: Any, name: str) -> TaskBudgetQueue:
    return TaskBudgetQueue(name, connection=redis)


async def _submit(callback: CallbackSchema | None = None, **options: Any) -> Job:
    """Submit a ``t`` job through the RQ door with ``options``."""
    return await RqTaskDoor().submit((), {"backend_tool_name": "t"}, options, callback)


@pytest.mark.parametrize("label", ["", "a:b", "a.b", "a b"])
async def test_enqueue_refuses_a_label_outside_the_rule_before_writing(redis, label) -> None:
    with pytest.raises(ValueError, match=r"must match \[A-Za-z0-9_-\]\+"):
        await _submit(queue=label)

    assert redis.keys("rq:job:*") == []
    assert redis.smembers("rq:queues") == set()


async def test_enqueue_with_a_label_sends_the_job_to_the_derived_queue(redis) -> None:
    job = await _submit(queue="gpu")

    assert _queue(redis, _GPU).job_ids == [job.id]
    assert Job.fetch(job.id, connection=redis).origin == _GPU
    assert _queue(redis, _SETTINGS_QUEUE).job_ids == []


async def test_enqueue_without_a_label_sends_the_job_to_the_settings_queue(redis) -> None:
    job = await _submit()

    assert _queue(redis, _SETTINGS_QUEUE).job_ids == [job.id]
    assert Job.fetch(job.id, connection=redis).origin == _SETTINGS_QUEUE


async def test_a_labelled_jobs_callback_waits_on_the_same_queue(redis) -> None:
    callback = CallbackSchema(tool="next", carried_kwargs={WORKER_SECRET_CAPABILITY_ARG: False})

    job = await _submit(callback, queue="gpu")

    [callback_id] = _queue(redis, _GPU).deferred_job_registry.get_job_ids()
    assert Job.fetch(callback_id, connection=redis).origin == _GPU
    assert Job.fetch(callback_id, connection=redis).dependency_ids == [job.id]


async def test_a_labelled_countdown_job_waits_in_its_queues_scheduled_registry(redis) -> None:
    job = await _submit(queue="gpu", countdown=30)

    assert _queue(redis, _GPU).scheduled_job_registry.get_job_ids() == [job.id]
    assert _queue(redis, _SETTINGS_QUEUE).scheduled_job_registry.get_job_ids() == []


async def test_a_labelled_eta_job_waits_in_its_queues_scheduled_registry(redis) -> None:
    eta = datetime.now(UTC) + timedelta(hours=1)

    job = await _submit(queue="gpu", eta=eta.isoformat())

    assert _queue(redis, _GPU).scheduled_job_registry.get_job_ids() == [job.id]


def test_a_worker_on_a_label_serves_the_derived_queue_alone(redis) -> None:
    worker, _conn = worker_module._build_worker(None, None, 500, "solo", "gpu", _no_report)

    assert worker.queue_names() == [_GPU]
    assert isinstance(worker.queues[0], TaskBudgetQueue)


def test_a_worker_without_a_label_serves_the_settings_queue(redis) -> None:
    worker, _conn = worker_module._build_worker(None, None, 500, "solo", None, _no_report)

    assert worker.queue_names() == [_SETTINGS_QUEUE]


async def test_the_worker_reads_the_settings_queue_at_build(redis, monkeypatch) -> None:
    runtime = cast("Any", worker_module.RqWorkerRuntime.from_args(["--queue", "gpu", "--pool", "solo"]))
    monkeypatch.setenv("RQ_QUEUE_NAME", "ns-2:default")
    reset_all_settings()

    await runtime.build()

    assert runtime._worker.queue_names() == ["ns-2:default.gpu"]


def test_the_worker_runtime_parses_the_label() -> None:
    runtime = cast("Any", worker_module.RqWorkerRuntime.from_args(["--queue", "gpu"]))

    assert runtime._params["queue"] == "gpu"


async def test_the_worker_runtime_build_passes_the_label(monkeypatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(worker_module, "_build_worker", lambda *args: calls.append(args) or (object(), None))
    runtime = cast("Any", worker_module.RqWorkerRuntime.from_args(["--queue", "gpu"]))

    await runtime.build()

    assert calls == [(None, None, 500, "prefork", "gpu", runtime.report_progress)]


def test_a_worker_label_outside_the_rule_is_a_bad_parameter() -> None:
    with pytest.raises(click.BadParameter, match=r"must match \[A-Za-z0-9_-\]\+"):
        worker_module.RqWorkerRuntime.from_args(["--queue", "a:b"])
