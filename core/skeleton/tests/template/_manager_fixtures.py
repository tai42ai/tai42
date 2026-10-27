"""A ResourceManager over an in-memory ``Storage`` double that records every delete and directory delete."""

from __future__ import annotations

import mimetypes

from tai42_contract.storage import ObjectStat, Storage

from tai42_skeleton.storage import StorageRegistry
from tai42_skeleton.template import ResourceManager

from .._fakes.listing import keys_under


class _InMemoryStorage(Storage):
    def __init__(self, items: dict[str, str] | None = None) -> None:
        self.items: dict[str, str] = dict(items or {})
        self.deleted: list[str] = []
        self.deleted_dirs: list[str] = []

    async def load(self, path: str) -> str:
        try:
            return self.items[path]
        except KeyError as exc:
            raise FileNotFoundError(path) from exc

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


def _manager(items: dict[str, str] | None = None) -> tuple[ResourceManager, _InMemoryStorage]:
    registry = StorageRegistry()
    store = _InMemoryStorage(items)
    registry.register_storage(lambda: store)  # type: ignore[arg-type]
    return ResourceManager(registry.provider, on_evicted=lambda _eviction: None), store
