"""Bind a recording stub app before the plugin packages are imported.

The plugin registers everything through the global ``tai42_app`` handle at import
time (tools, extensions, the backend class), so a stub app is bound here — at
collection time, before any test module import — and records what was
registered for the suite to assert on. Individual fixtures then point the
relevant facets (clients, admin, monitoring, storage) at per-test fakes.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from tai42_contract.app import tai42_app
from tai42_kit.utils.detached_util import in_detached_run


class _RecordingTools:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}
        self.tags: dict[str, set[str]] = {}
        self.run_tool_calls: list[tuple[str, dict[str, Any]]] = []
        self.run_tool_result: Any = None
        # The detached flag observed inside each call — a worker execution has no
        # live caller, so the tool must run detached and the turn budget be skipped.
        self.detached_seen: list[bool] = []

    def tool(self, func: Any = None, /, *args: Any, **kwargs: Any) -> Any:
        tags = kwargs.get("tags", set())
        if callable(func):
            self.registered[func.__name__] = func
            self.tags[func.__name__] = tags
            return func

        def decorate(f: Any) -> Any:
            self.registered[f.__name__] = f
            self.tags[f.__name__] = tags
            return f

        return decorate

    async def run_tool(self, key: str, arguments: dict[str, Any]) -> Any:
        self.run_tool_calls.append((key, arguments))
        self.detached_seen.append(in_detached_run())
        return self.run_tool_result


class _RecordingExtensions:
    def __init__(self) -> None:
        self.registered: dict[str, tuple[Any, Any]] = {}

    def extension(self, f: Any = None, *, kind: Any = None, name: str | None = None) -> Any:
        def decorate(fn: Any) -> Any:
            self.registered[name or fn.__name__] = (kind, fn)
            return fn

        return decorate(f) if callable(f) else decorate


class _RecordingBackends:
    def __init__(self) -> None:
        self.registered_cls: type | None = None
        self.instance: Any = None

    def register_backend(self, cls: type | None = None) -> Any:
        def decorate(klass: type) -> type:
            self.registered_cls = klass
            self.instance = klass()
            return klass

        return decorate(cls) if cls is not None else decorate


class _ClientCtx:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def __aenter__(self) -> Any:
        return self._client

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _StubClients:
    def __init__(self) -> None:
        self.client: Any = None
        self.shutdown_calls = 0

    def client_ctx(self, client_cls: Any, settings: Any = None, **kwargs: Any) -> _ClientCtx:
        if self.client is None:
            raise RuntimeError("test must set stub_app.clients.client before using a redis-backed tool")
        return _ClientCtx(self.client)

    async def shutdown_clients(self) -> None:
        self.shutdown_calls += 1


class _StubAdmin:
    """Records admin primitive calls and returns configured results."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.results: dict[str, Any] = {}
        self.live_manifest: dict[str, Any] = {"backend_module": "tai42_backend_celery"}

    def _record(self, op: str, *args: Any) -> Any:
        self.calls.append((op, args))
        result = self.results.get(op)
        if isinstance(result, Exception):
            raise result
        return result

    def update(self, manifest: Any) -> Any:
        return self._record("update", manifest)

    def reload_mcp(self, title: str) -> Any:
        return self._record("reload_mcp", title)

    def deregister_mcp(self, title: str) -> Any:
        return self._record("deregister_mcp", title)

    def reload_config(self) -> Any:
        return self._record("reload_config")

    async def run_tool_reload(self, kind: str, action: str, name: str) -> Any:
        return self._record("run_tool_reload", kind, action, name)

    def reload_failed_mcps(self) -> Any:
        return self._record("reload_failed_mcps")

    def list_failed_mcps(self) -> Any:
        return self._record("list_failed_mcps")


class _StubResourceManager:
    """Renders templates the way the tests need: inline content wins, else the
    id is echoed, else empty."""

    async def render_by_id_or_content(self, content: Any = None, template_id: Any = None, kwargs: Any = None) -> str:
        if content:
            return str(content)
        if template_id:
            return f"rendered:{template_id}"
        return ""


class _RecordingLifecycle:
    """Records ``on_fleet_op_applied`` registrations and drives the boot-ready latch
    the worker gate awaits. ``ready`` is pre-set, so ``wait_until_ready`` resolves at
    once unless a test clears it to exercise the gate."""

    def __init__(self) -> None:
        self.fleet_op_applied: list[Any] = []
        self.ready = asyncio.Event()
        self.ready.set()

    def on_fleet_op_applied(self, func: Any) -> Any:
        self.fleet_op_applied.append(func)
        return func

    async def wait_until_ready(self) -> None:
        await self.ready.wait()


class _StubApp:
    def __init__(self) -> None:
        self.tools = _RecordingTools()
        self.extensions = _RecordingExtensions()
        self.backends = _RecordingBackends()
        self.clients = _StubClients()
        self.admin = _StubAdmin()
        self.lifecycle = _RecordingLifecycle()
        self.storage = MagicMock()
        self.storage.resource_manager = _StubResourceManager()
        self.monitoring = MagicMock()
        self.config = MagicMock()


stub_app_instance = _StubApp()
tai42_app.bind(stub_app_instance)

# Import the plugin ONCE, after the stub is bound, so the import-time
# registration side effects are recorded exactly as a host boot would fire
# them. The single top-package import is the whole discovery surface: it must
# register the backend, the Celery app + tasks, the tools, and the extensions.
import tai42_backend_celery  # noqa: E402,F401


@pytest.fixture
def stub_app() -> _StubApp:
    return stub_app_instance


@pytest.fixture(autouse=True)
def _reset_stub_state() -> Any:
    """Per-test isolation for the mutable parts of the process-wide stub."""
    stub_app_instance.tools.run_tool_calls.clear()
    stub_app_instance.tools.run_tool_result = None
    stub_app_instance.tools.detached_seen.clear()
    stub_app_instance.clients.client = None
    stub_app_instance.clients.shutdown_calls = 0
    stub_app_instance.admin.calls.clear()
    stub_app_instance.admin.results.clear()
    stub_app_instance.admin.live_manifest = {"backend_module": "tai42_backend_celery"}
    # Fresh Event per test: an asyncio.Event binds to the loop of its first await
    # (``_LoopBoundMixin``), and each async test runs on its own loop, so a reused
    # Event would raise "bound to a different event loop".
    stub_app_instance.lifecycle.ready = asyncio.Event()
    stub_app_instance.lifecycle.ready.set()
    stub_app_instance.monitoring.reset_mock()
    stub_app_instance.config.reset_mock()
    return None


class FakePipeline:
    """Buffered-command pipeline over a :class:`FakeRedis` (MULTI/EXEC shape).

    Commands queue synchronously (each call returns the pipeline) and
    ``execute`` runs them as one uninterruptible batch against the base
    ``FakeRedis`` state operations — like a real transaction, a subclass's
    command overrides (the tests' concurrent writers) cannot interleave inside
    it. A concurrent write is modeled at the batch boundary via
    :meth:`FakeRedis.on_pipeline_execute`.
    """

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._commands: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> FakePipeline:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        self._commands = []
        return False

    def hget(self, key: str, field: str) -> FakePipeline:
        self._commands.append(("hget", (key, field)))
        return self

    def zscore(self, zset: str, member: str) -> FakePipeline:
        self._commands.append(("zscore", (zset, member)))
        return self

    async def execute(self) -> list[Any]:
        commands, self._commands = self._commands, []
        self._redis.on_pipeline_execute(commands)
        return [await getattr(FakeRedis, name)(self._redis, *args) for name, args in commands]


class FakeRedis:
    """Minimal async redis stub backed by an in-memory hash-of-hashes + zset."""

    def __init__(self, hashes: dict[str, dict[str, Any]] | None = None, zset: dict[str, float] | None = None) -> None:
        self.hashes = hashes or {}
        self.zset = zset or {}

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)

    def on_pipeline_execute(self, commands: list[tuple[str, tuple[Any, ...]]]) -> None:
        """Hook for tests: a concurrent write landing just before a pipeline
        batch executes (the batch itself is atomic)."""

    async def hget(self, key: str, field: str) -> Any:
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key: str, field: str, value: Any) -> None:
        self.hashes.setdefault(key, {})[field] = value

    async def zscore(self, _zset: str, member: str) -> Any:
        return self.zset.get(member)

    async def zadd(self, _zset: str, mapping: dict[str, float]) -> int:
        self.zset.update(mapping)
        return len(mapping)

    async def zrem(self, _zset: str, member: str) -> int:
        return 1 if self.zset.pop(member, None) is not None else 0

    async def zrange(self, _zset: str, start: int, end: int, withscores: bool = False) -> list[Any]:
        members = [m.encode() if isinstance(m, str) else m for m in self.zset]
        if withscores:
            return [(m, self.zset[m.decode() if isinstance(m, bytes) else m]) for m in members]
        return members

    async def exists(self, key: str) -> int:
        return 1 if key in self.hashes else 0

    async def delete(self, key: str) -> int:
        return 1 if self.hashes.pop(key, None) is not None else 0


@pytest.fixture
def fake_redis(stub_app: _StubApp) -> FakeRedis:
    redis = FakeRedis()
    stub_app.clients.client = redis
    return redis
