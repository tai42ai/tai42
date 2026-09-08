"""Async local-filesystem driver: raw file I/O rooted at a base path.

Every path is resolved under ``root`` and rejected if it escapes the boundary.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import aiofiles
from tai42_contract.storage import StoragePathConflictError

logger = logging.getLogger(__name__)


class AsyncLocalDriver:
    def __init__(self, root_path: str, create_dirs: bool):
        self.root_path = root_path
        self.create_dirs = create_dirs
        # Test seam only: a hook invoked between an empty-directory emptiness scan and
        # its removal, so a racing write can be exercised. ``None`` in every real use.
        self._before_empty_dir_removal: Callable[[], None] | None = None

    async def _resolve_under_root(self, path: str) -> tuple[Path, Path]:
        """Resolve the root and ``path`` beneath it, refusing an escape."""
        return await asyncio.to_thread(self._resolve, path)

    async def _prepare_write_target(self, path: str) -> Path:
        """Resolve ``path`` for writing, refusing a file/directory id collision.

        Files and directories share one id space here: an id that still names a
        non-empty directory cannot also become a file, and an id nested beneath an
        id already held as a file cannot be created. Either collision raises
        :class:`StoragePathConflictError` naming the stored ids in the way. An id
        naming an empty leftover directory tree has that tree removed so the id is
        free to hold a file, and parents are created when ``create_dirs`` is set.
        """

        def _prepare() -> Path:
            root, target = self._resolve(path)
            if target.is_dir():
                contained = sorted(p.relative_to(root).as_posix() for p in target.rglob("*") if p.is_file())
                if contained:
                    raise StoragePathConflictError(path, contained)
                self._remove_empty_dir_tree(root, target, path)
            if self.create_dirs:
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                except (NotADirectoryError, FileExistsError) as exc:
                    blocker = self._blocking_ancestor_id(root, target)
                    if blocker is None:
                        raise
                    raise StoragePathConflictError(path, [blocker]) from exc
            return target

        return await asyncio.to_thread(_prepare)

    def _remove_empty_dir_tree(self, root: Path, target: Path, path: str) -> None:
        """Remove the empty directory tree at ``target``, deepest-first, ``rmdir`` only.

        ``rmdir`` refuses a non-empty directory, so a file that races in after the
        emptiness scan makes the removal fail without destroying anything: the
        survivors are named in a :class:`StoragePathConflictError` instead of being
        silently wiped by an ``rmtree``. Removing the store root is refused, mirroring
        :meth:`delete_dir`.
        """
        if target == root:
            raise ValueError(f"Refusing to remove the storage root (path resolves to it): {path}")
        # A test seam: a hook fired between the emptiness scan and the removal,
        # so a file racing into the tree can be exercised deterministically.
        if self._before_empty_dir_removal is not None:
            self._before_empty_dir_removal()
        subdirs = sorted((p for p in target.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True)
        for directory in (*subdirs, target):
            try:
                directory.rmdir()
            except OSError as exc:
                survivors = sorted(p.relative_to(root).as_posix() for p in target.rglob("*") if p.is_file())
                raise StoragePathConflictError(path, survivors) from exc

    def _resolve(self, path: str) -> tuple[Path, Path]:
        root = Path(self.root_path).resolve()
        full_path = (root / path).resolve()
        # Boundary check, not a string prefix, so a sibling path can't escape the root.
        if not full_path.is_relative_to(root):
            raise ValueError(f"Path {path} is outside the storage root")
        return root, full_path

    @staticmethod
    def _blocking_ancestor_id(root: Path, target: Path) -> str | None:
        """The store id of the nearest ancestor of ``target`` that exists as a
        non-directory (a file, a device) — the one blocking a directory
        from being created for ``target``; ``None`` when the walk reaches the root
        with nothing blocking."""
        for ancestor in target.parents:
            if ancestor == root:
                break
            if ancestor.exists() and not ancestor.is_dir():
                return ancestor.relative_to(root).as_posix()
        return None

    @staticmethod
    def _prune_empty_parents(root: Path, target: Path) -> None:
        """Remove now-empty directories from ``target``'s parent up to (never
        including) the store root, so a delete leaves no empty scaffolding."""
        for parent in target.parents:
            if parent == root:
                break
            try:
                parent.rmdir()
            except OSError:
                # A non-empty parent (or one already gone) stops the climb.
                break

    async def read_file(self, path: str) -> str:
        _, target = await self._resolve_under_root(path)
        async with aiofiles.open(target, encoding="utf-8") as f:
            return await f.read()

    async def write_file(self, path: str, content: str) -> None:
        target = await self._prepare_write_target(path)
        async with aiofiles.open(target, mode="w", encoding="utf-8") as f:
            await f.write(content)

    async def read_bytes(self, path: str) -> bytes:
        _, target = await self._resolve_under_root(path)
        async with aiofiles.open(target, mode="rb") as f:
            return await f.read()

    async def write_bytes(self, path: str, data: bytes) -> None:
        target = await self._prepare_write_target(path)
        async with aiofiles.open(target, mode="wb") as f:
            await f.write(data)

    async def delete_file(self, path: str) -> None:
        root, target = await self._resolve_under_root(path)

        def _remove() -> None:
            target.unlink()
            self._prune_empty_parents(root, target)

        await asyncio.to_thread(_remove)

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
            self._prune_empty_parents(root, target)

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
