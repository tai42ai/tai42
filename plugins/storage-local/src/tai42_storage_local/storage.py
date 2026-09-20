"""Local-filesystem :class:`~tai42_contract.storage.Storage` backend.

Builds a stateless :class:`~tai42_storage_local.driver.AsyncLocalDriver` per call
from cached settings, so a live-reloaded root path takes effect on the next call.
"""

from __future__ import annotations

import logging

from tai42_contract.app import tai42_app
from tai42_contract.storage import Storage, assert_not_root

from tai42_storage_local.driver import AsyncLocalDriver
from tai42_storage_local.settings import storage_settings

logger = logging.getLogger(__name__)


# Importing this module registers LocalStorage in the storage provider registry.
@tai42_app.storage.register_storage
class LocalStorage(Storage):
    """The local-filesystem :class:`~tai42_contract.storage.Storage` implementation."""

    def _driver(self) -> AsyncLocalDriver:
        settings = storage_settings()
        return AsyncLocalDriver(root_path=settings.root_path, create_dirs=settings.create_dirs)

    async def load(self, path: str) -> str:
        """Read the text object at ``path``, raising ``FileNotFoundError`` on a miss."""
        try:
            return await self._driver().read_file(path)
        except FileNotFoundError as e:
            # Re-raise with the relative path so the absolute root isn't leaked.
            raise FileNotFoundError(f"Object not found: {path}") from e

    async def list(self) -> list[str]:
        """Every object path in the store, listed recursively."""
        return await self._driver().list_recursive()

    async def upload(self, path: str, content: str) -> None:
        """Write ``content`` as the text object at ``path``."""
        await self._driver().write_file(path, content)
        logger.info("Wrote object to local storage: %s", path)

    async def delete(self, path: str) -> None:
        """Delete the object at ``path``, raising ``FileNotFoundError`` on a miss."""
        try:
            await self._driver().delete_file(path)
        except FileNotFoundError as e:
            raise FileNotFoundError(f"Object not found: {path}") from e
        logger.info("Deleted object from local storage: %s", path)

    async def delete_dir(self, path: str) -> None:
        """Delete the directory subtree at ``path`` (never the storage root)."""
        assert_not_root(path)
        await self._driver().delete_dir(path)
        logger.info("Deleted directory from local storage: %s", path)

    async def load_bytes(self, path: str) -> bytes:
        """Read the binary object at ``path``, raising ``FileNotFoundError`` on a miss."""
        try:
            return await self._driver().read_bytes(path)
        except FileNotFoundError as e:
            raise FileNotFoundError(f"Object not found: {path}") from e

    async def upload_bytes(self, path: str, data: bytes, content_type: str | None = None) -> None:
        """Write ``data`` as the binary object at ``path`` (``content_type`` is accepted but not stored)."""
        # Local filesystem stores no MIME metadata; content_type is accepted for
        # contract parity but not stored.
        await self._driver().write_bytes(path, data)
        logger.info("Wrote bytes to local storage: %s", path)
