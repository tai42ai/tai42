"""Bind a recording stub app to the ``tai42_app`` handle before the plugin is
imported, and provide the shared in-memory Redis fakes.

The plugin registers its backend, tools, extensions, and lifecycle hooks through
``tai42_app`` at import time. Binding the stub here (at collection time, before
any test imports the plugin) captures those registrations so tests can assert on
them and call the registered functions directly.
"""

from __future__ import annotations

import asyncio
import fnmatch
import itertools
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.extensions import ExtensionKind

# -- The recording stub app ------------------------------------------------------


class StubTools:
    def __init__(self) -> None:
        self.registered: dict[str, Callable[..., Any]] = {}
        self.tags: dict[str, set[str]] = {}
        self.run_tool_mock = AsyncMock(return_value=None)

    def tool(self, *args: Any, force: bool = False, **kwargs: Any) -> Any:
        tags = kwargs.get("tags", set())

        def register(func: Callable[..., Any]) -> Callable[..., Any]:
            self.registered[func.__name__] = func
            self.tags[func.__name__] = tags
            return func

        if args and callable(args[0]):
            return register(args[0])
        return register

    async def run_tool(self, key: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        return await self.run_tool_mock(key, arguments)


class StubExtensions:
    def __init__(self) -> None:
        self.registered: dict[str, tuple[ExtensionKind, Callable[..., Any]]] = {}

    def extension(self, f: Callable[..., Any] | None = None, *, kind: ExtensionKind, name: str | None = None) -> Any:
        def register(func: Callable[..., Any]) -> Callable[..., Any]:
            self.registered[name or func.__name__] = (kind, func)
            return func

        if f and callable(f):
            return register(f)
        return register


class StubBackends:
    def __init__(self) -> None:
        self.registered_cls: type | None = None
        self.instance: Any = None

    def register_backend(self, cls: type | None = None) -> Any:
        def decorator(klass: type) -> type:
            self.registered_cls = klass
            self.instance = klass()
            return klass

        return decorator(cls) if cls is not None else decorator


class StubLifecycle:
    """Records shutdown handlers and drives the boot-ready latch the worker gate
    awaits. ``ready`` is pre-set, so ``wait_until_ready`` resolves at once unless a
    test clears it to exercise the gate."""

    def __init__(self) -> None:
        self.shutdown_handlers: list[Callable[..., Any]] = []
        self.ready = asyncio.Event()
        self.ready.set()

    def on_shutdown(self, func: Callable[..., Any]) -> Callable[..., Any]:
        self.shutdown_handlers.append(func)
        return func

    async def wait_until_ready(self) -> None:
        await self.ready.wait()


class StubAdmin:
    """Records every admin-primitive call and answers with canned results."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def update(self, manifest: Any) -> None:
        self.calls.append(("update", (manifest,)))

    def reload_mcp(self, title: str) -> dict[str, Any]:
        self.calls.append(("reload_mcp", (title,)))
        return {"title": title, "status": "ok"}

    def deregister_mcp(self, title: str) -> dict[str, Any]:
        self.calls.append(("deregister_mcp", (title,)))
        return {"title": title, "status": "absent"}

    async def run_tool_reload(self, kind: str, action: str, name: str) -> dict[str, Any]:
        self.calls.append(("run_tool_reload", (kind, action, name)))
        return {"kind": kind, "action": action, "name": name, "status": "ok"}

    def reload_config(self) -> dict[str, Any]:
        self.calls.append(("reload_config", ()))
        return {"status": "ok", "env_keys": 3}

    def reload_failed_mcps(self) -> list[dict[str, Any]]:
        self.calls.append(("reload_failed_mcps", ()))
        return [{"title": "srv", "status": "ok"}]

    def list_failed_mcps(self) -> list[dict[str, Any]]:
        self.calls.append(("list_failed_mcps", ()))
        return [{"title": "srv", "status": "unavailable"}]


class StubResourceManager:
    async def render_by_id_or_content(
        self, content: str | None = None, template_id: str | None = None, kwargs: dict[str, Any] | None = None
    ) -> str:
        return content or ""


class StubStorage:
    def __init__(self) -> None:
        self.resource_manager = StubResourceManager()


class StubApp:
    def __init__(self) -> None:
        self.tools = StubTools()
        self.extensions = StubExtensions()
        self.backends = StubBackends()
        self.lifecycle = StubLifecycle()
        self.admin = StubAdmin()
        self.storage = StubStorage()


_stub_app = StubApp()
tai42_app.bind(_stub_app)

# Imported AFTER the bind so every import-time registration lands in the stub.
import tai42_backend_arq  # noqa: E402,F401


@pytest.fixture
def stub_app() -> StubApp:
    return _stub_app


@pytest.fixture(autouse=True)
def _reset_stub_run_tool() -> None:
    _stub_app.tools.run_tool_mock = AsyncMock(return_value=None)
    _stub_app.admin.calls.clear()
    # A fresh, pre-set boot-ready latch per test: the stub app is process-global,
    # so a reused Event would stay bound to a prior test's (closed) loop.
    _stub_app.lifecycle.ready = asyncio.Event()
    _stub_app.lifecycle.ready.set()


# -- In-memory Redis fakes ---------------------------------------------------------


class FakeRedis:
    """In-memory stand-in for the arq Redis handle.

    Supports the surface the schedule tools and ``safe_schedule_transition``
    touch: ``scan_iter``, ``hgetall``, ``hset``, ``exists``, ``delete``,
    ``set``, ``lock`` and ``enqueue_job``. Hash fields are stored as bytes to
    mirror redis-py's decode-less responses.
    """

    def __init__(self) -> None:
        self._store: dict[str, dict[bytes, bytes]] = {}
        self._kv: dict[str, str] = {}
        self._job_ids = (f"job-{n}" for n in itertools.count())
        self.enqueued: list[tuple[Any, ...]] = []

    @staticmethod
    def _skey(key: Any) -> str:
        return key.decode() if isinstance(key, bytes) else key

    @staticmethod
    def _bval(val: Any) -> bytes:
        return val if isinstance(val, bytes) else str(val).encode()

    async def hgetall(self, key: Any) -> dict[bytes, bytes]:
        return dict(self._store.get(self._skey(key), {}))

    async def hset(self, key: Any, *args: Any, mapping: Any = None, **kwargs: Any) -> None:
        skey = self._skey(key)
        bucket = self._store.setdefault(skey, {})
        items = dict(mapping or {})
        if args:
            field, value = args
            items[field] = value
        items.update(kwargs)
        for field, value in items.items():
            bucket[self._bval(field)] = self._bval(value)

    async def exists(self, key: Any) -> int:
        return 1 if self._skey(key) in self._store else 0

    async def delete(self, key: Any) -> None:
        self._store.pop(self._skey(key), None)
        self._kv.pop(self._skey(key), None)

    async def set(self, key: Any, value: Any, ex: Any = None, nx: bool = False) -> Any:
        skey = self._skey(key)
        if nx and skey in self._kv:
            return None
        self._kv[skey] = value
        return True

    def scan_iter(self, match: Any = None) -> Any:
        pattern = match.decode() if isinstance(match, bytes) else match

        async def _gen() -> Any:
            for k in [*self._store, *self._kv]:
                if pattern is None or fnmatch.fnmatch(k, pattern):
                    yield k.encode()

        return _gen()

    @asynccontextmanager
    async def lock(self, *args: Any, **kwargs: Any) -> AsyncIterator[None]:
        yield

    async def enqueue_job(self, *args: Any, **kwargs: Any) -> Any:
        self.enqueued.append(args)
        job_id = kwargs.get("_job_id") or next(self._job_ids)
        return type("_Job", (), {"job_id": job_id})()


class DeleteOnLockRedis:
    """A :class:`FakeRedis` wrapper whose ``lock`` deletes one key on entry.

    Models a concurrent delete winning the per-schedule lock just before a
    locked schedule mutation (a ``safe_schedule_transition``, an enabled-flag
    write, a delete) re-checks the hash under the lock.
    """

    def __init__(self, base: Any, key: str) -> None:
        self._base = base
        self._key = key

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    @asynccontextmanager
    async def lock(self, *args: Any, **kwargs: Any) -> AsyncIterator[None]:
        await self._base.delete(self._key)
        async with self._base.lock(*args, **kwargs):
            yield


class FakePubSub:
    """One pub/sub subscriber over a :class:`FakePubSubRedis` hub."""

    def __init__(self, hub: FakePubSubRedis) -> None:
        self._hub = hub
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._channels: set[str] = set()

    async def subscribe(self, channel: str) -> None:
        self._hub.subscribers.setdefault(channel, set()).add(self)
        self._channels.add(channel)
        # Mirror Redis's subscribe confirmation, sent before any published message.
        self._queue.put_nowait({"type": "subscribe", "channel": channel, "data": len(self._channels)})

    async def unsubscribe(self, channel: str) -> None:
        self._hub.subscribers.get(channel, set()).discard(self)
        self._channels.discard(channel)

    async def aclose(self) -> None:
        for channel in list(self._channels):
            await self.unsubscribe(channel)

    async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float | None = None) -> Any:
        _subscribe_types = ("subscribe", "unsubscribe", "psubscribe", "punsubscribe")
        try:
            while True:
                msg = await asyncio.wait_for(self._queue.get(), timeout)
                if ignore_subscribe_messages and msg.get("type") in _subscribe_types:
                    continue
                return msg
        except TimeoutError:
            return None

    def deliver(self, message: dict[str, Any]) -> None:
        self._queue.put_nowait(message)


class FakePubSubRedis(FakeRedis):
    """A :class:`FakeRedis` with a working in-process pub/sub hub."""

    def __init__(self) -> None:
        super().__init__()
        self.subscribers: dict[str, set[FakePubSub]] = {}

    def pubsub(self) -> FakePubSub:
        return FakePubSub(self)

    async def publish(self, channel: str, data: str) -> int:
        receivers = list(self.subscribers.get(channel, ()))
        for pubsub in receivers:
            pubsub.deliver({"type": "message", "channel": channel, "data": data})
        return len(receivers)


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def hub() -> FakePubSubRedis:
    return FakePubSubRedis()


@pytest.fixture
def bind_pool(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], None]:
    """Route ``RedisPoolManager.get`` (everywhere it is imported) to a fake."""
    from tai42_backend_arq.pool import RedisPoolManager

    def bind(fake: Any) -> None:
        monkeypatch.setattr(RedisPoolManager, "get", AsyncMock(return_value=fake))

    return bind
