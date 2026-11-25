"""A ResourceManager over an in-memory ``Storage`` double that records every load, delete and directory delete.

A load of a held path waits until the test releases it, so a test can see which loads are in flight
together; a path given a failure raises it when loaded.
"""

from __future__ import annotations

import asyncio
import mimetypes
from collections.abc import Awaitable, Callable

from tai42_contract.storage import ObjectStat, Storage
from tai42_contract.tools import ReferenceHolder

from tai42_skeleton.storage import StorageRegistry
from tai42_skeleton.template import ResourceManager

from .._fakes.listing import keys_under


class _InMemoryStorage(Storage):
    def __init__(self, items: dict[str, str] | None = None) -> None:
        self.items: dict[str, str] = dict(items or {})
        self.deleted: list[str] = []
        self.deleted_dirs: list[str] = []
        self.loads: list[str] = []
        self.cancelled_loads: list[str] = []
        self.failures: dict[str, Exception] = {}
        self.in_flight = 0
        self.max_in_flight = 0
        self._held: dict[str, asyncio.Event] = {}

    def hold(self, *paths: str) -> asyncio.Event:
        """Hold every load of ``paths`` until the returned event is set."""
        release = asyncio.Event()
        for path in paths:
            self._held[path] = release
        return release

    async def until_in_flight(self, count: int) -> None:
        """Return once ``count`` loads are in flight together; a second's wait means they are not."""
        async with asyncio.timeout(1):
            while self.in_flight < count:
                await asyncio.sleep(0)

    async def load(self, path: str) -> str:
        self.loads.append(path)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        # A load answers the text stored when it began, as a read that a write lands behind does.
        stored = self.items.get(path)
        try:
            held = self._held.get(path)
            if held is not None:
                await held.wait()
            if path in self.failures:
                raise self.failures[path]
            if stored is None:
                raise FileNotFoundError(path)
            return stored
        except asyncio.CancelledError:
            self.cancelled_loads.append(path)
            raise
        finally:
            self.in_flight -= 1

    async def list(self, directory: str | None = None) -> list[str]:
        return keys_under(sorted(self.items), directory)

    async def upload(self, path: str, content: str) -> None:
        self.items[path] = content

    async def delete(self, path: str) -> None:
        self.deleted.append(path)
        self.items.pop(path, None)

    async def delete_dir(self, path: str) -> None:
        self.deleted_dirs.append(path)
        prefix = path.rstrip("/") + "/"
        for key in [k for k in self.items if k.startswith(prefix)]:
            del self.items[key]

    async def stat(self, path: str) -> ObjectStat:
        content_type, _ = mimetypes.guess_type(path)
        return ObjectStat(content_type=content_type, size=len(await self.load_bytes(path)))

    async def load_range(self, path: str, start: int, length: int) -> bytes:
        return (await self.load_bytes(path))[start : start + length]


async def _no_holders(ids: frozenset[str]) -> list[ReferenceHolder]:
    return []


def _manager(
    items: dict[str, str] | None = None,
    held_by: Callable[[frozenset[str]], Awaitable[list[ReferenceHolder]]] = _no_holders,
) -> tuple[ResourceManager, _InMemoryStorage]:
    registry = StorageRegistry()
    store = _InMemoryStorage(items)
    registry.register_storage(lambda: store)  # type: ignore[arg-type]
    return ResourceManager(registry.provider, on_evicted=lambda _eviction: None, held_by=held_by), store
