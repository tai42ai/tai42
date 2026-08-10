"""Bind a recording stub app before the backend package is imported.

``tai42_backend_rq`` registers its backend class, tools, and extensions on the
global ``tai42_app`` handle at import time. Binding a stub here (at collection
time, before any test imports the package) captures those registrations for tests
to assert on. The stub also carries the app facets the source reaches at call time
(resource manager, monitoring writer, tool runner); the autouse ``_reset_stub``
fixture resets per-test knobs.
"""

from __future__ import annotations

import fnmatch
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from tai42_contract.app import tai42_app


class RecordingTools:
    """Records ``@tai42_app.tools.tool`` registrations and ``run_tool`` calls."""

    def __init__(self) -> None:
        self.registered: list[str] = []
        self.tags: dict[str, set[str]] = {}
        self.run_calls: list[tuple[str, dict[str, Any]]] = []
        self.run_result: Any = None
        self.run_error: Exception | None = None

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        tags = kwargs.get("tags", set())
        if len(args) == 1 and callable(args[0]) and not kwargs:
            self.registered.append(args[0].__name__)
            self.tags[args[0].__name__] = tags
            return args[0]

        def decorator(func: Any) -> Any:
            self.registered.append(func.__name__)
            self.tags[func.__name__] = tags
            return func

        return decorator

    async def run_tool(self, key: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        self.run_calls.append((key, arguments))
        if self.run_error is not None:
            raise self.run_error
        return self.run_result


class RecordingExtensions:
    """Records ``@tai42_app.extensions.extension`` registrations."""

    def __init__(self) -> None:
        self.registered: list[tuple[str | None, Any]] = []

    def extension(self, f: Any = None, *, kind: Any, name: str | None = None) -> Any:
        def decorator(func: Any) -> Any:
            self.registered.append((name or func.__name__, kind))
            return func

        return decorator(f) if f is not None else decorator


class RecordingBackends:
    """Records ``@tai42_app.backends.register_backend`` registrations."""

    def __init__(self) -> None:
        self.registered: list[type] = []

    def register_backend(self, cls: type | None = None) -> Any:
        def decorator(inner: type) -> type:
            self.registered.append(inner)
            return inner

        return decorator(cls) if cls is not None else decorator


class StubResourceManager:
    """Renders by echoing the inline content (or a canned template body)."""

    def __init__(self) -> None:
        self.templates: dict[str, str] = {}

    async def render_by_id_or_content(
        self, *, content: str | None, template_id: str | None, kwargs: dict[str, Any] | None
    ) -> str:
        if content is not None:
            return content
        if template_id is not None:
            return self.templates[template_id]
        return ""


class StubStorage:
    def __init__(self) -> None:
        self.resource_manager = StubResourceManager()


class StubWriter:
    def __init__(self) -> None:
        self.shutdown_calls = 0
        self.flush_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def flush(self) -> None:
        self.flush_calls += 1


class StubMonitoringBackend:
    def __init__(self) -> None:
        self.writer = StubWriter()


class StubMonitoring:
    def __init__(self) -> None:
        self.active = StubMonitoringBackend()


class StubLifecycle:
    """The app double is always ready, so ``wait_until_ready`` resolves at once —
    the readiness gate ``run_rq_worker`` awaits before its work loop starts."""

    async def wait_until_ready(self) -> None:
        return None


class StubApp:
    def __init__(self) -> None:
        self.tools = RecordingTools()
        self.extensions = RecordingExtensions()
        self.backends = RecordingBackends()
        self.storage = StubStorage()
        self.monitoring = StubMonitoring()
        self.lifecycle = StubLifecycle()


stub_app = StubApp()
tai42_app.bind(stub_app)


@pytest.fixture(autouse=True)
def _reset_stub() -> AsyncIterator[None] | Any:
    """Reset per-test stub state (import-time registrations are kept)."""
    stub_app.tools.run_calls.clear()
    stub_app.tools.run_result = None
    stub_app.tools.run_error = None
    stub_app.monitoring.active.writer.shutdown_calls = 0
    stub_app.monitoring.active.writer.flush_calls = 0
    stub_app.storage.resource_manager.templates.clear()
    return None


@pytest.fixture(autouse=True)
def _reset_process_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the manifest payload env between tests, so an ambient value or one a
    prior test set cannot leak into the next. Set through ``monkeypatch``, which
    restores it when the test ends.
    """
    from tai42_backend_rq.settings import rq_settings

    monkeypatch.delenv(rq_settings().manifest_key, raising=False)


@pytest.fixture
def app() -> StubApp:
    return stub_app


# --- Shared fakes ----------------------------------------------------------


def make_client_ctx(client: Any) -> Callable[..., Any]:
    """A ``client_ctx``-shaped factory that always yields ``client``."""

    @asynccontextmanager
    async def _ctx(client_cls: Any, settings: Any = None, **kwargs: Any) -> AsyncIterator[Any]:
        yield client

    return _ctx


class FakeAsyncRedis:
    """Minimal async Redis stand-in over in-memory hash/stream/zset/kv maps."""

    def __init__(
        self,
        hashes: dict[str, dict[Any, Any]] | None = None,
        streams: dict[str, list[Any]] | None = None,
        sets: dict[str, set[Any]] | None = None,
        zsets: dict[str, dict[str, float]] | None = None,
        lists: dict[str, list[Any]] | None = None,
        kv: dict[str, str] | None = None,
    ) -> None:
        self.hashes = hashes or {}
        self.streams = streams or {}
        self.sets = sets or {}
        self.zsets = zsets or {}
        self.lists = lists or {}
        self.kv = kv or {}

    async def hget(self, key: str, field: str) -> Any:
        return self.hashes.get(key, {}).get(field)

    async def hgetall(self, key: str) -> dict[Any, Any]:
        return self.hashes.get(key, {})

    async def smembers(self, key: str) -> set[Any]:
        return self.sets.get(key, set())

    async def xrevrange(self, key: str, count: int | None = None) -> list[Any]:
        entries = list(reversed(self.streams.get(key, [])))
        return entries[:count] if count is not None else entries

    async def zrange(self, key: str, start: int, end: int, withscores: bool = False) -> list[Any]:
        items = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])
        return [(m, s) for m, s in items] if withscores else [m for m, _ in items]

    async def zscore(self, key: str, member: str) -> float | None:
        return self.zsets.get(key, {}).get(member)

    async def zrem(self, key: str, member: str) -> int:
        zset = self.zsets.get(key, {})
        if member in zset:
            del zset[member]
            return 1
        return 0

    async def lrange(self, key: str, start: int, end: int) -> list[Any]:
        return self.lists.get(key, [])

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            removed += int(self.kv.pop(key, None) is not None)
            removed += int(self.hashes.pop(key, None) is not None)
        return removed

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.kv[key] = value

    def scan_iter(self, match: str | None = None) -> Any:
        # ``async for`` consumes an async iterator, so this is a plain method
        # returning an async generator (mirroring the real client).
        keys = [k for k in self.kv if match is None or fnmatch.fnmatch(k, match)]

        async def _gen() -> Any:
            for key in keys:
                yield key

        return _gen()
