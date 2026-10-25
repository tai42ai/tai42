"""Shared rig for the ``langchain_deep_agent`` backend test modules: the fake
resource manager the ``fake_tm`` fixture installs in each module.
"""

from __future__ import annotations


class _FakeResourceManager:
    """In-memory stand-in for ``tai42_app.storage.resource_manager``.

    Stores raw key -> content (``str`` text or ``bytes``). ``load`` is the byte
    read: like ``ResourceManager.load`` over a storage id it returns
    ``(bytes, None)`` with ``with_mime=False``, or raises the storage's
    ``FileNotFoundError`` for a missing key. ``list_resources`` returns flat keys
    like the real providers (no leading slash). ``phantom_keys`` are listed but
    absent from the store, modelling a key that vanishes between the listing and
    the fetch.
    """

    def __init__(self) -> None:
        self.store: dict[str, str | bytes] = {}
        self.phantom_keys: set[str] = set()

    async def upload_template(self, path: str, content: str | bytes) -> None:
        self.store[path] = content

    async def list_resources(self) -> list[str]:
        return list(self.store) + [key for key in self.phantom_keys if key not in self.store]

    async def load(self, source: str, *, with_mime: bool = True) -> tuple[bytes, str | None]:
        try:
            stored = self.store[source]
        except KeyError as exc:
            raise FileNotFoundError(source) from exc
        return (stored.encode("utf-8") if isinstance(stored, str) else stored), None
