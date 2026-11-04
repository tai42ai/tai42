"""Every kit Redis client the backend leases takes the full ``RQ_*`` connection settings.

The worker inspection, the callback job, the task door and the schedule store each lease a pooled
client through ``client_ctx`` with :func:`~tai42_backend_rq.settings.rq_client_kwargs`: the
URL, the connection cap, the socket timeouts and the kit's connection retry, read as bytes.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from tai42_contract.backend import IntervalCadence, ScheduleRecord
from tai42_kit.backend import CallbackSchema
from tai42_kit.clients.impl.redis import RedisClient, SyncRedisClient
from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG

from tai42_backend_rq import inspection, schedule_store, task_door, tasks
from tai42_backend_rq.settings import rq_client_kwargs, rq_settings

_EXPECTED = {
    "url": "redis://configured:6390/2",
    "max_connections": 9,
    "decode_responses": False,
    "env_prefix": "RQ_",
    "socket_timeout": 3.0,
    "retry_attempts": 2,
}


class _LeasedError(Exception):
    """Raised by the recording lease once it has seen the kwargs, so the door stops there."""


@pytest.fixture(autouse=True)
def _rq_connection_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in list(os.environ):
        if key.startswith(("RQ_", "TAI_DEFAULT_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RQ_REDIS_URL", "redis://configured:6390/2")
    monkeypatch.setenv("RQ_REDIS_MAX_CONNECTIONS", "9")
    monkeypatch.setenv("RQ_SOCKET_TIMEOUT", "3")
    monkeypatch.setenv("RQ_RETRY_ATTEMPTS", "2")
    rq_settings.cache_clear()
    yield
    rq_settings.cache_clear()


def _recording_lease(seen: list[tuple[Any, dict[str, Any]]]) -> Callable[..., Any]:
    @asynccontextmanager
    async def _lease(client_cls: Any, settings: Any = None, **kwargs: Any) -> AsyncIterator[Any]:
        assert settings is None
        seen.append((client_cls, kwargs))
        raise _LeasedError
        yield  # pragma: no cover - the lease stops before it yields

    return _lease


def test_the_client_kwargs_are_the_connection_settings_read_as_bytes() -> None:
    assert rq_client_kwargs() == _EXPECTED


@pytest.mark.parametrize(
    ("lease", "client_cls"), [(inspection._async_redis, RedisClient), (inspection._sync_redis, SyncRedisClient)]
)
async def test_the_inspection_leases_with_the_full_settings(
    monkeypatch: pytest.MonkeyPatch, lease: Callable[[], Any], client_cls: type
) -> None:
    seen: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(inspection, "client_ctx", _recording_lease(seen))

    with pytest.raises(_LeasedError):
        async with lease():
            pass

    assert seen == [(client_cls, _EXPECTED)]


async def test_the_callback_job_leases_with_the_full_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(tasks, "client_ctx", _recording_lease(seen))

    with pytest.raises(_LeasedError):
        await tasks.callback_job(
            "job-1", CallbackSchema(tool="next_tool", carried_kwargs={WORKER_SECRET_CAPABILITY_ARG: False})
        )

    assert seen == [(SyncRedisClient, _EXPECTED)]


async def test_the_task_door_leases_with_the_full_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(task_door, "client_ctx", _recording_lease(seen))

    with pytest.raises(_LeasedError):
        await task_door.RqTaskDoor().submit((), {}, {}, None)

    assert seen == [(SyncRedisClient, _EXPECTED)]


async def test_the_schedule_store_leases_with_the_full_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(schedule_store, "client_ctx", _recording_lease(seen))
    record = ScheduleRecord(
        name="nightly", tool="sample_tool", args=[], kwargs={"x": 7}, cadence=IntervalCadence(every=3600), enabled=True
    )

    with pytest.raises(_LeasedError):
        await schedule_store.RqScheduleStore().write(record)

    assert seen == [(SyncRedisClient, _EXPECTED)]
