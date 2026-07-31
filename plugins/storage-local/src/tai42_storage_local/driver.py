"""Async local-filesystem driver: raw file I/O rooted at a base path.

Every path is resolved under ``root`` and rejected if it escapes the boundary.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

import aiofiles
import aiofiles.os

logger = logging.getLogger(__name__)


class AsyncLocalDriver:
    def __init__(self, root_path: str, create_dirs: bool):
        self.root_path = root_path
        self.create_dirs = create_dirs

    async def _resolve_under_root(self, path: str) -> tuple[Path, Path]:
        """Resolve the root and ``path`` beneath it, refusing an escape."""

        def _resolve() -> tuple[Path, Path]:
            root = Path(self.root_path).resolve()
            full_path = (root / path).resolve()
            # Boundary check, not a string prefix, so a sibling path can't escape the root.
            if not full_path.is_relative_to(root):
                raise ValueError(f"Path {path} is outside the storage root")
            return root, full_path

        return await asyncio.to_thread(_resolve)

    async def _ensure_parent(self, target: Path) -> None:
        if self.create_dirs:
            await aiofiles.os.makedirs(target.parent, exist_ok=True)

    async def read_file(self, path: str) -> str:
        _, target = await self._resolve_under_root(path)
        async with aiofiles.open(target, encoding="utf-8") as f:
            return await f.read()

    async def write_file(self, path: str, content: str) -> None:
        _, target = await self._resolve_under_root(path)
        await self._ensure_parent(target)
        async with aiofiles.open(target, mode="w", encoding="utf-8") as f:
            await f.write(content)

    async def read_bytes(self, path: str) -> bytes:
        _, target = await self._resolve_under_root(path)
        async with aiofiles.open(target, mode="rb") as f:
            return await f.read()

    async def write_bytes(self, path: str, data: bytes) -> None:
        _, target = await self._resolve_under_root(path)
        await self._ensure_parent(target)
        async with aiofiles.open(target, mode="wb") as f:
            await f.write(data)

    async def delete_file(self, path: str) -> None:
        _, target = await self._resolve_under_root(path)
        await aiofiles.os.remove(target)

    async def delete_dir(self, path: str) -> None:
        root, target = await self._resolve_under_root(path)
        if target == root:
            raise ValueError(f"Refusing to delete the storage root (path resolves to it): {path}")

        def _on_rmtree_error(func: object, failed_path: str, exc: BaseException) -> None:
            # Tolerate a file vanishing mid-delete (idempotent); re-raise anything else loudly.
            if isinstance(exc, FileNotFoundError):
                logger.info("Path %s already gone during dir delete of %s; skipping", failed_path, path)
                return
            raise exc

        def _remove() -> None:
            if not target.is_dir():
                raise FileNotFoundError(f"Storage directory not found: {path}")
            shutil.rmtree(target, onexc=_on_rmtree_error)

        await asyncio.to_thread(_remove)

    async def list_recursive(self) -> list[str]:
        def _walk() -> list[str]:
            root = Path(self.root_path).resolve()
            results: list[str] = []
            if not root.exists():
                return results
            for dirpath, _, files in os.walk(root):
                for file in files:
                    full_path = Path(dirpath) / file
                    rel_path = full_path.relative_to(root)
                    results.append(str(rel_path))
            return results

        return await asyncio.to_thread(_walk)
