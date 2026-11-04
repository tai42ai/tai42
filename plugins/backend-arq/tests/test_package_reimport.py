"""A config reload re-imports this plugin's whole package between the worker runtime's build and its run.

The host re-imports the package named as its ``backend_module`` on every epoch build, the
first-connect resync before the worker's boot-ready latch included, so the package's import-time
registrations re-fire into each epoch. The test builds the runtime, re-imports the package the way
the host does (every module of it popped from ``sys.modules``, then imported again), and then runs
jobs enqueued by name through the built worker, over an in-process fake Redis server: they run the
job functions its build wrapped, never the re-imported copies.
"""

from __future__ import annotations

import asyncio
import importlib
import pkgutil
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from types import ModuleType
from typing import Any

import arq.worker
import pytest
from arq.connections import ArqRedis, RedisSettings, create_pool
from tai42_kit.fork_gate import fork_gate

import tai42_backend_arq
from tai42_backend_arq import worker
from tai42_backend_arq.settings import job_deserializer, job_serializer
from tai42_backend_arq.worker import ArqWorkerRuntime

from .conftest import StubApp

_PACKAGE = tai42_backend_arq.__name__
_FUNCTIONS = ("task_scheduler", "callback_job", "tool_execution")
_SPANNED = ("callback_job", "tool_execution")
_RUNS_WITHIN = 5.0

# arq's own ``Worker.close`` closes its pool through redis-py's deprecated ``close``.
pytestmark = pytest.mark.filterwarnings("ignore:Call to deprecated close:DeprecationWarning")


def _package_module_names() -> list[str]:
    """Every module of the package, the package root first, as the host's re-import walks it."""
    return [_PACKAGE] + [info.name for info in pkgutil.walk_packages(tai42_backend_arq.__path__, prefix=f"{_PACKAGE}.")]


@pytest.fixture
def reimport_package(stub_app: StubApp, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], ModuleType]]:
    """Re-import the plugin's package as the host's reload does; the test process gets its own copies back after.

    The re-import registers into the stub app, as it does into the host's; what it registered is
    undone with ``sys.modules``.
    """
    names = _package_module_names()
    kept_modules = {name: module for name, module in sys.modules.items() if name.split(".")[0] == _PACKAGE}
    monkeypatch.setattr(stub_app.tools, "registered", dict(stub_app.tools.registered))
    monkeypatch.setattr(stub_app.tools, "tags", dict(stub_app.tools.tags))
    monkeypatch.setattr(stub_app.extensions, "registered", dict(stub_app.extensions.registered))
    monkeypatch.setattr(stub_app.backends, "registered_cls", stub_app.backends.registered_cls)
    monkeypatch.setattr(stub_app.backends, "instance", stub_app.backends.instance)

    def _reimport() -> ModuleType:
        for name in [name for name in sys.modules if name.split(".")[0] == _PACKAGE]:
            del sys.modules[name]
        for name in names:
            importlib.import_module(name)
        fresh = sys.modules[_PACKAGE]
        assert fresh is not tai42_backend_arq
        return fresh

    yield _reimport

    for name in [name for name in sys.modules if name.split(".")[0] == _PACKAGE]:
        del sys.modules[name]
    sys.modules.update(kept_modules)


@pytest.fixture
def job_bodies(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[int]]:
    """Each job function the build wraps replaced by one that records the job spans it ran inside, keyed by name."""
    seen: dict[str, list[int]] = {name: [] for name in _FUNCTIONS}

    def recorder(name: str) -> Any:
        async def body(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> str:
            seen[name].append(fork_gate.live_spans)
            return name

        body.__qualname__ = name
        return body

    for name in _FUNCTIONS:
        monkeypatch.setattr(worker, name, recorder(name))
    return seen


@pytest.fixture
async def queue(redis_url: str, settings_queue: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ArqRedis]:
    """An enqueue handle on the fake Redis server the worker consumes, on the settings queue."""
    # A worker's start logs the server's ``INFO``, a command the fake server does not serve.
    monkeypatch.setattr(arq.worker, "log_redis_info", _no_redis_info)
    redis = await create_pool(
        RedisSettings.from_dsn(redis_url),
        job_serializer=job_serializer,
        job_deserializer=job_deserializer,
        default_queue_name=settings_queue,
    )
    try:
        yield redis
    finally:
        await redis.aclose()


async def _no_redis_info(redis: Any, log_func: Any) -> None:
    return None


async def test_after_a_reimport_the_worker_runs_its_jobs_through_the_functions_its_build_wrapped(
    reimport_package: Callable[[], ModuleType], job_bodies: dict[str, list[int]], queue: ArqRedis
) -> None:
    runtime = ArqWorkerRuntime.from_args(["--burst"])
    runtime.bind_progress(lambda *, next_within: None)
    await runtime.build()
    try:
        built = {name: runtime.worker.functions[name].coroutine for name in _FUNCTIONS}

        reimport_package()
        assert sys.modules[worker.__name__] is not worker

        assert {name: runtime.worker.functions[name].coroutine for name in _FUNCTIONS} == built
        for name in _SPANNED:
            assert await queue.enqueue_job(name) is not None
        await asyncio.wait_for(runtime.run_on_loop(), _RUNS_WITHIN)
    finally:
        await runtime.aclose()

    # Each job ran the body the build wrapped, inside the fork gate's job span the wrap holds.
    assert job_bodies == {"task_scheduler": [], "callback_job": [1], "tool_execution": [1]}
