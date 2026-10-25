"""An in-memory storage provider that records every read it serves."""

from __future__ import annotations

from collections.abc import MutableMapping

from tai42_contract.storage import ObjectStat, Storage


class RecordingStorage(Storage):
    """Holds ``{id: (bytes, content_type)}`` and appends each read to ``calls``.

    A missing id raises ``FileNotFoundError`` on every read, as a real provider does.
    """

    def __init__(self, objects: dict[str, tuple[bytes, str | None]] | None = None) -> None:
        self.objects: dict[str, tuple[bytes, str | None]] = dict(objects or {})
        self.calls: list[tuple] = []

    def put(self, path: str, data: bytes, content_type: str | None = None) -> None:
        self.objects[path] = (data, content_type)

    def _data(self, path: str) -> bytes:
        if path not in self.objects:
            raise FileNotFoundError(path)
        return self.objects[path][0]

    async def load(self, path: str) -> str:
        self.calls.append(("load", path))
        return self._data(path).decode("utf-8")

    async def load_bytes(self, path: str) -> bytes:
        self.calls.append(("load_bytes", path))
        return self._data(path)

    async def load_range(self, path: str, start: int, length: int) -> bytes:
        self.calls.append(("load_range", path, start, length))
        return self._data(path)[start : start + length]

    async def stat(self, path: str) -> ObjectStat:
        self.calls.append(("stat", path))
        data = self._data(path)
        return ObjectStat(content_type=self.objects[path][1], size=len(data))

    async def list(self) -> list[str]:
        return sorted(self.objects)

    async def upload(self, path: str, content: str) -> None:
        self.objects[path] = (content.encode("utf-8"), None)

    async def upload_bytes(self, path: str, data: bytes, content_type: str | None = None) -> None:
        self.objects[path] = (data, content_type)

    async def delete(self, path: str) -> None:
        if path not in self.objects:
            raise FileNotFoundError(path)
        del self.objects[path]

    async def delete_dir(self, path: str) -> None:
        prefix = f"{path}/"
        doomed = [key for key in self.objects if key.startswith(prefix)]
        if not doomed:
            raise FileNotFoundError(path)
        for key in doomed:
            del self.objects[key]


class MappedStorage(RecordingStorage):
    """Answers reads from a live ``{id: text}`` mapping another fake also serves, so both see one store."""

    def __init__(self, items: MutableMapping[str, str]) -> None:
        super().__init__()
        self.items = items

    def _data(self, path: str) -> bytes:
        if path not in self.items:
            raise FileNotFoundError(path)
        return self.items[path].encode("utf-8")

    async def stat(self, path: str) -> ObjectStat:
        self.calls.append(("stat", path))
        return ObjectStat(content_type=None, size=len(self._data(path)))

    async def list(self) -> list[str]:
        return sorted(self.items)
